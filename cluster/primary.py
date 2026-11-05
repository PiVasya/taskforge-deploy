#!/usr/bin/env python3
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
from pathlib import Path
from typing import Any

DEFAULT_CLUSTER = Path('/etc/taskforge/cluster.json')
DEFAULT_ENV = Path('/etc/taskforge/taskforge.env')
DEFAULT_RUNTIME = Path('/var/lib/taskforge/cluster')


class PrimaryError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:
        raise PrimaryError(f'cannot read {path}: {exc}')


def load_env(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        if not raw.strip() or raw.lstrip().startswith('#') or '=' not in raw:
            continue
        key, value = raw.split('=', 1)
        value = value.strip()
        if len(value) >= 2 and value[:1] == value[-1:] and value[0] in "\"'":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def node_map(cluster: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(node.get('id', '')): node for node in cluster.get('nodes') or [] if str(node.get('id', ''))}


def node_url(node: dict[str, Any], kind: str) -> str:
    ip = str((node.get('wireguard') or {}).get('ip', '')).strip()
    if not ip:
        raise PrimaryError(f'node {node.get("id")}: WireGuard IP missing')
    if kind == 'patroni':
        port = int((node.get('postgres') or {}).get('patroni_rest_port', 8008))
    elif kind == 'agent':
        port = int(node.get('health_port', 9187))
    else:
        raise ValueError(kind)
    return f'http://{ip}:{port}'


def http_json(url: str, timeout: float = 4.0) -> tuple[int, dict[str, Any]]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            raw = response.read(2_000_000)
            return int(response.status), json.loads(raw.decode('utf-8')) if raw else {}
    except urllib.error.HTTPError as exc:
        try:
            return int(exc.code), json.loads(exc.read(2_000_000).decode('utf-8'))
        except Exception:
            return int(exc.code), {}
    except Exception:
        return 0, {}


def patroni_auth(env: dict[str, str]) -> str:
    key = env.get('TASKFORGE_INTERNAL_KEY', '')
    if len(key) < 40:
        raise PrimaryError('TASKFORGE_INTERNAL_KEY missing/short; cannot authenticate Patroni switchover')
    password = hmac.new(key.encode(), b'taskforge-patroni-rest-v2', hashlib.sha512).hexdigest()[:64]
    token = base64.b64encode(f'taskforge_patroni:{password}'.encode()).decode()
    return 'Basic ' + token


def post_switchover(leader_node: dict[str, Any], leader: str, target: str, env: dict[str, str]) -> tuple[int, str]:
    payload = json.dumps({'leader': leader, 'candidate': target}, separators=(',', ':')).encode('utf-8')
    request = urllib.request.Request(
        node_url(leader_node, 'patroni') + '/switchover',
        data=payload,
        method='POST',
        headers={'Content-Type': 'application/json', 'Authorization': patroni_auth(env)},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return int(response.status), response.read(65536).decode('utf-8', 'replace')
    except urllib.error.HTTPError as exc:
        return int(exc.code), exc.read(65536).decode('utf-8', 'replace')
    except Exception as exc:
        return 0, f'{type(exc).__name__}: {exc}'


def cluster_view(nodes: dict[str, dict[str, Any]], local_id: str) -> tuple[dict[str, Any], str]:
    order = [local_id] + [nid for nid in nodes if nid != local_id]
    seen: list[str] = []
    for nid in order:
        node = nodes.get(nid)
        if not node:
            continue
        code, doc = http_json(node_url(node, 'patroni') + '/cluster')
        seen.append(f'{nid}:{code or 0}')
        if code == 200 and isinstance(doc.get('members'), list):
            return doc, nid
    raise PrimaryError('cannot read Patroni cluster view from any node (' + ', '.join(seen) + ')')


def unique_leader(doc: dict[str, Any]) -> str:
    leaders = [str(member.get('name', '')) for member in doc.get('members') or [] if str(member.get('role', '')).lower() in {'leader', 'primary', 'master'}]
    leaders = [x for x in leaders if x]
    if len(leaders) != 1:
        raise PrimaryError(f'expected exactly one Patroni leader, got {leaders}')
    return leaders[0]


def member_by_name(doc: dict[str, Any], node_id: str) -> dict[str, Any]:
    member = next((m for m in doc.get('members') or [] if str(m.get('name', '')) == node_id), None)
    if not member:
        raise PrimaryError(f'node {node_id} is not present in Patroni /cluster')
    return member


def agent_telemetry(node: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return http_json(node_url(node, 'agent') + '/ha/telemetry')


def validate_agent(node_id: str, node: dict[str, Any], expected_role: str | None, expected_leader: str | None, require_hot: bool) -> dict[str, Any]:
    code, telemetry = agent_telemetry(node)
    if code != 200:
        raise PrimaryError(f'node {node_id}: Node Agent telemetry unavailable (http={code or 0})')
    n = telemetry.get('node') or {}
    ha = telemetry.get('ha') or {}
    if str(n.get('id', '')) != node_id:
        raise PrimaryError(f'node {node_id}: Agent identity mismatch: {n.get("id")}')
    if str(n.get('bundle_revision', '')) != '57':
        raise PrimaryError(f'node {node_id}: r57 Agent required, got revision={n.get("bundle_revision")}')
    if ha.get('reconciled') is not True or ha.get('control_plane_fenced') is not False:
        raise PrimaryError(f'node {node_id}: Agent is not reconciled/unfenced')
    if expected_role and str(ha.get('role', '')) != expected_role:
        raise PrimaryError(f'node {node_id}: expected Agent role={expected_role}, got {ha.get("role")}')
    if expected_leader and str(ha.get('leader', '')) != expected_leader:
        raise PrimaryError(f'node {node_id}: expected Agent leader={expected_leader}, got {ha.get("leader")}')
    if require_hot and ha.get('hot_start_ready') is not True:
        raise PrimaryError(f'node {node_id}: hot-start is not ready: {ha.get("hot_start_blockers") or []}')
    return telemetry


def configured_edge_on_any(nodes: dict[str, dict[str, Any]]) -> tuple[bool, dict[str, bool]]:
    state: dict[str, bool] = {}
    for nid, node in nodes.items():
        code, telemetry = agent_telemetry(node)
        if code != 200:
            continue
        n = telemetry.get('node') or {}
        if str(n.get('bundle_revision', '')) != '57':
            continue
        state[nid] = bool((telemetry.get('edge') or {}).get('configured', False))
    return any(state.values()), state


def target_lag(member: dict[str, Any]) -> int:
    try:
        return int(member.get('lag', 0) or 0)
    except Exception:
        raise PrimaryError(f'invalid Patroni lag value for target: {member.get("lag")}')


def wait_for_primary(target: str, target_node: dict[str, Any], edge_required: bool, timeout: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    started = time.monotonic()
    last_report = -10.0
    while time.monotonic() < deadline:
        code, telemetry = agent_telemetry(target_node)
        if code == 200:
            last = telemetry
            n = telemetry.get('node') or {}
            ha = telemetry.get('ha') or {}
            edge = telemetry.get('edge') or {}
            observed_revision = str(n.get('bundle_revision', ''))
            if str(n.get('id', '')) == target and observed_revision and observed_revision != '57':
                raise PrimaryError(f'node {target}: r57 Agent required for readiness wait, got revision={observed_revision}; migrate the node first')
            edge_state = edge.get('state') or {}
            elapsed = time.monotonic() - started
            if elapsed - last_report >= 10:
                print(f'[taskforge] waiting {int(elapsed)}s/{timeout}s target={target} '
                      f'role={ha.get("role")} phase={edge_state.get("phase","-")} '
                      f'dns_synced={edge_state.get("dns_synced",False)} '
                      f'route_ready={edge_state.get("route_ready",False)} '
                      f'confirmations={edge_state.get("route_confirmations",0)}/{edge_state.get("route_required",0)} '
                      f'traffic_ready={ha.get("traffic_ready",False)} '
                      f'retry_in={edge_state.get("retry_in_seconds",0)}s', flush=True)
                if edge_state.get('last_error'):
                    print('[taskforge] edge pending: '+str(edge_state['last_error']), flush=True)
                last_report = elapsed
            core_ok = (
                str(n.get('bundle_revision', '')) == '57'
                and str(n.get('id', '')) == target
                and ha.get('reconciled') is True
                and ha.get('control_plane_fenced') is False
                and str(ha.get('role', '')) == 'primary'
                and str(ha.get('leader', '')) == target
                and ha.get('applications_active') is True
            )
            if core_ok:
                if not edge_required:
                    return telemetry
                required = int(edge_state.get('route_required', 0) or 0)
                confirmed = int(edge_state.get('route_confirmations', 0) or 0)
                confirmations_ok = required <= 0 or confirmed >= required
                tls_ok = edge_state.get('tls_ready') is True or edge_state.get('http_only') is True
                edge_ok = (
                    bool(edge.get('configured'))
                    and edge_state.get('status') == 'synced'
                    and edge_state.get('success') is True
                    and edge_state.get('phase') == 'ready'
                    and edge_state.get('route_ready') is True
                    and confirmations_ok
                    and tls_ok
                    and str(edge_state.get('target_node', '')) == target
                )
                if edge_ok and ha.get('traffic_ready') is True:
                    return telemetry
        else:
            elapsed = time.monotonic() - started
            if elapsed - last_report >= 10:
                print(f'[taskforge] waiting {int(elapsed)}s/{timeout}s target={target} Agent unavailable http={code or 0}', flush=True)
                last_report = elapsed
        time.sleep(2)
    ha = last.get('ha') or {}
    edge = last.get('edge') or {}
    raise PrimaryError(
        'the database may already have switched; public readiness was NOT confirmed. '
        'Do not assume rollback or blindly repeat switchover; inspect status/Cloudflare first. '
        f'target={target} role={ha.get("role")} leader={ha.get("leader")} '
        f'applications_active={ha.get("applications_active")} traffic_ready={ha.get("traffic_ready")} '
        f'blockers={ha.get("traffic_blockers")} edge={edge}'
    )


def switch(args: argparse.Namespace) -> int:
    if os.geteuid() != 0:
        raise PrimaryError('run with sudo/root')
    cluster = load_json(Path(args.config))
    env = load_env(Path(args.env_file))
    nodes = node_map(cluster)
    target = str(args.target).strip()
    if target not in nodes:
        raise PrimaryError(f'unknown target node {target}; available={sorted(nodes)}')
    target_node = nodes[target]
    app = target_node.get('app') or {}
    if not app.get('can_be_primary', True):
        raise PrimaryError(f'node {target} is configured with can_be_primary=false')

    runtime_node = Path(args.runtime) / 'node-id'
    if not runtime_node.is_file():
        raise PrimaryError('local node-id missing')
    local_id = runtime_node.read_text(encoding='utf-8').strip()
    if local_id not in nodes:
        raise PrimaryError(f'local node-id {local_id} is not in cluster topology')

    doc, source = cluster_view(nodes, local_id)
    leader = unique_leader(doc)
    if leader not in nodes:
        raise PrimaryError(f'Patroni leader {leader} is not in configured topology')

    edge_required, edge_state = configured_edge_on_any(nodes)
    if edge_required and not edge_state.get(target, False):
        raise PrimaryError(
            f'Cloudflare failover is already configured in the cluster, but target {target} has no r57 edge credential/config; '
            f'configured={sorted(k for k,v in edge_state.items() if v)}. Configure Cloudflare on {target} before moving Primary.'
        )

    if target == leader:
        validate_agent(target, target_node, 'primary', target, False)
        print(f'[taskforge] Primary already {target}; no Patroni switchover required.')
        if edge_required:
            wait_for_primary(target, target_node, True, int(args.timeout))
        print(f'PRIMARY SWITCH: COMPLETE target={target} changed=no profile={(target_node.get("app") or {}).get("profile","full")}')
        return 0

    leader_node = nodes[leader]
    validate_agent(leader, leader_node, 'primary', leader, False)
    target_telemetry = validate_agent(target, target_node, 'standby', leader, True)

    member = member_by_name(doc, target)
    state = str(member.get('state', '')).lower()
    role = str(member.get('role', '')).lower()
    if state not in {'running', 'streaming'} or role not in {'replica', 'standby', 'slave'}:
        raise PrimaryError(f'node {target}: Patroni member is not a healthy replica (role={role} state={state})')
    lag = target_lag(member)
    limit = int(cluster.get('maximum_lag_on_switchover_bytes', cluster.get('maximum_lag_on_failback_bytes', 16_777_216)))
    if lag > limit:
        raise PrimaryError(f'node {target}: replication lag {lag} bytes exceeds manual switchover limit {limit}')

    profile = str((target_node.get('app') or {}).get('profile', 'full'))
    print(f'[taskforge] current Primary={leader}; requested Primary={target}; target_profile={profile}; cluster_view={source}')
    print(f'[taskforge] target preflight PASS: hot_start_ready=true lag={lag} edge_required={str(edge_required).lower()}')

    code, body = post_switchover(leader_node, leader, target, env)
    if code not in {200, 202}:
        raise PrimaryError(f'Patroni rejected switchover {leader} -> {target}: http={code or 0} body={body[:1200]}')
    print(f'[taskforge] Patroni accepted switchover {leader} -> {target} (http={code}).')

    telemetry = wait_for_primary(target, target_node, edge_required, int(args.timeout))
    ha = telemetry.get('ha') or {}
    edge = telemetry.get('edge') or {}
    edge_state_after = edge.get('state') or {}
    print(
        f'[taskforge] confirmed Primary={target}; applications_active={ha.get("applications_active")} '
        f'traffic_ready={ha.get("traffic_ready")} edge={edge_state_after.get("status","unconfigured")}'
    )
    print(f'PRIMARY SWITCH: COMPLETE target={target} changed=yes profile={profile}')
    return 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description='Safely move TaskForge Patroni Primary to a chosen node.')
    ap.add_argument('target')
    ap.add_argument('--config', default=str(DEFAULT_CLUSTER))
    ap.add_argument('--env-file', default=str(DEFAULT_ENV))
    ap.add_argument('--runtime', default=str(DEFAULT_RUNTIME))
    ap.add_argument('--timeout', type=int, default=600)
    return ap


def main() -> int:
    try:
        return switch(parser().parse_args())
    except PrimaryError as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
