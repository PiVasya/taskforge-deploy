"""Application image reconciliation inside the existing Node Agent.

Registry polling is independent of readiness. One local lifecycle writer, bounded
commands, immutable image IDs, per-service retries and persistent incidents.
Database engines and persistent infrastructure are NOT floating application tags.
"""
from __future__ import annotations
import hashlib
import fcntl
import json
import os
import signal
import stat
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

class MutationDeferred(RuntimeError):
    """Authority changed; retry from fresh HA state without failure backoff."""


DATA_SERVICES = {'postgres', 'etcd', 'redis', 'rabbitmq', 'minio', 'sql-postgres', 'sql-mysql'}


def utc(epoch=None):
    return datetime.fromtimestamp(time.time() if epoch is None else epoch, timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp.' + uuid.uuid4().hex)
    try:
        with tmp.open('x') as handle:
            os.chmod(tmp, 0o600)
            json.dump(value, handle, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def bounded_run(args, env=None, timeout=60, limit=4 * 1024 * 1024):
    """Drain continuously, keep bounded tails; kill the whole CLI process group."""
    proc = None
    output = [bytearray(), bytearray()]
    def consume(stream, index):
        try:
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                output[index].extend(chunk)
                if len(output[index]) > limit:
                    del output[index][:-limit]
        finally:
            stream.close()
    try:
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, start_new_session=True)
        readers = [threading.Thread(target=consume, args=(stream, index), daemon=True)
                   for index, stream in enumerate((proc.stdout, proc.stderr))]
        for thread in readers:
            thread.start()
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=5)
            code = 124
        for thread in readers:
            thread.join(timeout=2)
        return subprocess.CompletedProcess(args, code, output[0].decode('utf-8', 'replace'), output[1].decode('utf-8', 'replace'))
    except Exception as error:
        if proc is not None and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return subprocess.CompletedProcess(args, 125, '', type(error).__name__)


class DockerApplications:
    def __init__(self, root, runtime, project, env):
        self.root, self.runtime, self.project, self.env = Path(root), Path(runtime), project, dict(env)
        self.last_retirement_error = ''
        self.last_watchtower_state = 'unknown'
        self._references_cache = {}
        self._references_cache_key = None
        self._references_cache_at = 0.0
        self._compose_config_calls = 0
        self._compose_prepare_calls = 0
        self._compose_force_recreate_calls = 0
        self._compose_reference_cache_hits = 0
        self._inspect_fallback_calls = 0
        self._inspect_fallback_successes = 0
        self._inspect_fallback_missing = 0
        self.last_observe_error = ''
        self.last_observe_fallback_reason = ''

    def cli(self, *args, timeout=60):
        return bounded_run(['docker', *args], self.env, timeout)

    def compose(self, *args, timeout=120, override=None):
        env = dict(self.env)
        if override is not None:
            env['TASKFORGE_COMPOSE_OVERRIDE'] = str(override)
        return bounded_run(['bash', str(self.root / 'compose.sh'), *args], env, timeout, 16 * 1024 * 1024)

    def _reference_config_key(self):
        # All files consulted by compose.sh for a rendered image reference.
        # mtime + size avoids rerendering unchanged Compose for every image batch,
        # but invalidates immediately on routing/config changes or a new release.
        paths = [self.root / 'compose.sh', self.root / 'cluster/compose.cluster.yaml',
                 self.root / 'cluster/compose.telemetry.yaml',
                 self.runtime / 'app-routing.env', self.runtime / 'node.env',
                 self.runtime / 'storage-bindings.yaml',
                 Path(self.env.get('TASKFORGE_ENV_FILE') or '/etc/taskforge/taskforge.env')]
        config_root = Path(self.env.get('TASKFORGE_CONFIG_ROOT') or '/etc/taskforge')
        paths += [config_root / f for f in ('sql-runtime.env', 'local.env', 'cpu.env')]
        paths += sorted((self.root / 'compose').glob('*.yaml'))
        paths += sorted((self.root / 'compose').glob('*.yml'))
        result = []
        for path in paths:
            try:
                st = path.stat()
                result.append((str(path), st.st_mtime_ns, st.st_size))
            except FileNotFoundError:
                result.append((str(path), None, None))
        return tuple(result)

    def references(self, assigned):
        now = time.monotonic()
        key = self._reference_config_key()
        if (self._references_cache_key == key and
                now - self._references_cache_at < 90):
            self._compose_reference_cache_hits += 1
            refs = self._references_cache
        else:
            self._compose_config_calls += 1
            result = self.compose('config', '--format', 'json', timeout=45)
            if result.returncode:
                raise RuntimeError('compose-config-failed')
            services = json.loads(result.stdout).get('services', {})
            refs = {name: str(value.get('image') or '')
                    for name, value in services.items() if isinstance(value, dict)}
            # Cache only after successful parsing; failing config is retried.
            self._references_cache = refs
            self._references_cache_key = key
            self._references_cache_at = now
        return {name: refs[name] for name in assigned if name in refs}

    def observe_counters(self):
        return {'fallback_calls': self._inspect_fallback_calls,
                'fallback_successes': self._inspect_fallback_successes,
                'missing_confirmed': self._inspect_fallback_missing,
                'last_error': self.last_observe_error,
                'last_fallback_reason': self.last_observe_fallback_reason}

    def compose_counters(self):
        return {'config': self._compose_config_calls,
                'prepare': self._compose_prepare_calls,
                'force_recreate': self._compose_force_recreate_calls,
                'reference_cache_hits': self._compose_reference_cache_hits}

    @staticmethod
    def _docker_message(result):
        # Docker diagnostics must be actionable but never dump large logs or
        # registry authentication material into the dashboard.
        raw = str(result.stderr or result.stdout or '').strip().splitlines()
        return (raw[-1] if raw else 'no-cli-output').replace('\r', ' ').replace('\t', ' ')[:160]

    def _fallback_observe(self, services, rows, reason):
        """Resolve missing/stopped containers independent of CLI error wording.

        `docker inspect -f` can fail as a batch when ANY referenced Compose
        container is absent, or because a Docker version rejects a Go template.
        Neither is evidence that Docker itself is down. `docker ps -a` includes
        stopped/created containers. For those that genuinely exist, read the raw
        inspect JSON instead of attempting the same broken template again.
        The command NEVER starts, stops, prunes or modifies containers.
        """
        self._inspect_fallback_calls += 1
        self.last_observe_fallback_reason = reason
        known = {f'{self.project}-{s}-1': s for s in services}
        listing = self.cli('ps', '-a', '--format', '{{.Names}}|{{.ID}}', timeout=20)
        if listing.returncode:
            self.last_observe_error = ('docker-list-failed:' + str(listing.returncode) + ':' +
                                       self._docker_message(listing))
            raise RuntimeError(self.last_observe_error)
        present = {}
        for line in listing.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.strip().split('|', 1)
            if len(parts) != 2:
                self.last_observe_error = 'docker-list-invalid-response'
                raise RuntimeError(self.last_observe_error)
            name, ident = parts
            if name in known and ident:
                present[name] = ident
        self._inspect_fallback_missing += len(services) - len(present)
        if not present:
            self.last_observe_error = ''
            self._inspect_fallback_successes += 1
            return rows

        # Raw JSON does not depend on the formatted `inspect` template, and
        # short IDs are resolved only from the project-qualified exact names.
        details = self.cli('container', 'inspect', *present.values(), timeout=25)
        if details.returncode:
            self.last_observe_error = ('docker-json-inspect-failed:' + str(details.returncode) + ':' +
                                       self._docker_message(details))
            raise RuntimeError(self.last_observe_error)
        try:
            objs = json.loads(details.stdout)
            if not isinstance(objs, list):
                raise ValueError('inspect-json-not-a-list')
            seen = set()
            for obj in objs:
                if not isinstance(obj, dict):
                    raise ValueError('inspect-json-item-not-object')
                name = str(obj.get('Name') or '').lstrip('/')
                if name not in present or name in seen:
                    raise ValueError('inspect-json-unexpected-container')
                if not str(obj.get('Id') or '').startswith(str(present[name])):
                    raise ValueError('inspect-json-id-mismatch')
                state = obj.get('State') or {}
                health = state.get('Health') or {}
                service = known[name]
                rows[service] = {
                    'state': str(state.get('Status') or 'unknown'),
                    'health': str(health.get('Status') or 'none'),
                    'image': str(obj.get('Image') or ''),
                    'id': str(obj.get('Id') or ''),
                    'restarts': int(obj.get('RestartCount') or 0),
                    'started_at': str(state.get('StartedAt') or ''),
                    'exit_code': int(state.get('ExitCode') or 0),
                }
                seen.add(name)
            if seen != set(present):
                raise ValueError('inspect-json-incomplete')
        except (ValueError, TypeError, KeyError) as error:
            self.last_observe_error = 'docker-json-inspect-invalid:' + type(error).__name__ + ':' + str(error)[:90]
            raise RuntimeError(self.last_observe_error) from error
        self._inspect_fallback_successes += 1
        self.last_observe_error = ''
        return rows

    def observe(self, services):
        names = [f'{self.project}-{s}-1' for s in services]
        rows = {s: {'state': 'missing', 'health': 'missing', 'image': '', 'id': ''} for s in services}
        if not names:
            return rows
        fmt = '{{.Name}}|{{.Id}}|{{.Image}}|{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{.State.StartedAt}}|{{.State.ExitCode}}'
        result = self.cli('inspect', '-f', fmt, *names, timeout=20)
        if result.returncode:
            # Never guess availability from English/localized stderr strings.
            # A stopped/absent Compose container is normal on a Standby.
            return self._fallback_observe(services, rows, self._docker_message(result))
        parsed = set()
        for line in result.stdout.splitlines():
            parts = line.strip().split('|')
            if len(parts) != 8:
                return self._fallback_observe(services, rows, 'formatted-inspect-invalid-row')
            name, cid, image, state, health, restarts, started, exit_code = parts
            name = name.lstrip('/')
            if name not in names or name in parsed:
                return self._fallback_observe(services, rows, 'formatted-inspect-unexpected-name')
            try:
                service = name[len(self.project)+1:-2]
                rows[service] = {'state': state, 'health': health, 'image': image, 'id': cid,
                                 'restarts': int(restarts or 0), 'started_at': started,
                                 'exit_code': int(exit_code or 0)}
            except (ValueError, KeyError):
                return self._fallback_observe(services, rows, 'formatted-inspect-invalid-field')
            parsed.add(name)
        if parsed != set(names):
            # Silent partial output is not proof that the omitted containers
            # are absent. Confirm with a fresh Docker list.
            return self._fallback_observe(services, rows, 'formatted-inspect-incomplete')
        self.last_observe_error = ''
        return rows

    def pull(self, reference):
        # Slow VPS hosts (CPU steal >40%) need a bounded but realistic window.
        # A single slow image is retried independently; it never holds a release lock.
        result = self.cli('pull', '--quiet', reference, timeout=420)
        if result.returncode:
            raise RuntimeError('registry-pull-failed:' + str(result.returncode))
        image = self.cli('image', 'inspect', '-f', '{{.Id}}', reference, timeout=15)
        value = image.stdout.strip()
        if image.returncode or not value.startswith('sha256:'):
            raise RuntimeError('pulled-image-id-unavailable')
        return value

    def preserve(self, service, image):
        # Prevent ordinary dangling-image cleanup from removing a proven rollback.
        tag = f'taskforge-rollback/{self.project}-{service}:last-good'
        result = self.cli('tag', image, tag, timeout=10)
        return result.returncode == 0

    def prepare_guarded(self, service, image, guard):
        return self.prepare(service, image, guard=guard)

    def prepare(self, service, image, guard=None):
        guard = guard or (lambda: True)
        if not guard():raise MutationDeferred("prepare-authority-unconfirmed")
        self._compose_prepare_calls += 1
        # Override just this invocation; do not pin the moving tag in global Compose.
        fd, name = tempfile.mkstemp(prefix='update-image-', suffix='.json', dir=self.runtime)
        try:
            with os.fdopen(fd, 'w') as handle:
                json.dump({'services': {service: {'image': image}}}, handle)
            result = self.compose('up', '--no-start', '--no-deps', '--no-build', '--pull', 'never', service,
                                  timeout=120, override=Path(name))
            if result.returncode:
                raise RuntimeError('container-prepare-failed:' + str(result.returncode))
            # Compose can report no changes because of an inherited/stale
            # service hash even when the image ID does not match. Retry ONCE
            # with --force-recreate, still with --no-start and --no-deps.
            actual = self.observe([service]).get(service, {})
            if actual.get('state') == 'missing' or actual.get('image') != image:
                if not guard():raise MutationDeferred('force-prepare-authority-changed')
                self._compose_force_recreate_calls += 1
                result = self.compose('up', '--no-start', '--no-deps', '--no-build',
                                      '--pull', 'never', '--force-recreate', service,
                                      timeout=120, override=Path(name))
                if result.returncode:
                    raise RuntimeError('container-force-prepare-failed:' + str(result.returncode))
        finally:
            Path(name).unlink(missing_ok=True)

    def start(self, service):
        result = self.cli('start', f'{self.project}-{service}-1', timeout=30)
        if result.returncode:
            raise RuntimeError('container-start-failed:' + str(result.returncode))

    def stop(self, service):
        result = self.cli('stop', '-t', '20', f'{self.project}-{service}-1', timeout=30)
        if result.returncode:
            raise RuntimeError('container-stop-failed:' + str(result.returncode))

    def cleanup(self, image):
        # Never prune globally or force removal. Docker refuses any image still
        # referenced by another running/stopped container or additional tags.
        return self.cli('image', 'rm', image, timeout=15).returncode == 0

    def _watchtower_objects(self):
        """Probe actual Compose-labelled containers, not fragile inspect errors.

        Docker inspect on an absent container returns version/localization-dependent
        error text. An unrelated Docker error used to be misreported as an active
        legacy updater, permanently fencing GHCR polling after Watchtower removal.
        """
        expected = f'{self.project}-watchtower-1'
        queries = (
            ('ps', '-a', '--filter', f'label=com.docker.compose.project={self.project}',
             '--filter', 'label=com.docker.compose.service=watchtower',
             '--format', '{{.Names}}|{{.State}}'),
            ('ps', '-a', '--filter', f'name=^/{expected}$',
             '--format', '{{.Names}}|{{.State}}'),
        )
        found = {}
        for query in queries:
            result = self.cli(*query, timeout=25)
            if result.returncode:
                reason = (result.stderr or result.stdout or '').strip().replace('\n', ' ')[:100]
                self.last_retirement_error = f'docker-ps-failed:{result.returncode}:{reason}'
                self.last_watchtower_state = 'unknown'
                return None  # Fail CLOSED if the Docker daemon cannot be queried.
            for line in result.stdout.splitlines():
                values = line.strip().split('|', 1)
                if len(values) != 2 or not all(values):
                    self.last_retirement_error = 'docker-ps-invalid-response'
                    self.last_watchtower_state = 'unknown'
                    return None
                name, state = values
                if name != expected and query is queries[1]:
                    # Defensive: the name filter must not retire other projects.
                    self.last_retirement_error = 'docker-ps-unexpected-name'
                    self.last_watchtower_state = 'unknown'
                    return None
                found[name] = state.strip().lower()
        self.last_retirement_error = ''
        self.last_watchtower_state = ('running' if any(st in {'running','restarting','paused'} for st in found.values())
                                      else 'stopped' if found else 'absent')
        return found

    def retire_watchtower(self):
        """Make this project's legacy updater inert, and confirm by Docker listing.

        Never touch a second project's Watchtower or any Docker volume. A stopped
        legacy object is not a competing writer; removal is only best-effort.
        """
        found = self._watchtower_objects()
        if found is None:
            return False
        if not found:
            return True
        for name, state in found.items():
            result = self.cli('update', '--restart=no', name, timeout=25)
            if result.returncode:
                self.last_retirement_error = f'watchtower-disable-restart-failed:{result.returncode}'
                return False
            if state not in {'exited', 'created', 'dead'}:
                result = self.cli('stop', '-t', '20', name, timeout=40)
                if result.returncode:
                    self.last_retirement_error = f'watchtower-stop-failed:{result.returncode}'
                    return False
            # Docker volume data is NEVER removed, even on removal failure.
            self.cli('rm', name, timeout=25)
        # A stopped object that races with rm is safe; an active writer is not.
        remaining = self._watchtower_objects()
        if remaining is None:
            return False
        if any(state not in {'exited', 'created', 'dead'} for state in remaining.values()):
            self.last_retirement_error = 'watchtower-reappeared-or-still-running'
            return False
        return True


class ManagedUpdates:
    def __init__(self, backend, runtime, assigned, stop, can_start, fenced, emit, poll=300, grace=180,
                 can_stop=None, can_recover=None, can_poll=None):
        self.backend, self.runtime, self.stop_event = backend, Path(runtime), stop
        self.services = tuple(dict.fromkeys(s for s in assigned if s not in DATA_SERVICES))
        self.can_start, self.fenced, self.emit = can_start, fenced, emit
        self.can_stop = can_stop or (lambda service: not self._wanted(service))
        self.can_recover = can_recover or can_start
        self.can_poll = can_poll or (lambda: True)
        self.registry_paused_for_ha = False
        self.policy_generation = 0
        self.health_confirmation_seconds = 60
        self.health_confirmation_samples = 3
        self.poll, self.grace = max(30, poll), max(30, grace)
        self.stable_grace = min(60, self.grace)
        self.lock = threading.RLock()
        self.desired = None
        self.desired_reason = 'not-yet-reconciled'
        self.config_version = ''
        self.records = {s: {} for s in self.services}
        self.phase, self.error, self.current_service = 'initializing', '', ''
        self.heartbeat = 0
        self.poll_heartbeat = 0
        self.scan_completed = 0
        self.retired = False
        self.next_owner_check = 0
        self.incident = {}
        self.persist_at = 0
        self.wake = threading.Event()
        try:
            saved = json.loads((self.runtime / 'application-updates.json').read_text())
            self.records.update({s: row for s, row in saved.get('services', {}).items() if s in self.records and isinstance(row, dict)})
            self.incident = saved.get('incident', {})
        except (ValueError, TypeError, OSError):
            pass
        # v41.5.5 treated some inspect errors (e.g. absent stopped containers)
        # as generic daemon failure. Clear ONLY that obsolete diagnosis so the
        # new independent fallback can recheck immediately; preserve all real
        # pull, image and health failures, and the last-known rollback image.
        for service in self.services:
            record = self.records[service]
            if record.get('error') == 'docker-inspect-unavailable':
                record.update(error='', failed_image='', next_attempt=0, attempts=0, status='rechecking-docker')
            # v41.5.9 could keep extending the retry backoff for a stopped
            # container whose Docker Health was still 'unhealthy'. Give this
            # exact legacy failure ONE immediate retry after upgrade; persist
            # the marker so an Agent restart cannot bypass future cooldowns.
            if (str(record.get('error') or '').startswith('healthcheck-failed:exited/')
                    and not record.get('stopped_health_retry_migrated')):
                record.update(next_attempt=0, verify_deadline=0,
                              stopped_health_retry_migrated=True,
                              status='retrying-stopped-recovery')
        # A restart must immediately check the registry, not trust an old timer.
        self.next_scan = 0
        self.registry_batch_size = 6
        self.registry_active = False
        self.registry_started_at = 0
        self.registry_batch = []
        self.scanned_services = set()
        # An operator-only, expiring standby maintenance lease. Promotion must
        # always override it: no maintenance request may hold back failover.
        self.maintenance_ack_token = ''
        self.prepare_verified_count = 0
        self.prepare_mismatch_count = 0
        for service in self.services:
            self.records[service]['next_pull_at'] = 0


    def set_desired(self, services, config_version, reason='ha-state'):
        wanted = set(services).intersection(self.services)
        with self.lock:
            changed = self.desired != wanted or self.config_version != config_version
            if changed:self.policy_generation += 1
            self.desired = wanted
            self.desired_reason = str(reason)[:80]
            self.config_version = config_version
        # Role promotion/demotion still wakes immediately. Unchanged HA ticks
        # must not trigger redundant full application scans between normal cycles.
        if changed:
            self.wake.set()

    def _mutation_allowed(self, service):
        if self._blocked():return False
        with self.lock:
            generation=self.policy_generation
            wanted=self._wanted(service)
        allowed=self.can_start(service) if wanted else self.can_stop(service)
        with self.lock:
            return bool(allowed and generation==self.policy_generation and not self._blocked())

    def _policy_stop(self, service, now):
        with self.lock:
            generation=self.policy_generation
            if self._wanted(service) or self._blocked():return False
        if not self.can_stop(service):return False
        # HA may recover while the authoritative probe is in flight.
        with self.lock:
            if self._wanted(service) or self._blocked() or generation!=self.policy_generation:return False
            reason=self.desired_reason
        self.backend.stop(service)
        self._record(service, status='staged', verify_deadline=0,
                     last_stop_reason='not-assigned:'+reason, last_stop_at=utc(now))
        return True

    def _unhealthy_confirmed(self, service, row, now):
        record=self._get(service)
        identity=str(row.get('id') or '')+'|'+str(row.get('started_at') or '')
        if row['state']!='running' or row['health']!='unhealthy':
            if record.get('unhealthy_since') is not None:
                self._record(service, unhealthy_since=None, unhealthy_samples=0, unhealthy_identity='')
            return False
        if record.get('unhealthy_since') is None or record.get('unhealthy_identity')!=identity:
            self._record(service, unhealthy_since=now, unhealthy_samples=1, unhealthy_identity=identity)
            return False
        samples=int(record.get('unhealthy_samples') or 0)+1
        self._record(service, unhealthy_samples=samples)
        return samples>=self.health_confirmation_samples and now-float(record['unhealthy_since'])>=self.health_confirmation_seconds

    def _record(self, service, **fields):
        with self.lock:
            self.records[service].update(fields)

    def _get(self, service):
        with self.lock:
            return dict(self.records[service])

    def _wanted(self, service):
        with self.lock:
            return self.desired is not None and service in self.desired

    def _blocked(self):
        return self.stop_event.is_set() or self.fenced() or self.desired is None

    def persist(self, now):
        with self.lock:
            data = {'services': self.records, 'incident': self.incident}
            atomic_json(self.runtime / 'application-updates.json', data)
        self.persist_at = now

    def _maintenance_token(self):
        """Return a live operator lease only while this is an unfenced standby.

        This is deliberately checked on *every* lifecycle tick rather than
        remembered. The moment HA requests any application, promotion wins.
        Expired or malformed leases cannot strand an updater after a crash.
        """
        with self.lock:
            safe_role = self.desired is not None and not self.desired
        if not safe_role or not self.retired or self.stop_event.is_set() or self.fenced():
            return ''
        try:
            path = self.runtime / 'updater-maintenance.json'
            meta = path.stat()
            if meta.st_uid != 0 or stat.S_IMODE(meta.st_mode) != 0o600:
                return ''
            row = json.loads(path.read_text())
            if (row.get('kind') == 'rabbitmq-standby' and
                    isinstance(row.get('token'), str) and
                    len(row['token']) >= 16 and
                    float(row.get('expires_at') or 0) > time.time()):
                return row['token']
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return ''

    def poll_once(self, now=None):
        """Check a fair, bounded subset; a stuck registry pull cannot starve all apps.

        Each result has its own next-attempt timestamp. A fast service can get
        a published fix while unrelated big/slow images are timing out. A full
        sweep is completed over multiple batches without recreating containers.
        """
        now = time.time() if now is None else now
        self.poll_heartbeat = now
        if self._blocked() or not self.retired or self._maintenance_token():
            return
        self.registry_paused_for_ha = not self.can_poll()
        if self.registry_paused_for_ha:
            return
        force = self.runtime / 'updates-check-request'
        if force.exists():
            force.unlink(missing_ok=True)
            self.next_scan = 0
            with self.lock:
                for service in self.services:
                    self.records[service]['next_pull_at'] = 0
        if now < self.next_scan:
            return
        with self.lock:
            due = [name for name in self.services if float(self.records[name].get('next_pull_at') or 0) <= now]
            due.sort(key=lambda name: (float(self.records[name].get('last_checked_epoch') or 0), name))
        if not due:
            with self.lock:
                self.next_scan = min(float(self.records[s].get('next_pull_at') or now + self.poll) for s in self.services) if self.services else now + self.poll
            return
        batch = due[:self.registry_batch_size]
        self.registry_active = True
        self.registry_started_at = now
        self.registry_batch = list(batch)
        try:
            try:
                references = self.backend.references(batch)
            except Exception as error:
                self.error = 'registry-config-failed:' + str(error)[:130]
                self.next_scan = time.time() + 30
                return
            if self.error.startswith(('registry-config-failed:', 'registry-loop-failed:')):
                self.error = ''
            def check_image(service):
                self.poll_heartbeat = time.time()
                if self.stop_event.is_set() or self.fenced():
                    return
                reference = references.get(service)
                if not reference:
                    err = 'service-image-not-configured'
                    previous = self._get(service)
                    failures = min(12, int(previous.get('pull_failures') or 0) + 1)
                    self._record(service, pull_status='failed', pull_error=err, pull_failures=failures,
                                 last_checked_at=utc(now), last_checked_epoch=now,
                                 next_pull_at=now + min(1800, 30 * 2 ** min(failures - 1, 6)))
                    return
                self._record(service, image_ref=reference, pull_status='checking')
                try:
                    image = self.backend.pull(reference)
                    old = self._get(service).get('target_image')
                    fields = {'target_image': image, 'pull_status': 'ready', 'pull_error': '',
                              'pull_failures': 0, 'last_checked_at': utc(now),
                              'last_checked_epoch': now, 'next_pull_at': now + self.poll}
                    if old != image:
                        fields.update(next_attempt=0, attempts=0, error='', failed_image='')
                    self._record(service, **fields)
                except Exception as error:
                    previous = self._get(service)
                    failures = min(12, int(previous.get('pull_failures') or 0) + 1)
                    self._record(service, pull_status='failed', pull_error=str(error)[:160],
                                 pull_failures=failures, last_checked_at=utc(now),
                                 last_checked_epoch=now,
                                 next_pull_at=now + min(1800, 30 * 2 ** min(failures - 1, 6)))
            # Submit only a small batch instead of 31+ queued jobs. Even when a
            # couple of pulls hit their timeout, later images are not trapped
            # behind the entire set of failed pulls.
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix='image-pull') as workers:
                pending = [workers.submit(check_image, service) for service in batch]
                for future in as_completed(pending):
                    future.result()
                    if self.stop_event.is_set() or self.fenced():
                        for queued in pending:
                            queued.cancel()
                        break
            self.scanned_services.update(batch)
            if len(self.scanned_services) >= len(self.services):
                self.scan_completed = time.time()
                self.scanned_services.clear()
            # Let the registry worker process the next due batch next tick;
            # retry failures independently at their persisted next_pull_at.
            self.next_scan = time.time() + 1 if len(due) > len(batch) else 0
            if not self.next_scan:
                with self.lock:
                    self.next_scan = min((float(self.records[s].get('next_pull_at') or now + self.poll) for s in self.services), default=now + self.poll)
            self.persist(time.time())
        finally:
            self.registry_active = False
            self.registry_batch = []

    def _failed(self, service, image, reason, now):
        previous = self._get(service)
        attempts = min(16, int(previous.get('attempts', 0)) + 1)
        self._record(service, status='retrying', failed_image=image, error=reason[:160], attempts=attempts,
                     next_attempt=now + min(1800, 30 * 2 ** min(attempts - 1, 6)), verify_deadline=0)

    def _apply(self, service, image, now, rollback=False):
        if self._blocked():
            return
        if self._wanted(service) and not self.can_start(service):
            self._record(service, status='waiting-role')
            return
        with self.lock:
            config_version = self.config_version
        self.current_service = service
        self.phase = 'rollback' if rollback else 'applying'
        self._record(service, status=self.phase)
        self.persist(now)  # Durable before mutation; interrupted apply is reconciled.
        began = time.monotonic()
        if not self._mutation_allowed(service):raise MutationDeferred('prepare-role-changed')
        observed=self.backend.observe([service]).get(service,{})
        if self._wanted(service) and observed.get('state')=='running' and not self.can_recover(service):
            raise MutationDeferred('dependencies-not-ready-for-replacement')
        if hasattr(self.backend,'prepare_guarded'):
            self.backend.prepare_guarded(service, image, lambda:self._mutation_allowed(service))
        else:
            self.backend.prepare(service, image)
        # Docker Compose can exit successfully without creating/replacing the
        # container with the requested immutable image. Validate Docker's
        # actual container state BEFORE advancing durable current_image.
        # Otherwise a standby retries the same up --no-start on every tick,
        # burning CPU yet reporting green / pending=0 between invocations.
        actual = self.backend.observe([service]).get(service, {})
        actual_id = str(actual.get('image') or '')
        if actual.get('state') == 'missing' or actual_id != image:
            self.prepare_mismatch_count += 1
            raise RuntimeError('container-prepare-not-applied:' +
                               (actual_id[:30] or 'missing') + '!=' + image[:30])
        self.prepare_verified_count += 1
        # DO NOT queue the old image for deletion before the candidate has
        # survived its stable-health window and a new last-good tag exists.
        fields = dict(applied_config=config_version, current_image=image,
                      status='staged', verify_deadline=0, stable_image='',
                      stable_since=0, stable_started_at='', stable_restart_count=0)
        if not rollback:
            # A verified candidate resolves a previous standby preparation
            # failure. On an active Primary the health proof still follows.
            fields.update(error='', next_attempt=0, attempts=0, failed_image='')
        self._record(service, **fields)
        # Re-read authoritative role after slow Docker work, including promotion/demotion.
        if self._wanted(service) and not self._blocked() and self.can_start(service):
            self.backend.start(service)
            self._record(service, status='verifying', verify_deadline=now + (time.monotonic() - began) + self.grace, started_at=utc())
            # Lack of START permission is not positive evidence for STOP.
            # A single REST timeout after a successful start used to kill it.
            if not self.fenced() and self.can_stop(service):
                self.backend.stop(service)
                self._record(service, status='staged', verify_deadline=0,
                             last_stop_reason='confirmed-role-loss-after-start', last_stop_at=utc())
        self.current_service = ''
        self.phase = 'reconciling'

    def step(self, now=None):
        now = time.time() if now is None else now
        self.heartbeat = now
        if self._blocked():
            self.maintenance_ack_token = ''
            self.phase = 'fenced' if self.fenced() else 'waiting-role'
            return
        # Maintenance is acknowledged only after any previous mutation has
        # finished. No image or container mutation can start while leased.
        token = self._maintenance_token()
        if token:
            self.maintenance_ack_token = token
            self.current_service = ''
            self.phase = 'maintenance'
            return
        self.maintenance_ack_token = ''
        if not self.retired:
            self.phase = 'taking-ownership'
            if not self.backend.retire_watchtower():
                self.error = 'legacy-owner-check-failed:' + str(getattr(self.backend, 'last_retirement_error', '') or 'unconfirmed')[:125]
                self._incidents(now)
                self.persist(now)
                return
            self.retired = True
            self.next_owner_check = now + 60
            self.error = ''
        elif now >= self.next_owner_check:
            # A forgotten legacy profile or an old operator script may revive
            # Watchtower. Retire it again, but never on every five-second tick.
            self.next_owner_check = now + 60
            if not self.backend.retire_watchtower():
                self.error = 'legacy-owner-check-failed:' + str(getattr(self.backend, 'last_retirement_error', '') or 'unconfirmed')[:125]
                self._incidents(now)
                self.persist(now)
                return
            if self.error.startswith('legacy-owner-check-failed:'):
                self.error = ''
        rows = self.backend.observe(self.services)
        if self.error.startswith('reconcile-failed:'):
            self.error = ''
        # Stop all obsolete writers before starting anything in a new role.
        for service in self.services:
            row = rows[service]
            if self._blocked():
                return
            if not self._wanted(service) and row['state'] in {'running', 'restarting', 'paused'}:
                try:
                    self._policy_stop(service, now)
                except Exception as error:
                    self._failed(service, row['image'], str(error), now)
        for service in self.services:
            self.heartbeat = time.time()
            if self._blocked():
                break
            row, record = rows[service], self._get(service)
            image = row['image']
            target = record.get('target_image') or image
            wanted = self._wanted(service)
            ready = row['state'] == 'running' and row['health'] in {'healthy', 'none'}
            unhealthy_confirmed=self._unhealthy_confirmed(service,row,now)
            self._record(service, current_image=image, container_state=row['state'], health=row['health'], restart_count=row.get('restarts', 0))
            if not ready and record.get('stable_image'):
                # Any unhealthy/starting interval breaks the candidate's
                # consecutive-stability proof. A flapping image never becomes
                # the rollback image just because it answered one healthcheck.
                self._record(service, stable_image='', stable_since=0, stable_started_at='')
            if ready and wanted:
                # Startup with no persisted deployment history adopts a working
                # container without recreating it just to populate a cache.
                if not record.get('applied_config'):
                    self._record(service, applied_config=self.config_version)
                    record['applied_config'] = self.config_version
                if record.get('last_good_image') != image:
                    old_good = record.get('last_good_image')
                    if old_good:
                        # A *new* release requires a consecutive healthy period
                        # before superseding the known-good rollback tag. Detect
                        # restarts as well as health flapping during that period.
                        started = str(row.get('started_at') or '')
                        restarts = int(row.get('restarts') or 0)
                        if (record.get('stable_image') != image or
                                record.get('stable_started_at', '') != started or
                                int(record.get('stable_restart_count') or 0) != restarts):
                            self._record(service, stable_image=image, stable_since=now,
                                         stable_started_at=started, stable_restart_count=restarts,
                                         status='verifying')
                            continue
                        if now - float(record.get('stable_since') or now) < self.stable_grace:
                            self._record(service, status='verifying')
                            continue
                    # Only a verified candidate may replace the last-good tag.
                    # Failed tag/preservation never queues the old image for GC.
                    if self.backend.preserve(service, image):
                        self._record(service, last_good_image=image, stable_image='',
                                     stable_since=0, stable_started_at='')
                        if old_good:
                            self._queue_cleanup(service, old_good)
                    else:
                        self._record(service, status='verifying', error='rollback-image-preserve-failed')
                        continue
                if not record.get('failed_image') or record['failed_image'] == image:
                    self._record(service, status='healthy', error='', next_attempt=0, attempts=0, verify_deadline=0, failed_image='')
                else:
                    self._record(service, status='rolled-back', verify_deadline=0)
            elif record.get('verify_deadline', 0) and now < record['verify_deadline']:
                self._record(service, status='verifying')
                continue
            elif (wanted and image == target and row['state'] in {'exited', 'created', 'dead'}
                  and record.get('verify_deadline', 0)):
                # A previous start never became healthy. Fail ONCE, clear its
                # verify deadline and wait for a bounded retry. Do not extend
                # backoff again merely by observing an old unhealthy healthcheck.
                self._failed(service, image, 'start-verification-failed:' +
                             row['state'] + '/' + row['health'], now)
                continue
            elif (wanted and image == target and row['state'] not in {'exited', 'created', 'dead'}
                  and (record.get('verify_deadline', 0) or row['health'] == 'unhealthy'
                       or row['state'] == 'restarting')):
                if not record.get('next_attempt', 0) or now >= record['next_attempt']:
                    # Before destroying a running process, prove both persistent
                    # unhealthy state and that dependencies/HA permit recovery.
                    if row['state']=='running' and row['health']=='unhealthy':
                        if not unhealthy_confirmed:
                            self._record(service,status='confirming-unhealthy')
                            continue
                        if not self.can_recover(service) or not self._wanted(service) or self._blocked():
                            self._record(service,status='waiting-recovery-authority')
                            continue
                    good = record.get('last_good_image')
                    self._failed(service, image, 'healthcheck-failed:' + row['state'] + '/' + row['health'], now)
                    if good and good != image:
                        try:
                            self._apply(service, good, now, rollback=True)
                        except MutationDeferred:
                            self._record(service,status='waiting-role')
                        except Exception as error:
                            self._failed(service, image, 'rollback-failed:' + str(error), now)
                        continue
                    # An unhealthy but still running process is not restarted by
                    # Docker's restart policy. Recover it only at the bounded
                    # per-service retry, never at the five-second HA cadence.
                    if row['state'] == 'running' and row['health'] == 'unhealthy':
                        try:
                            if not self.can_recover(service) or not self._wanted(service) or self._blocked():
                                continue
                            self.backend.stop(service)
                            self._record(service,last_stop_reason='confirmed-unhealthy-recovery',last_stop_at=utc(now))
                            if self._wanted(service) and not self._blocked() and self.can_start(service):
                                self.backend.start(service)
                                self._record(service, status='verifying', verify_deadline=now + self.grace)
                        except Exception as error:
                            self._failed(service, image, 'recovery-failed:' + str(error), now)
                        continue
            record = self._get(service)
            if record.get('next_attempt', 0) > now:
                continue
            if not target:
                self._record(service, status='awaiting-image')
                continue
            # Never revive a deliberately stopped standby. Stage its exact image
            # and rendered routing; verify it only after actual promotion.
            config_changed = bool(record.get('applied_config')) and record['applied_config'] != self.config_version
            needs_image = image != target or row['state'] == 'missing'
            try:
                if needs_image or config_changed:
                    reason = ('missing-container' if row['state'] == 'missing' else
                              'image-mismatch' if image != target else 'routing-change')
                    self._record(service, last_prepare_reason=reason,
                                 last_prepare_at=utc(now),
                                 observed_before_prepare=image)
                    self._apply(service, target, now)
                elif wanted and row['state'] in {'exited', 'created', 'dead'}:
                    if self.can_start(service) and not self._blocked():
                        self.backend.start(service)
                        self._record(service, status='verifying', started_at=utc(now),
                                     verify_deadline=now + self.grace,
                                     last_start_reason='desired-running-self-heal',
                                     last_start_at=utc(now))
                        if not self.fenced() and self.can_stop(service):
                            self.backend.stop(service)
                            self._record(service,status='staged',verify_deadline=0,
                                         last_stop_reason='confirmed-role-loss-after-start',last_stop_at=utc(now))
                elif not wanted and row['state'] not in {'running','restarting','paused'}:
                    self._record(service, status='staged', error='', applied_config=self.config_version, verify_deadline=0)
                elif not ready and row['health'] == 'starting':
                    if not record.get('verify_deadline', 0):
                        self._record(service, status='verifying', verify_deadline=now + self.grace)
            except MutationDeferred:
                self._record(service,status='waiting-role')
                self.current_service=''
            except Exception as error:
                self._failed(service, target, str(error), now)
                good = self._get(service).get('last_good_image')
                if good and good != target and self._wanted(service) and not self._blocked():
                    try:
                        self._apply(service, good, now, rollback=True)
                    except MutationDeferred:
                        self._record(service,status='waiting-role')
                    except Exception as rollback_error:
                        self._failed(service, target, 'rollback-failed:' + str(rollback_error), now)
                self.current_service = ''
        self.phase = 'monitoring'
        self._cleanup_one()
        self._incidents(now)
        self.persist(now)

    def _queue_cleanup(self, service, image):
        with self.lock:
            queue = list(self.records[service].get('obsolete_images') or [])
            if image not in queue:
                queue.append(image)
            self.records[service]['obsolete_images'] = queue[-32:]

    def _cleanup_one(self):
        if self._blocked() or not hasattr(self.backend, 'cleanup'):
            return
        with self.lock:
            keep = {r.get(k) for r in self.records.values() for k in ('current_image', 'target_image', 'last_good_image')}
            candidate = next(((name, image) for name, row in self.records.items() for image in row.get('obsolete_images', []) if image not in keep), None)
        if candidate:
            service, image = candidate
            self.backend.cleanup(image)
            with self.lock:
                queue = self.records[service].get('obsolete_images', [])
                self.records[service]['obsolete_images'] = [x for x in queue if x != image]

    def _incidents(self, now):
        with self.lock:
            failed = {s: r.get('error') or r.get('pull_error') for s, r in self.records.items() if r.get('error') or r.get('pull_error')}
            if self.error:
                failed['updater'] = self.error
            if failed:
                if not self.incident:
                    self.incident = {'id': uuid.uuid4().hex, 'since': now, 'notified': False}
                self.incident.pop('clear_since', None)
                if now - self.incident['since'] >= 60 and not self.incident.get('notified'):
                    names = ', '.join(sorted(failed))
                    self.emit('update.incident', 'Приложения требуют внимания',
                              f'{names}. Сбой проверки, образа или запуска; это не обязательно обновление. Повторные попытки автоматические; подробности в разделе «Кластер».',
                              'error', event_id='updates-' + self.incident['id'])
                    self.incident['notified'] = True
            elif self.incident:
                self.incident.setdefault('clear_since', now)
                if now - self.incident['clear_since'] >= 30:
                    if self.incident.get('notified'):
                        self.emit('update.recovered', 'Обновление приложений восстановлено',
                                  'Все отслеживаемые ошибки устранены.', 'success', event_id='updates-recovered-' + self.incident['id'])
                    self.incident = {}

    def snapshot(self):
        now = time.time()
        with self.lock:
            rows = [dict(self.records[s], service=s, desired_state='running' if self._wanted(s) else 'prepared',
                         retry_in_seconds=max(0, int(self.records[s].get('next_attempt', 0) - now))) for s in self.services]
        return {'mode': 'node-agent', 'running': now - self.heartbeat < 300 and not self.stop_event.is_set(),
                'watchtower_running': getattr(self.backend, 'last_watchtower_state', '') == 'running',
                'legacy_watchtower_state': getattr(self.backend, 'last_watchtower_state', 'unknown'),
                'legacy_owner_error': getattr(self.backend, 'last_retirement_error', ''),
                'include_stopped': True, 'revive_stopped': False,
                'status': self.phase, 'current_service': self.current_service, 'error': self.error,
                'desired_count': len(self.desired) if self.desired is not None else None,
                'desired_reason': self.desired_reason,
                'policy_generation': self.policy_generation,
                'health_confirmation_seconds': self.health_confirmation_seconds,
                'health_confirmation_samples': self.health_confirmation_samples,
                'poll_interval_seconds': self.poll, 'last_scan_at': utc(self.scan_completed) if self.scan_completed else None,
                'next_scan_at': utc(self.next_scan) if self.next_scan else None,
                'heartbeat_at': utc(self.heartbeat) if self.heartbeat else None,
                'registry_heartbeat_at': utc(self.poll_heartbeat) if self.poll_heartbeat else None,
                'registry_active': self.registry_active,
                'registry_paused_for_ha': self.registry_paused_for_ha,
                'registry_current_batch': list(self.registry_batch),
                'registry_stalled': bool(self.registry_active and now - self.registry_started_at > 2 * 420),
                'registry_pull_timeout_seconds': 420,
                'registry_thread_alive': getattr(self, 'registry_thread', None).is_alive() if getattr(self, 'registry_thread', None) is not None else None,
                'compose_calls': self.backend.compose_counters() if hasattr(self.backend, 'compose_counters') else {},
                'docker_observe': self.backend.observe_counters() if hasattr(self.backend, 'observe_counters') else {},
                'prepare_verified_count': self.prepare_verified_count,
                'prepare_mismatch_count': self.prepare_mismatch_count,
                'maintenance_ack_token': self.maintenance_ack_token if self._maintenance_token() else '',
                'lifecycle_thread_alive': getattr(self, 'lifecycle_thread', None).is_alive() if getattr(self, 'lifecycle_thread', None) is not None else None,
                'services': rows, 'failed_count': sum(bool(r.get('error') or r.get('pull_error')) for r in rows) + bool(self.error),
                'pending_count': sum(bool(r.get('target_image')) and r.get('current_image') != r.get('target_image') for r in rows),
                'protected_services': sorted(DATA_SERVICES)}

    def reconcile_loop(self):
        self.runtime.mkdir(parents=True, exist_ok=True)
        with (self.runtime / 'application-updates.lock').open('a') as ownership:
            try:
                fcntl.flock(ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.phase = 'blocked'
                self.error = 'another-application-updater-is-active'
                return
            while not self.stop_event.is_set():
                try:
                    self.step()
                except Exception as error:
                    self.error = 'reconcile-failed:' + type(error).__name__
                    self.phase = 'retrying'
                    self._incidents(time.time())
                self.wake.wait(5)
                self.wake.clear()

    def registry_loop(self):
        while not self.stop_event.is_set():
            try:
                self.poll_once()
            except Exception as error:
                self.error = 'registry-loop-failed:' + type(error).__name__
            self.stop_event.wait(5)
