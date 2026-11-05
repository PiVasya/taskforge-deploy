from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import shutil
import subprocess
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TOKEN_PURPOSE = b'taskforge-cluster-agent-maintenance-v1'
DEFAULT_JOURNAL_VACUUM_SIZE = '128M'


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def maintenance_token(env: dict[str, str]) -> str:
    key = str(env.get('TASKFORGE_INTERNAL_KEY', ''))
    if len(key) < 40:
        return ''
    return hmac.new(key.encode(), TOKEN_PURPOSE, hashlib.sha256).hexdigest()


def run(args: list[str], timeout: int = 60, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)
    except Exception as exc:
        return subprocess.CompletedProcess(args, 125, '', str(exc))


class LogCleanupError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _parse_size(value: str) -> int:
    match = re.search(r'([0-9]+(?:\.[0-9]+)?)\s*([KMGTPE]?[Bb]?)', value or '', re.IGNORECASE)
    if not match:
        return 0
    amount = float(match.group(1))
    unit = match.group(2).upper()
    if unit and not unit.endswith('B'):
        unit += 'B'
    if not unit:
        unit = 'B'
    powers = {'B': 0, 'KB': 1, 'MB': 2, 'GB': 3, 'TB': 4, 'PB': 5, 'EB': 6}
    return int(amount * (1024 ** powers.get(unit, 0)))


class LogCleanupManager:
    def __init__(
        self,
        runtime: Path,
        project: str,
        *,
        runner=run,
        host_log_dir: Path = Path('/var/log/taskforge'),
        filesystem_root: Path = Path('/'),
        journal_vacuum_size: str = DEFAULT_JOURNAL_VACUUM_SIZE,
    ):
        self.runtime = runtime
        self.project = project
        self.runner = runner
        self.host_log_dir = host_log_dir
        self.filesystem_root = filesystem_root
        self.journal_vacuum_size = journal_vacuum_size
        self.lock = threading.Lock()

    def cleanup(self) -> dict[str, Any]:
        if not self.lock.acquire(blocking=False):
            raise LogCleanupError(409, 'LOG_CLEANUP_BUSY', 'log cleanup is already running on this node')
        started_at = utc_now()
        try:
            free_before = self._free_bytes()
            docker = self._clear_docker_logs()
            host = self._clear_host_logs()
            journal = self._vacuum_journal()
            free_after = self._free_bytes()
            known = int(docker.get('reclaimed_bytes') or 0) + int(host.get('reclaimed_bytes') or 0) + int(journal.get('reclaimed_bytes') or 0)
            free_delta = max(0, free_after - free_before) if free_before >= 0 and free_after >= 0 else 0
            return {
                'status': 'completed',
                'started_at': started_at,
                'finished_at': utc_now(),
                'reclaimed_bytes': known,
                'filesystem_free_delta_bytes': free_delta,
                'docker': docker,
                'host_logs': host,
                'journal': journal,
                'preserved': {
                    'docker_volumes': True,
                    'docker_images': True,
                    'diagnostics_archives': True,
                    'databases': True,
                },
            }
        finally:
            self.lock.release()

    def _free_bytes(self) -> int:
        try:
            return int(shutil.disk_usage(self.filesystem_root).free)
        except OSError:
            return -1

    def _docker_root(self) -> Path | None:
        result = self.runner(['docker', 'info', '--format', '{{.DockerRootDir}}'], timeout=20)
        if result.returncode != 0:
            return None
        raw = (result.stdout or '').strip()
        if not raw or not os.path.isabs(raw):
            return None
        try:
            return Path(raw).resolve(strict=False)
        except OSError:
            return None

    def _clear_docker_logs(self) -> dict[str, Any]:
        listed = self.runner([
            'docker', 'ps', '-aq', '--filter', f'label=com.docker.compose.project={self.project}'
        ], timeout=20)
        if listed.returncode != 0:
            return {
                'ok': False,
                'containers_found': 0,
                'containers_cleared': 0,
                'containers_skipped': 0,
                'reclaimed_bytes': 0,
                'message': (listed.stderr or listed.stdout or 'docker ps failed').strip()[:400],
            }
        ids = [line.strip() for line in (listed.stdout or '').splitlines() if line.strip()]
        if not ids:
            return {'ok': True, 'containers_found': 0, 'containers_cleared': 0, 'containers_skipped': 0, 'reclaimed_bytes': 0, 'message': 'no TaskForge containers found'}

        docker_root = self._docker_root()
        if docker_root is None:
            return {
                'ok': False,
                'containers_found': len(ids),
                'containers_cleared': 0,
                'containers_skipped': len(ids),
                'reclaimed_bytes': 0,
                'message': 'Docker root directory is unavailable',
            }
        allowed_root = (docker_root / 'containers').resolve(strict=False)
        inspected = self.runner(['docker', 'inspect', *ids], timeout=30)
        if inspected.returncode != 0:
            return {
                'ok': False,
                'containers_found': len(ids),
                'containers_cleared': 0,
                'containers_skipped': len(ids),
                'reclaimed_bytes': 0,
                'message': (inspected.stderr or inspected.stdout or 'docker inspect failed').strip()[:400],
            }
        try:
            rows = json.loads(inspected.stdout or '[]')
        except (TypeError, ValueError):
            rows = []

        reclaimed = 0
        cleared = 0
        skipped = 0
        skipped_drivers: dict[str, int] = {}
        errors: list[str] = []
        for row in rows if isinstance(rows, list) else []:
            labels = ((row.get('Config') or {}).get('Labels') or {}) if isinstance(row, dict) else {}
            if str(labels.get('com.docker.compose.project') or '') != self.project:
                skipped += 1
                continue
            # HA evidence is never erased by the application log-cleanup action.
            service=str(labels.get('com.docker.compose.service') or '')
            if service in {'postgres','etcd'}:
                skipped += 1
                skipped_drivers['protected-ha'] = skipped_drivers.get('protected-ha',0)+1
                continue
            host = row.get('HostConfig') or {}
            log_cfg = host.get('LogConfig') or {}
            driver = str(log_cfg.get('Type') or '').strip() or 'unknown'
            if driver != 'json-file':
                skipped += 1
                skipped_drivers[driver] = skipped_drivers.get(driver, 0) + 1
                continue
            raw_path = str(row.get('LogPath') or '').strip()
            container_id = str(row.get('Id') or '').strip()
            if not raw_path or not container_id or not os.path.isabs(raw_path):
                skipped += 1
                continue
            try:
                path = Path(raw_path).resolve(strict=False)
                path.relative_to(allowed_root)
            except (OSError, ValueError):
                skipped += 1
                errors.append(f'{container_id[:12]}: unsafe log path')
                continue
            if container_id not in path.parts:
                skipped += 1
                errors.append(f'{container_id[:12]}: unexpected log path')
                continue
            try:
                current_size = path.stat().st_size if path.is_file() else 0
                if path.is_file():
                    with path.open('r+b', buffering=0) as stream:
                        stream.truncate(0)
                    reclaimed += current_size
                for rotated in path.parent.glob(path.name + '.*'):
                    if rotated.is_symlink() or not rotated.is_file():
                        continue
                    try:
                        size = rotated.stat().st_size
                        rotated.unlink()
                        reclaimed += size
                    except OSError as exc:
                        errors.append(f'{container_id[:12]}: {exc}')
                cleared += 1
            except OSError as exc:
                skipped += 1
                errors.append(f'{container_id[:12]}: {exc}')

        # If inspect unexpectedly omitted a container, count it as skipped instead of
        # pretending that every discovered container was processed.
        if len(rows) < len(ids):
            skipped += len(ids) - len(rows)
        return {
            'ok': len(errors) == 0,
            'containers_found': len(ids),
            'containers_cleared': cleared,
            'containers_skipped': skipped,
            'skipped_drivers': skipped_drivers,
            'reclaimed_bytes': reclaimed,
            'message': '; '.join(errors[:4]) if errors else 'TaskForge json-file logs cleared',
        }

    def _clear_host_logs(self) -> dict[str, Any]:
        reclaimed = 0
        cleared = 0
        errors: list[str] = []
        try:
            candidates = sorted(self.host_log_dir.glob('*.log')) if self.host_log_dir.is_dir() else []
        except OSError as exc:
            return {'ok': False, 'files_cleared': 0, 'reclaimed_bytes': 0, 'message': str(exc)[:400]}
        for path in candidates:
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                size = path.stat().st_size
                with path.open('r+b', buffering=0) as stream:
                    stream.truncate(0)
                reclaimed += size
                cleared += 1
            except OSError as exc:
                errors.append(f'{path.name}: {exc}')
        return {
            'ok': len(errors) == 0,
            'files_cleared': cleared,
            'reclaimed_bytes': reclaimed,
            'message': '; '.join(errors[:4]) if errors else 'TaskForge host logs cleared',
        }

    def _vacuum_journal(self) -> dict[str, Any]:
        env = os.environ.copy()
        env['LC_ALL'] = 'C'
        rotated = self.runner(['journalctl', '--rotate'], timeout=45, env=env)
        vacuumed = self.runner(['journalctl', f'--vacuum-size={self.journal_vacuum_size}'], timeout=90, env=env)
        text = '\n'.join(x for x in (rotated.stdout, rotated.stderr, vacuumed.stdout, vacuumed.stderr) if x)
        reclaimed = 0
        for match in re.finditer(r'freed\s+([0-9]+(?:\.[0-9]+)?\s*[KMGTPE]?[Bb]?)', text, re.IGNORECASE):
            reclaimed += _parse_size(match.group(1))
        ok = rotated.returncode == 0 and vacuumed.returncode == 0
        detail = (vacuumed.stderr or vacuumed.stdout or rotated.stderr or rotated.stdout or '').strip().replace('\n', ' ')
        return {
            'ok': ok,
            'vacuum_size': self.journal_vacuum_size,
            'reclaimed_bytes': reclaimed,
            'message': detail[:600] if detail else ('journal vacuum completed' if ok else 'journal vacuum failed'),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description='Clear bounded TaskForge server logs without touching application data.')
    parser.add_argument('--runtime', default=os.environ.get('TASKFORGE_CLUSTER_RUNTIME', '/var/lib/taskforge/cluster'))
    parser.add_argument('--project', default=os.environ.get('COMPOSE_PROJECT_NAME', 'taskforge-prod'))
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit('error: run with sudo/root')
    result = LogCleanupManager(Path(args.runtime), args.project).cleanup()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
