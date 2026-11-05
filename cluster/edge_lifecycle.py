#!/usr/bin/env python3
"""Ordered Primary -> DNS -> public route -> local Certbot -> HTTPS lifecycle.

Runs beside the Agent reconciliation loop, not inside its HTTP handlers. No
storage re-home, leader election, peer certificate copying or TLS downgrade is
performed here. All external operations are bounded and leadership-guarded.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

import cloudflare_dns as cf
from edge_route import PublicRoute, normalize_hosts


class Pending(RuntimeError):
    """Not ready; leave traffic readiness false and retry without re-election."""


class Cancelled(Pending):
    pass


@dataclass(frozen=True)
class Policy:
    settle: int = 15
    route_timeout: int = 180
    interval: int = 5
    confirmations: int = 3
    certbot_timeout: int = 240
    tls_timeout: int = 60
    recheck: int = 60
    retry: int = 60
    cert_retry: int = 900
    renew_check: int = 21600

    @classmethod
    def from_env(cls, env: dict[str, str]) -> 'Policy':
        ranges = {
            'settle': ('TASKFORGE_EDGE_SETTLE_SECONDS', 1, 300),
            'route_timeout': ('TASKFORGE_ACME_ROUTE_WAIT_SECONDS', 15, 600),
            'interval': ('TASKFORGE_EDGE_PROBE_INTERVAL_SECONDS', 2, 30),
            'confirmations': ('TASKFORGE_EDGE_ROUTE_CONFIRMATIONS', 2, 10),
            'certbot_timeout': ('TASKFORGE_CERTBOT_TIMEOUT_SECONDS', 30, 600),
        }
        values = {}
        for field, (key, low, high) in ranges.items():
            if not env.get(key, '').strip():
                continue
            try:
                value = int(env[key])
            except (ValueError, TypeError) as exc:
                raise Pending(f'{key} must be an integer') from exc
            if not low <= value <= high:
                raise Pending(f'{key} must be in {low}..{high}')
            values[field] = value
        policy = cls(**values)
        if policy.route_timeout < (policy.confirmations - 1) * policy.interval + 10:
            raise Pending('ACME route timeout is shorter than the confirmation window')
        return policy


class SystemOps:
    def __init__(self, root: Path, config: Path, env_file: Path, runtime: Path,
                 cancel: threading.Event, critical: list[str] | None = None):
        self.root, self.config_path, self.env_path, self.runtime = root, config, env_file, runtime
        self.cancel = cancel
        self.cluster = cf.load_json(config)
        self.env = cf.load_env(env_file)
        self.config_root = config.parent
        self.node = (runtime / 'node-id').read_text().strip()
        self.node_config = next(n for n in self.cluster['nodes'] if str(n['id']) == self.node)
        self.project = self.env.get('COMPOSE_PROJECT_NAME', 'taskforge-prod')
        self.gateway = self.project + '-gateway-1'
        self.cf_config = self.config_root / 'cloudflare-failover.json'
        self.token_path = self.config_root / 'cloudflare-api-token'
        self.configured = self.cf_config.is_file() and self.token_path.is_file()
        if any(p.exists() and not p.is_file() for p in (self.cf_config, self.token_path)):
            raise Pending('Cloudflare token/config must be regular files, not directories')
        if self.cf_config.exists() != self.token_path.exists():
            raise Pending('partial Cloudflare configuration: both token and config are required; rerun cloudflare setup')
        self.cf_args = SimpleNamespace(config=str(config), env_file=str(env_file), runtime=str(runtime),
                                      cf_config=str(self.cf_config), token_path=str(self.token_path),
                                      node=self.node, reason='edge-lifecycle')
        self.token = self.token_path.read_text().strip() if self.configured else ''
        if self.configured and not self.token:
            raise Pending('Cloudflare token is empty')
        self.cf_document = cf.load_json(self.cf_config) if self.configured else {}
        domains = normalize_hosts([self.env.get('DOMAIN', ''), self.env.get('CT_DOMAIN', '')])
        managed = normalize_hosts(self.cf_document.get('managed_hosts') or domains)
        if self.configured and not set(domains).issubset(set(managed)):
            raise Pending('Cloudflare setup must include DOMAIN and CT_DOMAIN before certificate bootstrap')
        self.domains = domains
        self.hosts = normalize_hosts(domains + managed)
        if critical is None:
            # Shared with the installed Agent; no second, drifting service list.
            from agent import APP_FULL, APP_LITE, OPTIONAL_SERVICES
            app = self.node_config.get('app') or {}
            base = APP_FULL if app.get('profile', 'full') == 'full' else APP_LITE
            critical = [s for s in base if s not in OPTIONAL_SERVICES and s not in app.get('exclude_services', [])]
        self.critical = critical
        self.policy = Policy.from_env(self.env)
        self.term = None
        self.http_only = str((self.node_config.get('web') or {}).get('tls_mode', 'auto')).lower() in {'http', 'off', 'disabled', 'none'}
        self.process_env = os.environ.copy()
        self.process_env.update(TASKFORGE_CONTROL_ROOT=str(root), TASKFORGE_CONFIG_ROOT=str(config.parent),
                                TASKFORGE_CLUSTER_CONFIG=str(config), TASKFORGE_ENV_FILE=str(env_file),
                                TASKFORGE_CLUSTER_RUNTIME=str(runtime))
        if (self.config_root / 'docker/config.json').is_file():
            self.process_env['DOCKER_CONFIG'] = str(self.config_root / 'docker')
        self.watched = [config, env_file, runtime / 'node-id', self.cf_config, self.token_path]
        self.fingerprint = self._fingerprint()
        self._route = None
        self.ledger_path = runtime / 'edge-certbot-attempt.json'
        self.ledger = self._read_ledger()

    def _fingerprint(self):
        return tuple(hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None for p in self.watched)

    def guard(self):
        if self.cancel.is_set():
            raise Cancelled('edge lifecycle cancelled')
        if not (self.runtime / 'enabled').is_file():
            raise Cancelled('quorum runtime is not enabled')
        if any((self.runtime / name).exists() for name in ('join-rehome-fence', 'migration-fence')):
            raise Cancelled('migration fence is armed')
        if self._fingerprint() != self.fingerprint:
            raise Cancelled('configuration changed; restart proof with the new configuration')
        if not (self.node_config.get('app') or {}).get('can_be_primary', True):
            raise Cancelled('node cannot be Primary')
        try:
            primary = cf.exact_one_leader(self.cluster, self.node)
        except cf.CFError as exc:
            raise Cancelled(str(exc)) from exc
        timeline = primary.get('timeline')
        if timeline is None:
            raise Cancelled('Patroni timeline missing; cannot bind edge work to a Primary term')
        if self.term is None:
            self.term = timeline
        elif self.term != timeline:
            raise Cancelled('Patroni timeline changed; discard the previous route proof')

    @contextlib.contextmanager
    def exclusive(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / 'edge-lifecycle.lock').open('a') as handle:
            os.fchmod(handle.fileno(), 0o600)
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Pending('another local edge/Certbot lifecycle is already running') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def pause(self, seconds):
        deadline = time.monotonic() + max(0, seconds)
        while time.monotonic() < deadline:
            self.guard()
            if self.cancel.wait(min(2, max(0, deadline - time.monotonic()))):
                raise Cancelled('edge lifecycle cancelled')
        self.guard()

    @staticmethod
    def _terminate(proc):
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait(timeout=3)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                pass

    def command(self, args, timeout=10):
        self.guard()
        # Output to temporary files avoids a full pipe deadlock and unbounded
        # in-memory Docker/Certbot output. Only a bounded tail is returned.
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            proc = subprocess.Popen(args, stdout=out, stderr=err, env=self.process_env,
                                    start_new_session=True)
            deadline = time.monotonic() + timeout
            next_guard = time.monotonic() + 2
            try:
                while proc.poll() is None:
                    if self.cancel.is_set():
                        raise Cancelled('external command cancelled')
                    now = time.monotonic()
                    if now >= deadline:
                        raise Pending(f'command timeout: {Path(str(args[0])).name} after {timeout}s')
                    if now >= next_guard:
                        self.guard()
                        next_guard = time.monotonic() + 2
                    self.cancel.wait(min(0.25, max(0, deadline - time.monotonic())))
                self.guard()
            except BaseException:
                self._terminate(proc)
                raise
            def tail(handle):
                handle.seek(0, os.SEEK_END)
                end = handle.tell()
                handle.seek(max(0, end - 65536))
                return handle.read().decode('utf-8', 'replace')
            return subprocess.CompletedProcess(args, proc.returncode, tail(out), tail(err))

    def core_ready(self):
        names = [f'{self.project}-{svc}-1' for svc in self.critical]
        fmt = '{{.Name}}|{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}'
        result = self.command(['docker', 'inspect', '-f', fmt, *names], timeout=10)
        if result.returncode:
            raise Pending('traffic-critical containers are unavailable')
        rows = [line.split('|') for line in result.stdout.splitlines() if line.strip()]
        if len(rows) != len(names) or {row[0].lstrip('/') for row in rows} != set(names):
            raise Pending('traffic-critical container inventory is incomplete')
        bad = [row[0] for row in rows if len(row) != 3 or row[1] != 'running' or row[2] not in {'none', 'healthy'}]
        if bad:
            raise Pending('traffic-critical containers not healthy: ' + ','.join(bad))

    def reconcile(self):
        self.guard()
        if not self.configured:
            return {'success': True, 'configured': False, 'changed': [], 'target_node': self.node}
        return cf.reconcile(self.cf_args, guard=self.guard)

    def route_probe(self, scheme='http'):
        if self._route is None:
            result = self.command(['bash', str(self.root / 'cluster/ops/current/local-cert.sh'), 'webroot-path'], timeout=10)
            if result.returncode or not result.stdout.strip():
                raise Pending('existing local certbot-webroot volume was not found')
            self._route = PublicRoute(Path(result.stdout.strip()), self.hosts, self.node,
                                      self.configured, self.command)
        return self._route.probe(scheme, timeout=5)

    def certificate(self):
        result = self.command(['bash', str(self.root / 'cluster/ops/current/local-cert.sh'), 'status'], timeout=15)
        if result.returncode:
            return None
        try:
            cert = json.loads(result.stdout)
            return cert if cert.get('fingerprint') and cert.get('not_after_epoch', 0) > time.time() else None
        except (ValueError, TypeError):
            return None

    def _read_ledger(self):
        try:
            doc = json.loads(self.ledger_path.read_text())
            return doc if doc.get('domains') == self.domains else {}
        except (FileNotFoundError, ValueError):
            return {}

    def _save_ledger(self):
        self.ledger['domains'] = self.domains
        cf.atomic_secret(self.ledger_path, json.dumps(self.ledger) + '\n')

    def cert_due(self, cert):
        if cert and cert['not_after_epoch'] > time.time() + 30 * 86400:
            return False
        # A failed ACME attempt is not retried every Agent tick or on restart.
        if not cert:
            return time.time() >= float(self.ledger.get('last_attempt_epoch', 0)) + self.policy.cert_retry
        return time.time() >= float(self.ledger.get('next_certbot_epoch', 0))

    def cert_wait(self):
        return max(0, int(float(self.ledger.get('last_attempt_epoch', 0)) + self.policy.cert_retry - time.time()))

    def certbot(self):
        self.guard()
        self.core_ready()
        self.ledger['last_attempt_epoch'] = time.time()
        self.ledger['next_certbot_epoch'] = time.time() + self.policy.cert_retry
        self._save_ledger()
        name = f'{self.project}-certbot-edge'
        # Recover only our labelled one-shot container left by a killed Agent.
        result = self.command(['docker', 'inspect', name], timeout=10)
        if result.returncode == 0:
            rows = json.loads(result.stdout)
            labels = (rows[0].get('Config') or {}).get('Labels') or {}
            if labels.get('by.taskforge.edge-certbot') != 'true':
                raise Pending(f'{name} exists without the ownership label; refusing to remove it')
            removed = self.command(['docker', 'rm', '-f', name], timeout=15)
            if removed.returncode:
                raise Pending('cannot retire the previous managed Certbot container')
        argv = ['bash', str(self.root / 'compose.sh'), '--profile', 'certbot', 'run',
                '--rm', '--no-deps', '--name', name, '--label', 'by.taskforge.edge-certbot=true', 'certbot']
        try:
            result = self.command(argv, timeout=self.policy.certbot_timeout)
            if result.returncode:
                message = (result.stderr or result.stdout)[-1800:]
                raise Pending('Certbot failed; retry is throttled: ' + message)
            cert = self.certificate()
            if not cert:
                raise Pending('Certbot exited successfully but no valid matching local certificate is present')
            self.ledger['next_certbot_epoch'] = time.time() + self.policy.renew_check
            self._save_ledger()
            return cert
        finally:
            # Deleting this ephemeral, labelled container never uses -v, never
            # deletes a volume, and is allowed even after demotion (cancellation).
            try:
                subprocess.run(['docker', 'rm', '-f', name], timeout=15,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=self.process_env)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def local_tls(self):
        port = int(self.env.get('PUBLIC_HTTPS_PORT', '443') or 443)
        for domain in self.domains:
            result = self.command(['curl', '-q', '-fsS', '--noproxy', '*', '--connect-timeout', '2',
                                   '--max-time', '4', '--resolve', f'{domain}:{port}:127.0.0.1',
                                   f'https://{domain}:{port}/health/gateway'], timeout=6)
            if result.returncode:
                return False
        return True

    def restart_gateway(self):
        self.guard()
        if time.time() < float(self.ledger.get('gateway_restart_after_epoch', 0)):
            raise Pending('gateway HTTPS still unavailable; restart is throttled for 5 minutes')
        self.ledger['gateway_restart_after_epoch'] = time.time() + 300
        self._save_ledger()
        result = self.command(['docker', 'restart', '-t', '20', self.gateway], timeout=40)
        if result.returncode:
            raise Pending('gateway restart after certificate installation failed')


class Runner:
    """Deterministic lifecycle ordering; external I/O is injected via ops."""
    def __init__(self, ops, publish: Callable[[dict], None], clock=time.monotonic):
        self.ops, self.publish, self.clock = ops, publish, clock
        self.policy = ops.policy

    def phase(self, name, **extra):
        self.publish({'phase': name, **extra})

    def prove(self, scheme, timeout, confirmations):
        started = self.clock()
        good = 0
        last = {}
        while self.clock() - started < timeout:
            self.ops.guard()
            last = self.ops.route_probe(scheme)
            if last.get('ok'):
                good += 1
            else:
                good = 0
                self.publish({'invalidate': True, 'route_ready': False})
            self.phase('route-check' if scheme == 'http' else 'public-https-check',
                       route_confirmations=good, route_required=confirmations,
                       elapsed_seconds=int(self.clock() - started), wait_limit_seconds=timeout,
                       route_checks=last.get('hosts', []))
            if good >= confirmations:
                self.ops.guard()
                return
            remaining = timeout - (self.clock() - started)
            if remaining > 0:
                self.ops.pause(min(self.policy.interval, remaining))
        detail = '; '.join(f'{x.get("host")}: {x.get("reason") or x.get("http")}' for x in last.get('hosts', []))
        raise Pending(f'{scheme.upper()} public route not confirmed within {timeout}s; {detail}. No ready state was granted')

    def run(self):
        o = self.ops
        with o.exclusive():
            o.guard()
            o.core_ready()
            self.phase('dns-update', configured=o.configured, target_node=o.node)
            result = o.reconcile()
            if not result.get('success'):
                raise Pending('DNS reconciliation failed')
            self.phase('dns-synced' if o.configured else 'static-route',
                       dns_synced=o.configured, changed=result.get('changed', []),
                       target_ip=result.get('target_ip', ''), invalidate=bool(result.get('changed')))
            if o.configured:
                self.phase('edge-settle', wait_limit_seconds=self.policy.settle)
                # Deliberate minimum settling period, followed by evidence, not
                # a blind assertion that all Cloudflare edges have converged.
                o.pause(self.policy.settle)
            self.prove('http', self.policy.route_timeout, self.policy.confirmations)
            self.phase('route-confirmed', route_ready=True)
            o.guard()
            o.core_ready()
            warning = ''
            if not o.http_only:
                before = o.certificate()
                cert = before
                if o.cert_due(before):
                    self.phase('certbot-running', certificate_action='ensure-local', invalidate=not bool(before))
                    try:
                        cert = o.certbot()
                    except Cancelled:
                        raise
                    except Pending as exc:
                        # Renewal failure is visible, but need not take down a
                        # site that still has a valid, matching local certificate.
                        cert = o.certificate()
                        if not cert:
                            raise
                        warning = str(exc)
                elif not before:
                    raise Pending(f'certificate absent; ACME retry cooldown remaining={o.cert_wait()}s')
                else:
                    self.phase('certificate-reused', certificate_action='existing-valid-certificate')
                if not cert:
                    raise Pending('matching local certificate missing')
                changed = not before or cert['fingerprint'] != before['fingerprint']
                self.phase('local-https-check')
                if changed or not o.local_tls():
                    self.phase('gateway-reload')
                    o.restart_gateway()
                start = self.clock()
                while not o.local_tls():
                    if self.clock() - start >= self.policy.tls_timeout:
                        raise Pending('local HTTPS certificate/health check did not pass')
                    o.pause(2)
                self.prove('https', self.policy.tls_timeout, 1)
            o.guard()
            o.core_ready()
            receipt = {'success': True, 'configured': o.configured,
                       'status': 'synced' if o.configured else 'unconfigured',
                       'phase': 'ready', 'dns_synced': o.configured, 'route_ready': True,
                       'tls_ready': not o.http_only, 'http_only': o.http_only,
                       'target_node': o.node, 'target_ip': result.get('target_ip', ''),
                       'changed': result.get('changed', []), 'last_error': '', 'warning': warning,
                       'verified_at': cf.utc_now()}
            self.publish(receipt)
            return receipt


class Supervisor:
    """One cancellable worker per node; telemetry snapshots never block on I/O."""
    def __init__(self, root: Path, config: Path, env_file: Path, runtime: Path, critical: list[str],
                 factory=SystemOps, clock=time.monotonic):
        self.root, self.config, self.env_file, self.runtime = root, config, env_file, runtime
        self.critical, self.factory, self.clock = critical, factory, clock
        self.lock = threading.Lock()
        self.thread = None
        self.cancel = threading.Event()
        self.eligible = False
        self.generation = 0
        self.next_run = 0.0
        self.ready_at = None
        self.state = {'success': False, 'status': 'standby-no-write', 'phase': 'standby',
                      'route_ready': False, 'tls_ready': False, 'dns_synced': False}

    def _revoke(self):
        self.ready_at = None
        self.state['success'] = False
        self.state['route_ready'] = False
        self.state['tls_ready'] = False
        try:
            (self.runtime / 'readiness/traffic-ready').unlink(missing_ok=True)
        except OSError:
            pass

    def update(self, eligible: bool):
        with self.lock:
            self.eligible = eligible
            if not eligible:
                self.cancel.set()
                self._revoke()
                self.state.update(status='standby-no-write', phase='standby', dns_synced=False)
                self.next_run = 0
            elif (self.thread is None or not self.thread.is_alive()) and self.clock() >= self.next_run:
                self.cancel = threading.Event()
                self.generation += 1
                generation = self.generation
                if self.ready_at is None:
                    self.state.update(phase='starting', status='pending', success=False, last_error='')
                self.thread = threading.Thread(target=self._run, args=(generation, self.cancel),
                                               name='taskforge-edge', daemon=True)
                self.thread.start()
            return self._snapshot()

    def _snapshot(self):
        doc = dict(self.state)
        # Do not advertise an indefinitely stale proof if the worker is stuck.
        if not self.eligible or self.ready_at is None or self.clock() - self.ready_at > 180:
            doc['success'] = False
            if doc.get('status') in {'synced', 'unconfigured'} and doc.get('phase') != 'standby':
                doc['status'] = 'pending'
        if doc.get('success'):
            doc['status'] = 'synced' if doc.get('configured') else 'unconfigured'
        doc['retry_in_seconds'] = max(0, int(self.next_run - self.clock()))
        return doc

    def snapshot(self):
        with self.lock:
            return self._snapshot()

    def _publish(self, generation, cancel, patch):
        with self.lock:
            if generation != self.generation or cancel.is_set() or not self.eligible:
                return
            patch = dict(patch)
            if patch.pop('invalidate', False):
                self._revoke()
            self.state.update(patch)
            if patch.get('success') is True:
                self.ready_at = self.clock()

    def _run(self, generation, cancel):
        retry = 60
        ops = None
        try:
            ops = self.factory(self.root, self.config, self.env_file, self.runtime, cancel, self.critical)
            Runner(ops, lambda patch: self._publish(generation, cancel, patch)).run()
            retry = ops.policy.recheck
        except Exception as exc:
            error = str(exc)
            if ops is not None and ops.token:
                error = error.replace(ops.token, '<redacted>')
            self._publish(generation, cancel, {'invalidate': True, 'success': False, 'status': 'pending',
                                             'phase': 'cancelled' if isinstance(exc, Cancelled) else 'retry-wait',
                                             'last_error': error[-2000:]})
            if isinstance(exc, Cancelled):
                retry = 5
        finally:
            with self.lock:
                if generation == self.generation:
                    self.next_run = self.clock() + retry if self.eligible and not cancel.is_set() else 0

    def close(self):
        self.update(False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['once', 'route-check'])
    parser.add_argument('--config', default=os.environ.get('TASKFORGE_CLUSTER_CONFIG', '/etc/taskforge/cluster.json'))
    parser.add_argument('--env-file', default=os.environ.get('TASKFORGE_ENV_FILE', '/etc/taskforge/taskforge.env'))
    parser.add_argument('--runtime', default=os.environ.get('TASKFORGE_CLUSTER_RUNTIME', '/var/lib/taskforge/cluster'))
    parser.add_argument('--wait', type=int)
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('run with sudo/root')
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    ops = None
    try:
        ops = SystemOps(Path(__file__).resolve().parents[1], Path(args.config), Path(args.env_file), Path(args.runtime), stop)
        def progress(patch):
            if 'phase' in patch:
                print(json.dumps(patch, ensure_ascii=False), flush=True)
        runner = Runner(ops, progress)
        if args.mode == 'route-check':
            with ops.exclusive():
                runner.prove('http', args.wait or ops.policy.route_timeout, ops.policy.confirmations)
        else:
            runner.run()
        return 0
    except Exception as exc:
        text = str(exc).replace(ops.token, '<redacted>') if ops is not None and ops.token else str(exc)
        print('error: ' + text, file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
