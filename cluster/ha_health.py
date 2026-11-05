"""Bounded, read-only HA connectivity evidence; never elects or demotes a leader.

All failures mean "unreachable from this observer", not "the host is dead".
Patroni and etcd remain the only database leadership/lease authorities.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def utc():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def request_json(url, timeout=4):
    started = time.monotonic()
    try:
        with HTTP.open(url, timeout=timeout) as response:
            payload = json.loads(response.read(1024 * 1024))
            return {'http': response.status, 'elapsed_ms': round((time.monotonic()-started)*1000),
                    'data': payload, 'error': ''}
    except urllib.error.HTTPError as error:
        return {'http': error.code, 'elapsed_ms': round((time.monotonic()-started)*1000),
                'data': {}, 'error': 'http-error'}
    except (OSError, ValueError, TimeoutError) as error:
        return {'http': 0, 'elapsed_ms': round((time.monotonic()-started)*1000),
                'data': {}, 'error': type(error).__name__}


def probe_node(node, timeout=4, transport=request_json):
    ip = node['wireguard']['ip']
    ident = str(node['id'])
    paths = {
        'agent': f"http://{ip}:{int(node.get('health_port', 9187))}/ha/live",
        'patroni': f"http://{ip}:{int(node.get('postgres', {}).get('patroni_rest_port', 8008))}/patroni",
    }
    if node.get('dcs_voter', True):
        paths['etcd'] = f"http://{ip}:{int(node.get('etcd', {}).get('client_port', 2379))}/health"
    result = {'node': ident, 'sampled_at': utc(), 'endpoints': {}}
    for kind, url in paths.items():
        raw = transport(url, timeout)
        data = raw.get('data') or {}
        # A 200 from the wrong application/agent is not health evidence.
        if kind == 'agent':
            valid = data.get('ok') is True and str(data.get('node')) == ident
        elif kind == 'patroni':
            valid = (str(data.get('patroni', {}).get('name', ident)) == ident and
                     data.get('state') == 'running' and
                     data.get('role') in ('primary', 'master', 'leader', 'replica', 'standby_leader'))
        else:
            valid = data.get('health') in (True, 'true')
        result['endpoints'][kind] = {
            'http': raw['http'], 'healthy': raw['http'] == 200 and valid,
            'elapsed_ms': raw['elapsed_ms'], 'error': raw['error'],
        }
        if kind == 'patroni':
            result['endpoints'][kind].update(role=data.get('role'), state=data.get('state'), timeline=data.get('timeline'),
                                                  dcs_last_seen=data.get('dcs_last_seen'),
                                                  postmaster_start_time=data.get('postmaster_start_time'))
    if node.get('dcs_voter', True):
        began = time.monotonic()
        try:
            with socket.create_connection((ip, int(node.get('etcd', {}).get('peer_port', 2380))), timeout):
                connected = True
        except OSError:
            connected = False
        # TCP reachability is NOT a Raft/quorum health check.
        result['raft_tcp'] = {'reachable': connected, 'elapsed_ms': round((time.monotonic()-began)*1000)}
    return result


class Evidence:
    def __init__(self, minimum_samples=3, minimum_seconds=30):
        self.minimum_samples = minimum_samples
        self.minimum_seconds = minimum_seconds
        self.count = 0
        self.since = None
        self.confirmed = False

    def update(self, healthy, now):
        previous = self.confirmed
        if healthy:
            self.count, self.since, self.confirmed = 0, None, False
        else:
            if self.since is None:
                self.since = now
            self.count += 1
            self.confirmed = (self.count >= self.minimum_samples and
                              now - self.since >= self.minimum_seconds)
        return self.confirmed, previous and not self.confirmed


class HaHealth:
    def __init__(self, nodes, local_node, runtime, stop, emit, interval=15):
        self.nodes = nodes
        self.local_node = local_node
        self.runtime = Path(runtime)
        self.stop = stop
        self.emit = emit
        self.interval = interval
        self.lock = threading.RLock()
        self.value = {'observer': local_node, 'sampled_at': None, 'nodes': [], 'quorum_observed': None}
        self.evidence = {}
        self.thread = None

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.value))

    def record(self, rows, now=None):
        now = time.monotonic() if now is None else now
        voters = [str(n['id']) for n in self.nodes if n.get('dcs_voter', True)]
        healthy_voters = 0
        for row in rows:
            endpoints = row['endpoints']
            if row['node'] in voters and endpoints.get('etcd', {}).get('healthy'):
                healthy_voters += 1
            broken = sorted(kind for kind, detail in endpoints.items() if not detail['healthy'])
            if row.get('raft_tcp', {}).get('reachable') is False:
                broken.append('raft-tcp')
            key = row['node']
            evidence = self.evidence.setdefault(key, Evidence())
            was_confirmed = evidence.confirmed
            confirmed, recovered = evidence.update(not broken, now)
            row.update(failed_checks=broken, failure_samples=evidence.count,
                       failure_seconds=0 if evidence.since is None else round(now-evidence.since, 1),
                       confirmed_degraded=confirmed)
            if confirmed and not was_confirmed:
                self.emit('ha.connectivity_degraded', 'Нарушена связность HA',
                          f"Observer={self.local_node}; node={key}; checks={','.join(broken)}; "
                          f"samples={evidence.count}; duration={row['failure_seconds']}s. "
                          'This is a connectivity observation, not a declaration of host failure.', 'warning', node=key)
            elif recovered:
                self.emit('ha.connectivity_recovered', 'Связность HA восстановлена',
                          f'Observer={self.local_node}; node={key}; all monitored endpoints recovered.', 'success', node=key)
        value = {'observer': self.local_node, 'sampled_at': utc(), 'nodes': rows,
                 'healthy_voters_observed': healthy_voters, 'voters': len(voters),
                 'quorum_observed': healthy_voters >= len(voters)//2+1,
                 'confirmation_samples': 3, 'confirmation_seconds': 30}
        with self.lock:
            self.value = value
        self.runtime.mkdir(parents=True, exist_ok=True)
        tmp = self.runtime / ('ha-health.json.tmp.' + str(os.getpid()))
        tmp.write_text(json.dumps(value, ensure_ascii=False) + '\n')
        tmp.chmod(0o600)
        os.replace(tmp, self.runtime/'ha-health.json')
        history = self.runtime/'ha-health-history.jsonl'
        # Keep two bounded generations; do not touch Docker's log files.
        if history.exists() and history.stat().st_size > 8*1024*1024:
            os.replace(history, self.runtime/'ha-health-history.jsonl.1')
        with history.open('a') as handle:
            history.chmod(0o600)
            handle.write(json.dumps(value, ensure_ascii=False, separators=(',', ':'))+'\n')
        return value

    def loop(self):
        with ThreadPoolExecutor(max_workers=min(4, max(1, len(self.nodes))), thread_name_prefix='ha-probe') as pool:
            while not self.stop.is_set():
                try:
                    rows = list(pool.map(probe_node, self.nodes))
                    self.record(rows)
                except Exception as error:
                    with self.lock:
                        self.value = {**self.value, 'collector_error': type(error).__name__}
                self.stop.wait(self.interval)
