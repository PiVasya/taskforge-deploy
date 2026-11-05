#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))

cleanup_spec = importlib.util.spec_from_file_location('taskforge_log_cleanup_v41.5.5', ROOT / 'cluster' / 'log_cleanup.py')
cleanup_module = importlib.util.module_from_spec(cleanup_spec)
assert cleanup_spec.loader is not None
cleanup_spec.loader.exec_module(cleanup_module)
LogCleanupManager = cleanup_module.LogCleanupManager
maintenance_token = cleanup_module.maintenance_token

key = 'm' * 64
expected = hmac.new(key.encode(), b'taskforge-cluster-agent-maintenance-v1', hashlib.sha256).hexdigest()
assert maintenance_token({'TASKFORGE_INTERNAL_KEY': key}) == expected
assert maintenance_token({'TASKFORGE_INTERNAL_KEY': 'short'}) == ''

with tempfile.TemporaryDirectory(prefix='taskforge-v41.5.5-cleanup-') as raw:
    td = Path(raw)
    docker_root = td / 'docker'
    cid_json = 'a' * 64
    cid_local = 'b' * 64
    log_dir = docker_root / 'containers' / cid_json
    log_dir.mkdir(parents=True)
    log_path = log_dir / f'{cid_json}-json.log'
    rotated = log_dir / f'{cid_json}-json.log.1'
    log_path.write_bytes(b'x' * 4096)
    rotated.write_bytes(b'y' * 2048)

    local_dir = docker_root / 'containers' / cid_local
    local_dir.mkdir(parents=True)
    local_path = local_dir / 'local.log'
    local_path.write_bytes(b'z' * 1024)

    host_logs = td / 'host-logs'
    host_logs.mkdir()
    migrate = host_logs / 'migrate-v41.5.5-test.log'
    migrate.write_bytes(b'm' * 512)

    calls = []

    def fake_run(args, timeout=60, env=None):
        calls.append(tuple(args))
        if args[:3] == ['docker', 'ps', '-aq']:
            return subprocess.CompletedProcess(args, 0, cid_json + '\n' + cid_local + '\n', '')
        if args[:3] == ['docker', 'info', '--format']:
            return subprocess.CompletedProcess(args, 0, str(docker_root) + '\n', '')
        if args[:2] == ['docker', 'inspect']:
            payload = [
                {
                    'Id': cid_json,
                    'LogPath': str(log_path),
                    'Config': {'Labels': {'com.docker.compose.project': 'taskforge-prod'}},
                    'HostConfig': {'LogConfig': {'Type': 'json-file'}},
                },
                {
                    'Id': cid_local,
                    'LogPath': str(local_path),
                    'Config': {'Labels': {'com.docker.compose.project': 'taskforge-prod'}},
                    'HostConfig': {'LogConfig': {'Type': 'local'}},
                },
            ]
            return subprocess.CompletedProcess(args, 0, json.dumps(payload), '')
        if args == ['journalctl', '--rotate']:
            return subprocess.CompletedProcess(args, 0, '', '')
        if args == ['journalctl', '--vacuum-size=128M']:
            return subprocess.CompletedProcess(args, 0, '', 'Vacuuming done, freed 64.0M of archived journals.\n')
        raise AssertionError(f'unexpected command: {args}')

    result = LogCleanupManager(
        td / 'runtime',
        'taskforge-prod',
        runner=fake_run,
        host_log_dir=host_logs,
        filesystem_root=td,
    ).cleanup()

    assert result['status'] == 'completed', result
    assert result['docker']['containers_found'] == 2, result
    assert result['docker']['containers_cleared'] == 1, result
    assert result['docker']['containers_skipped'] == 1, result
    assert result['docker']['skipped_drivers'] == {'local': 1}, result
    assert result['docker']['reclaimed_bytes'] == 4096 + 2048, result
    assert result['host_logs']['reclaimed_bytes'] == 512, result
    assert result['journal']['reclaimed_bytes'] == 64 * 1024 * 1024, result
    assert result['preserved'] == {
        'docker_volumes': True,
        'docker_images': True,
        'diagnostics_archives': True,
        'databases': True,
    }
    assert log_path.stat().st_size == 0
    assert not rotated.exists()
    assert local_path.stat().st_size == 1024
    assert migrate.stat().st_size == 0
    joined = ' '.join(' '.join(x) for x in calls)
    for forbidden in ('docker volume rm', 'docker image rm', 'docker system prune', 'docker volume prune'):
        assert forbidden not in joined, joined

agent_spec = importlib.util.spec_from_file_location('taskforge_agent_v41.5.5', ROOT / 'cluster' / 'agent.py')
agent_module = importlib.util.module_from_spec(agent_spec)
assert agent_spec.loader is not None
agent_spec.loader.exec_module(agent_module)


class Diagnostics:
    def __init__(self):
        self.busy = False

    def is_busy(self):
        return self.busy


class Cleanup:
    def cleanup(self):
        return {
            'status': 'completed',
            'reclaimed_bytes': 1234,
            'filesystem_free_delta_bytes': 1234,
            'docker': {'containers_cleared': 2},
            'host_logs': {'files_cleared': 1},
            'journal': {'ok': True},
            'preserved': {'docker_volumes': True, 'docker_images': True, 'diagnostics_archives': True, 'databases': True},
        }


class Edge:
    def snapshot(self):
        return {'success': True}


def request(path, raw):
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    sock.connect(path)
    sock.sendall(raw)
    chunks = []
    while True:
        part = sock.recv(65536)
        if not part:
            break
        chunks.append(part)
    sock.close()
    blob = b''.join(chunks)
    head, _, body = blob.partition(b'\r\n\r\n')
    status = int(head.split(b' ', 2)[1])
    return status, json.loads(body.decode())


with tempfile.TemporaryDirectory(prefix='taskforge-v41.5.5-cleanup-socket-') as raw:
    path = str(Path(raw) / 'node-agent.sock')
    diagnostics = Diagnostics()
    agent_module.Handler.agent = SimpleNamespace(
        diagnostics_token='diag-token',
        maintenance_token='maintenance-token',
        diagnostics=diagnostics,
        log_cleanup=Cleanup(),
        maintenance_lock=threading.RLock(),
        node_id='A',
        bundle_revision='73',
        telemetry={'ha': {'reconciled': True}},
        edge_controller=Edge(),
        _cloudflare_configured=lambda: True,
    )
    server = agent_module.ThreadingUnixHTTPServer(path, agent_module.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        good = (
            b'POST /ha/logs/cleanup HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n'
            b'X-TaskForge-Cluster-Token: maintenance-token\r\nContent-Length: 2\r\n\r\n{}'
        )
        status, payload = request(path, good)
        assert status == 200 and payload['node'] == 'A' and payload['reclaimed_bytes'] == 1234, (status, payload)

        bad = b'POST /ha/logs/cleanup HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}'
        status, payload = request(path, bad)
        assert status == 401 and payload['code'] == 'LOG_CLEANUP_UNAUTHORIZED', (status, payload)

        diagnostics.busy = True
        status, payload = request(path, good)
        assert status == 409 and payload['code'] == 'LOG_CLEANUP_DIAGNOSTICS_BUSY', (status, payload)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

print('PASS: v41.5.5 authenticated log cleanup is bounded, preserves data, and rejects diagnostics overlap')
