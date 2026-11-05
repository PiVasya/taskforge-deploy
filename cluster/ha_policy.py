#!/usr/bin/env python3
"""Verified, leader-only application of a bounded Patroni retry profile.

No direct etcd writes, member changes, promotion, storage/container recreation,
or PostgreSQL restart. PATCH changes only the three retry/lease timers. Existing
replication, failsafe, watchdog and failover-lag policy are preserved.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

TARGET = {'ttl': 120, 'loop_wait': 10, 'retry_timeout': 45}
OLD_DEFAULT = {'ttl': 30, 'loop_wait': 10, 'retry_timeout': 10}
REVISION = 410509
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class PolicyError(RuntimeError):
    pass


def call(url, method='GET', data=None, auth=None, timeout=6):
    headers = {'Accept': 'application/json'}
    body = None
    if data is not None:
        body = json.dumps(data, separators=(',', ':')).encode('utf-8')
        headers.update({'Content-Type': 'application/json', 'Content-Length': str(len(body))})
    if auth:
        headers['Authorization'] = auth
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with HTTP.open(req, timeout=timeout) as response:
            raw = response.read(2*1024*1024)
            return response.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        return error.code, {}
    except (OSError, ValueError):
        return 0, {}


def base(node):
    return f"http://{node['wireguard']['ip']}:{int(node.get('postgres', {}).get('patroni_rest_port', 8008))}"


def timers(data):
    # Patroni defaults are effective if a key was never written to the DCS.
    return {key: int(data.get(key, default)) for key, default in OLD_DEFAULT.items()}


def validate(values):
    if values['loop_wait'] < 1 or values['retry_timeout'] < 3 or values['ttl'] < 20:
        raise PolicyError('invalid Patroni timing values')
    if values['loop_wait'] + 2*values['retry_timeout'] > values['ttl']:
        raise PolicyError('loop_wait + 2*retry_timeout exceeds ttl')


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp.' + str(os.getpid()))
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False)+'\n')
    tmp.chmod(0o600)
    os.replace(tmp, path)


def read_env(path):
    values = {}
    for line in Path(path).read_text().splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            key, value = line.split('=', 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class Policy:
    def __init__(self, cfg, node_id, runtime, transport=call, sleep=time.sleep):
        self.cfg, self.node_id, self.runtime = cfg, str(node_id), Path(runtime)
        self.request, self.sleep = transport, sleep
        self.nodes = cfg['nodes']
        self.local = next(node for node in self.nodes if str(node['id']) == self.node_id)
        self.urls = {str(node['id']): base(node) for node in self.nodes}

    def get(self, url):
        code, data = self.request(url)
        if code != 200 or not isinstance(data, dict):
            raise PolicyError('read failed: '+url+' http='+str(code))
        return data

    def primary(self):
        member = self.get(base(self.local)+'/patroni')
        return member.get('role') in ('primary', 'master', 'leader') and member.get('state') == 'running'

    def proof(self):
        def inspect(node):
            ident = str(node['id'])
            b = base(node)
            member = self.get(b+'/patroni')
            identity = (member.get('patroni') or {}).get('name')
            if identity is not None and str(identity) != ident:
                raise PolicyError('wrong Patroni member identity: '+ident)
            if member.get('state') != 'running':
                raise PolicyError('PostgreSQL is not running on '+ident)
            role = str(member.get('role') or '')
            if role not in ('primary', 'master', 'leader', 'replica'):
                raise PolicyError('unexpected PostgreSQL role on '+ident+': '+role)
            cluster = self.get(b+'/cluster')
            leaders = [str(m.get('name')) for m in cluster.get('members', []) if m.get('role') == 'leader']
            if leaders != [self.node_id]:
                raise PolicyError('inconsistent leader view on '+ident+': '+str(leaders))
            if {str(m.get('name')) for m in cluster.get('members', [])} != set(self.urls):
                raise PolicyError('cluster membership is incomplete on '+ident)
            ip = node['wireguard']['ip']
            agent = self.get(f"http://{ip}:{int(node.get('health_port', 9187))}/ha/live")
            if (str(agent.get('node')) != ident or agent.get('ok') is not True or
                    agent.get('reconciled') is not True or int(agent.get('revision') or 0) < REVISION):
                raise PolicyError('finish standby migrations before timing policy: '+ident)
            if node.get('dcs_voter', True):
                health = self.get(f"http://{ip}:{int(node.get('etcd', {}).get('client_port', 2379))}/health")
                if health.get('health') not in (True, 'true'):
                    raise PolicyError('etcd endpoint not healthy on '+ident)
            return ident, role
        with ThreadPoolExecutor(max_workers=min(4, len(self.nodes))) as pool:
            roles = dict(pool.map(inspect, self.nodes))
        primaries = [ident for ident, role in roles.items() if role in ('primary', 'master', 'leader')]
        if primaries != [self.node_id]:
            raise PolicyError('unique local primary not confirmed: '+str(primaries))
        code, _ = self.request(base(self.local)+'/primary')
        if code != 200:
            raise PolicyError('local Patroni does not confirm leader lock')
        return roles

    def status(self):
        code, data = self.request(base(self.local)+'/config')
        if code != 200:
            raise PolicyError('cannot read effective Patroni config: http='+str(code))
        value = timers(data)
        validate(value)
        report = {'node': self.node_id, 'effective': value, 'target': TARGET,
                  'matches_target': value == TARGET, 'failsafe_mode': data.get('failsafe_mode', False),
                  'note': 'Read from DCS, not bootstrap YAML. Lease expiry is not a guaranteed recovery time.'}
        print(json.dumps(report, indent=2))
        return report

    def apply(self, env, explicit=False, restore=False):
        target=dict(TARGET)
        backup=self.runtime/'ha-timing-before-v41.5.9.json'
        if restore:
            if not backup.is_file():raise PolicyError('no saved timing profile on this node')
            target=json.loads(backup.read_text())['before']
            if set(target)!=set(TARGET):raise PolicyError('invalid timing backup')
        validate(target)
        if not self.primary():
            if restore:raise PolicyError('rollback must run on the current Primary')
            print('HA_POLICY: STANDBY; local control installed; cluster timers apply on current Primary last.')
            return 'standby'
        before = self.get(base(self.local)+'/config')
        current = timers(before)
        validate(current)
        if current == target:
            print('HA_POLICY: already applied '+json.dumps(target,sort_keys=True))
            return 'unchanged'
        if restore and current!=TARGET:
            raise PolicyError('current timing changed since release; refusing blind rollback')
        if current != OLD_DEFAULT and not explicit and not restore:
            raise PolicyError('custom timing policy preserved; inspect cluster.sh ha policy status, then explicitly apply if intended')
        # Two complete independent health observations before a cluster-wide PATCH.
        self.proof()
        self.sleep(3)
        self.proof()
        # Refuse to overwrite an intervening administrative timer change.
        if timers(self.get(base(self.local)+'/config')) != current:
            raise PolicyError('configuration changed during preflight; retry after inspection')
        key = env.get('TASKFORGE_INTERNAL_KEY', '')
        if len(key) < 40:
            raise PolicyError('TASKFORGE_INTERNAL_KEY missing/short')
        password = hmac.new(key.encode(), b'taskforge-patroni-rest-v2', hashlib.sha512).hexdigest()[:64]
        auth = 'Basic ' + base64.b64encode(('taskforge_patroni:'+password).encode()).decode()
        report = {'at': datetime.now(timezone.utc).isoformat(), 'node': self.node_id,
                  'before': current, 'after': target, 'status': 'patch-requested'}
        backup = self.runtime/'ha-timing-before-v41.5.9.json'
        if not backup.exists():
            atomic(backup, report)
        atomic(self.runtime/'ha-timing-apply.json', report)
        # An uncertain HTTP result is NOT blindly retried; verify effective state.
        code, _ = self.request(base(self.local)+'/config', method='PATCH', data=target, auth=auth, timeout=12)
        deadline = time.monotonic()+60
        while True:
            observed = {}
            for ident, url in self.urls.items():
                response, config = self.request(url+'/config')
                observed[ident] = timers(config) if response == 200 else None
            if all(row == target for row in observed.values()):
                report.update(status='rollback-verified' if restore else 'verified', response_http=code, observed=observed)
                atomic(self.runtime/'ha-timing-apply.json', report)
                print('HA_POLICY: VERIFIED on all nodes '+json.dumps(target,sort_keys=True)+'; no PostgreSQL restart')
                return 'applied'
            if time.monotonic() >= deadline:
                report.update(status='verification-incomplete', response_http=code, observed=observed)
                atomic(self.runtime/'ha-timing-apply.json', report)
                raise PolicyError('timing PATCH verification incomplete; no retry sent; inspect cluster.sh ha policy status')
            self.sleep(2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['status', 'migrate', 'apply', 'rollback'])
    parser.add_argument('--config', default='/etc/taskforge/cluster.json')
    parser.add_argument('--runtime', default='/var/lib/taskforge/cluster')
    parser.add_argument('--env-file', default='/etc/taskforge/taskforge.env')
    args = parser.parse_args()
    if args.action != 'status' and os.geteuid() != 0:
        parser.error('mutating timing policy requires sudo')
    try:
        runtime = Path(args.runtime)
        policy = Policy(json.loads(Path(args.config).read_text()), (runtime/'node-id').read_text().strip(), runtime)
        if args.action == 'status':
            policy.status()
        else:
            policy.apply(read_env(args.env_file), explicit=args.action=='apply', restore=args.action=='rollback')
        return 0
    except (OSError, ValueError, KeyError, PolicyError, StopIteration) as error:
        print('HA_POLICY: NOT APPLIED/NOT VERIFIED: '+str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
