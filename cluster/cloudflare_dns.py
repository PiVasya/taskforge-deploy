#!/usr/bin/env python3
from __future__ import annotations

import argparse
import getpass
import fcntl
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DEFAULT_CLUSTER = Path('/etc/taskforge/cluster.json')
DEFAULT_ENV = Path('/etc/taskforge/taskforge.env')
DEFAULT_TOKEN = Path('/etc/taskforge/cloudflare-api-token')
DEFAULT_CF_CONFIG = Path('/etc/taskforge/cloudflare-failover.json')
DEFAULT_RUNTIME = Path('/var/lib/taskforge/cluster')
API = 'https://api.cloudflare.com/client/v4'


class CFError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding='utf-8'))


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


def atomic_secret(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent), text=True)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def public_candidates(cluster: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    nodes = sorted(cluster.get('nodes') or [], key=lambda x: int(x.get('priority', 0)), reverse=True)
    for node in nodes:
        app = node.get('app') or {}
        public_ip = str(node.get('public_host', '')).strip()
        if app.get('can_be_primary', True) and public_ip:
            out[str(node.get('id', ''))] = public_ip
    return out


def managed_hosts(env: dict[str, str]) -> list[str]:
    explicit = str(env.get('CLOUDFLARE_FAILOVER_HOSTS', '')).strip()
    raw: list[str]
    if explicit:
        raw = explicit.replace(';', ',').split(',')
    else:
        raw = [str(env.get('DOMAIN', '')), str(env.get('CT_DOMAIN', ''))]
    result: list[str] = []
    for value in raw:
        host = value.strip().rstrip('.').lower()
        if host and host not in result:
            result.append(host)
    return result


class Client:
    def __init__(self, token: str):
        token = token.strip()
        if not token:
            raise CFError('Cloudflare API token is empty')
        self.token = token

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = None
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/json'}
        if payload is not None:
            data = json.dumps(payload, separators=(',', ':')).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        request = urllib.request.Request(API + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read(2_000_000)
        except urllib.error.HTTPError as exc:
            raw = exc.read(2_000_000)
            try:
                detail: Any = json.loads(raw.decode('utf-8', 'replace'))
            except Exception:
                detail = raw.decode('utf-8', 'replace')[:1000]
            raise CFError(f'Cloudflare API HTTP {exc.code}: {detail}')
        except Exception as exc:
            raise CFError(f'Cloudflare API unavailable: {type(exc).__name__}: {exc}')
        try:
            document = json.loads(raw)
        except Exception as exc:
            raise CFError(f'Cloudflare returned invalid JSON: {exc}')
        if not document.get('success', False):
            raise CFError('Cloudflare API rejected request: ' + json.dumps(document.get('errors') or document, ensure_ascii=False))
        return document

    def verify(self) -> dict[str, Any]:
        return self.request('GET', '/user/tokens/verify').get('result') or {}

    def zone_by_name(self, name: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({'name': name, 'status': 'active', 'per_page': 50})
        rows = self.request('GET', '/zones?' + query).get('result') or []
        rows = [row for row in rows if str(row.get('name', '')).rstrip('.').lower() == name.rstrip('.').lower()]
        if len(rows) != 1:
            raise CFError(f'expected exactly one active Cloudflare zone named {name}, got {len(rows)}')
        return rows[0]

    def records_by_name(self, zone_id: str, name: str) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({'name': name, 'per_page': 100})
        return self.request('GET', f'/zones/{zone_id}/dns_records?{query}').get('result') or []

    def record(self, zone_id: str, record_id: str) -> dict[str, Any]:
        return self.request('GET', f'/zones/{zone_id}/dns_records/{record_id}').get('result') or {}

    def patch_content(self, zone_id: str, record_id: str, ip: str) -> dict[str, Any]:
        # PATCH is intentionally partial: all record settings other than content
        # (including proxied, TTL and comments) stay owned by Cloudflare/current DNS.
        return self.request('PATCH', f'/zones/{zone_id}/dns_records/{record_id}', {'content': ip}).get('result') or {}


def exact_one_leader(cluster: dict[str, Any], target: str) -> dict[str, Any]:
    node = next((n for n in cluster.get('nodes', []) if str(n.get('id')) == target), None)
    if not node:
        raise CFError(f'unknown node {target}')
    ip = str((node.get('wireguard') or {}).get('ip', ''))
    port = int((node.get('postgres') or {}).get('patroni_rest_port', 8008))

    def get(path: str) -> tuple[int, dict[str, Any]]:
        try:
            with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(f'http://{ip}:{port}{path}', timeout=3) as response:
                return int(response.status), json.loads(response.read(1_000_000))
        except Exception as exc:
            raise CFError(f'cannot prove Patroni state on {target}: {type(exc).__name__}: {exc}')

    # /primary additionally checks the Patroni leader lock; /patroni alone does not.
    code, patroni = get('/primary')
    role = str(patroni.get('role', '')).lower()
    state = str(patroni.get('state', '')).lower()
    if code != 200 or role not in {'primary', 'master', 'leader'} or state != 'running':
        raise CFError(f'node {target} is not a healthy Patroni primary (http={code} role={role} state={state})')
    code, cluster_doc = get('/cluster')
    leaders = [str(m.get('name', '')) for m in cluster_doc.get('members', []) if str(m.get('role', '')).lower() == 'leader']
    if code != 200 or leaders != [target]:
        raise CFError(f'Patroni does not report one authoritative leader {target}: {leaders}')
    if patroni.get('name') and str(patroni['name']) != target:
        raise CFError('Patroni endpoint identity mismatch')
    return patroni


def choose_record_plan(client: Client, zone_id: str, hosts: list[str], candidates: dict[str, str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    managed: list[dict[str, Any]] = []
    dependent: list[dict[str, Any]] = []
    allowed_ips = set(candidates.values())
    hostset = {host.rstrip('.').lower() for host in hosts}
    for host in hosts:
        rows = client.records_by_name(zone_id, host)
        rows = [row for row in rows if str(row.get('name', '')).rstrip('.').lower() == host.rstrip('.').lower()]
        a_records = [row for row in rows if row.get('type') == 'A']
        aaaa_records = [row for row in rows if row.get('type') == 'AAAA']
        cname_records = [row for row in rows if row.get('type') == 'CNAME']
        if aaaa_records:
            raise CFError(f'{host}: AAAA origin record exists; IPv4 origin failover would be ambiguous')
        if len(a_records) == 1 and not cname_records:
            record = a_records[0]
            if record.get('proxied') is not True:
                raise CFError(f'{host}: A record must be Cloudflare proxied for this failover design')
            if str(record.get('content', '')) not in allowed_ips:
                raise CFError(f'{host}: current origin {record.get("content")} is not one of public candidates {sorted(allowed_ips)}')
            managed.append({'id': str(record['id']), 'name': host, 'type': 'A', 'proxied': True})
            continue
        if not a_records and len(cname_records) == 1:
            cname = cname_records[0]
            target = str(cname.get('content', '')).rstrip('.').lower()
            if cname.get('proxied') is not True:
                raise CFError(f'{host}: managed CNAME must be Cloudflare proxied')
            if target not in hostset:
                raise CFError(f'{host}: CNAME points to {target}; refusing to assume it follows a managed origin')
            dependent.append({'id': str(cname['id']), 'name': host, 'type': 'CNAME', 'target': target, 'proxied': True})
            continue
        raise CFError(f'{host}: expected one proxied A record or one CNAME to another managed hostname; got A={len(a_records)} CNAME={len(cname_records)}')
    if not managed:
        raise CFError('no direct A record selected for Cloudflare failover')
    direct = {row['name'] for row in managed}
    links = {row['name']: row['target'] for row in dependent}
    for host in links:
        seen = set()
        current = host
        while current not in direct:
            if current in seen or current not in links:
                raise CFError(f'{host}: CNAME chain does not resolve to a managed A record')
            seen.add(current)
            current = links[current]
    return managed, dependent


def setup(args: argparse.Namespace) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise CFError('setup must run as root/sudo')
    cluster = load_json(Path(args.config))
    env = load_env(Path(args.env_file))
    candidates = public_candidates(cluster)
    if len(candidates) < 2:
        raise CFError(f'need at least two public primary candidates, got {candidates}')
    runtime_node = Path(args.runtime) / 'node-id'
    if not runtime_node.is_file():
        raise CFError('local node-id is missing; configure Cloudflare only on an installed cluster node')
    local_id = runtime_node.read_text(encoding='utf-8').strip()
    if local_id not in candidates:
        raise CFError(f'Cloudflare edge credential is only installed on public primary candidates {sorted(candidates)}; local node={local_id}')

    domain = str(env.get('DOMAIN', '')).strip().rstrip('.').lower()
    zone_name = str(env.get('CLOUDFLARE_ZONE', domain)).strip().rstrip('.').lower()
    zone_id = str(env.get('CLOUDFLARE_ZONE_ID', '')).strip()
    hosts = managed_hosts(env)
    cert_hosts = {str(env.get(k, '')).strip().rstrip('.').lower() for k in ('DOMAIN', 'CT_DOMAIN')} - {''}
    if not cert_hosts.issubset(set(hosts)):
        raise CFError('CLOUDFLARE_FAILOVER_HOSTS must include DOMAIN and CT_DOMAIN')
    if not domain or not zone_name or not hosts:
        raise CFError('DOMAIN/CLOUDFLARE_ZONE/failover hosts are missing')

    if args.token_file:
        token = Path(args.token_file).read_text(encoding='utf-8').strip()
    elif os.environ.get('CLOUDFLARE_API_TOKEN'):
        token = os.environ['CLOUDFLARE_API_TOKEN'].strip()
    else:
        token = getpass.getpass('Cloudflare API token: ').strip()

    client = Client(token)
    verify = client.verify()
    if str(verify.get('status', '')).lower() != 'active':
        raise CFError(f'Cloudflare token status is not active: {verify}')
    if not zone_id:
        try:
            zone_id = str(client.zone_by_name(zone_name).get('id', ''))
        except CFError as exc:
            raise CFError(f'{exc}. Add Zone:Zone:Read to the token or set CLOUDFLARE_ZONE_ID in /etc/taskforge/taskforge.env')
    if not zone_id:
        raise CFError('Cloudflare zone id is empty')

    managed, dependent = choose_record_plan(client, zone_id, hosts, candidates)
    document = {
        'schema_version': 1,
        'provider': 'cloudflare-dns',
        'zone_name': zone_name,
        'zone_id': zone_id,
        'records': managed,
        'dependent_hosts': dependent,
        'managed_hosts': hosts,
        'public_candidates': candidates,
        'configured_at': utc_now(),
    }
    atomic_secret(Path(args.token_path), token + '\n')
    atomic_secret(Path(args.cf_config), json.dumps(document, ensure_ascii=False, indent=2) + '\n')
    return {
        'success': True,
        'configured': True,
        'local_node': local_id,
        'zone': zone_name,
        'records': managed,
        'dependent_hosts': dependent,
        'public_candidates': candidates,
        'token_status': 'active',
    }


def load_installed(args: argparse.Namespace) -> tuple[Client | None, dict[str, Any] | None]:
    token_path = Path(args.token_path)
    config_path = Path(args.cf_config)
    if any(p.exists() and not p.is_file() for p in (token_path, config_path)):
        raise CFError('Cloudflare token/config must be regular files, not directories')
    if token_path.exists() != config_path.exists():
        raise CFError('partial Cloudflare setup: token and configuration must both exist')
    if not token_path.is_file() or not config_path.is_file():
        return None, None
    token = token_path.read_text(encoding='utf-8').strip()
    return Client(token), load_json(config_path)


def assert_layout_unchanged(client: Client, config: dict[str, Any]) -> None:
    hosts = [str(x).rstrip('.').lower() for x in (config.get('managed_hosts') or []) if str(x).strip()]
    if not hosts:
        hosts = [str(x.get('name', '')).rstrip('.').lower() for x in (config.get('records') or [])]
        hosts += [str(x.get('name', '')).rstrip('.').lower() for x in (config.get('dependent_hosts') or [])]
        hosts = list(dict.fromkeys(x for x in hosts if x))
    live_managed, live_dependent = choose_record_plan(
        client,
        str(config.get('zone_id', '')),
        hosts,
        {str(k): str(v) for k, v in (config.get('public_candidates') or {}).items()},
    )
    def managed_key(rows):
        return sorted((str(x.get('id', '')), str(x.get('name', '')).rstrip('.').lower(), str(x.get('type', ''))) for x in rows)
    def dependent_key(rows):
        return sorted((str(x.get('id', '')), str(x.get('name', '')).rstrip('.').lower(), str(x.get('type', '')), str(x.get('target', '')).rstrip('.').lower()) for x in rows)
    if managed_key(live_managed) != managed_key(config.get('records') or []):
        raise CFError(f'managed direct DNS layout changed; rerun setup. configured={config.get("records")} live={live_managed}')
    if dependent_key(live_dependent) != dependent_key(config.get('dependent_hosts') or []):
        raise CFError(f'managed CNAME layout changed; rerun setup. configured={config.get("dependent_hosts")} live={live_dependent}')


def inspect_records(client: Client, config: dict[str, Any], strict: bool = True) -> list[dict[str, Any]]:
    zone_id = str(config.get('zone_id', ''))
    allowed = set((config.get('public_candidates') or {}).values())
    out: list[dict[str, Any]] = []
    for spec in config.get('records') or []:
        record = client.record(zone_id, str(spec.get('id', '')))
        if str(record.get('name', '')).rstrip('.').lower() != str(spec.get('name', '')).rstrip('.').lower() or record.get('type') != 'A':
            raise CFError(f'record identity changed for {spec}')
        if record.get('proxied') is not True:
            raise CFError(f'{record.get("name")}: failover record is no longer proxied')
        ip = str(record.get('content', ''))
        if strict and ip not in allowed:
            raise CFError(f'{record.get("name")}: current origin {ip} is outside configured candidates')
        out.append({'id': str(record.get('id', '')), 'name': str(record.get('name', '')), 'content': ip, 'proxied': True})
    if not out:
        raise CFError('configured Cloudflare failover contains no direct A record')
    return out


def routing_state(config: dict[str, Any], records: list[dict[str, Any]]) -> tuple[list[str], bool]:
    by_ip = {ip: node for node, ip in (config.get('public_candidates') or {}).items()}
    nodes = sorted({by_ip.get(record['content'], 'external') for record in records})
    return nodes, len(nodes) == 1 and nodes[0] in (config.get('public_candidates') or {})


def status(args: argparse.Namespace) -> dict[str, Any]:
    client, config = load_installed(args)
    if not client or not config:
        return {'success': True, 'configured': False, 'reason': 'token-or-config-missing'}
    try:
        verify = client.verify()
        assert_layout_unchanged(client, config)
        records = inspect_records(client, config, strict=False)
        nodes, in_sync = routing_state(config, records)
        return {
            'success': True,
            'configured': True,
            'token_status': verify.get('status'),
            'zone': config.get('zone_name'),
            'records': records,
            'dependent_hosts': config.get('dependent_hosts') or [],
            'routed_nodes': nodes,
            'in_sync': in_sync,
        }
    except Exception as exc:
        return {'success': False, 'configured': True, 'error': str(exc)}


def preflight(args: argparse.Namespace) -> dict[str, Any]:
    client, config = load_installed(args)
    if not client or not config:
        raise CFError('Cloudflare failover is not configured')
    verify = client.verify()
    if str(verify.get('status', '')).lower() != 'active':
        raise CFError(f'token status={verify.get("status")}')
    assert_layout_unchanged(client, config)
    rows = inspect_records(client, config, strict=True)
    nodes, in_sync = routing_state(config, rows)
    if not in_sync:
        raise CFError(f'managed DNS records disagree about current origin: {rows}')
    return {
        'success': True,
        'configured': True,
        'zone': config.get('zone_name'),
        'records': rows,
        'routed_nodes': nodes,
        'public_candidates': config.get('public_candidates'),
    }


def reconcile(args: argparse.Namespace, guard=None) -> dict[str, Any]:
    runtime = Path(args.runtime)
    if not runtime.is_dir():
        raise CFError('local runtime is missing')
    with (runtime / 'cloudflare-dns.lock').open('a') as handle:
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise CFError('another local Cloudflare DNS update is in progress') from exc
        try:
            return _reconcile(args, guard)
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _reconcile(args: argparse.Namespace, guard=None) -> dict[str, Any]:
    client, config = load_installed(args)
    if not client or not config:
        raise CFError('Cloudflare failover is not configured')
    cluster = load_json(Path(args.config))
    live_candidates = public_candidates(cluster)
    candidates = {str(k): str(v) for k, v in (config.get('public_candidates') or {}).items()}
    if not candidates:
        raise CFError('configured Cloudflare failover has no public candidates; rerun setup')
    drift = {node: {'configured': ip, 'live': live_candidates.get(node)} for node, ip in candidates.items() if live_candidates.get(node) != ip}
    if set(candidates) != set(live_candidates):
        raise CFError('public candidate set changed; rerun Cloudflare setup on all nodes')
    if drift:
        raise CFError(f'configured public candidate topology changed; rerun setup. drift={drift}')
    target = str(args.node)
    if target not in candidates:
        if target in live_candidates:
            raise CFError(f'node {target} is a live primary candidate but is not present in this Cloudflare setup; rerun setup on this node')
        raise CFError(f'node {target} is not a configured public failover candidate')
    runtime_node = Path(args.runtime) / 'node-id'
    if not runtime_node.is_file() or runtime_node.read_text(encoding='utf-8').strip() != target:
        raise CFError(f'local node-id does not match requested target {target}')

    extra_guard = guard
    def check():
        if any((Path(args.runtime) / name).exists() for name in ('join-rehome-fence', 'migration-fence')):
            raise CFError('migration fence is armed; public DNS must not be changed')
        if extra_guard is not None:
            extra_guard()
        else:
            exact_one_leader(cluster, target)

    check()
    # DNS layout is revalidated on every reconciliation, so a newly added AAAA,
    # duplicate A, de-proxied record, or changed CNAME cannot silently bypass HA.
    assert_layout_unchanged(client, config)
    # Independent proof inside the edge controller: Agent state alone is never
    # enough authority to change public DNS.
    check()
    before = inspect_records(client, config, strict=True)
    target_ip = candidates[target]
    changed: list[str] = []
    changed_rows: list[dict[str, Any]] = []
    try:
        for row in before:
            if row['content'] == target_ip:
                continue
            check()
            # Track the attempted write too: a lost API response may have committed.
            changed_rows.append(row)
            result = client.patch_content(str(config['zone_id']), row['id'], target_ip)
            check()
            if str(result.get('content', '')) != target_ip or result.get('proxied') is not True:
                raise CFError(f'Cloudflare update did not preserve expected proxied A state for {row["name"]}')
            changed.append(row['name'])
        check()
        assert_layout_unchanged(client, config)
        verified = inspect_records(client, config, strict=True)
        wrong = [row for row in verified if row['content'] != target_ip]
        if wrong:
            raise CFError(f'Cloudflare records did not converge to {target}: {wrong}')
    except Exception as exc:
        rollback_errors: list[str] = []
        # Best-effort rollback prevents taskforge.by and ct.taskforge.by from
        # intentionally being left on different origins after a partial update.
        for old in reversed(changed_rows):
            try:
                check()  # Never let an old Primary roll back a new Primary's DNS.
                live = client.record(str(config['zone_id']), old['id'])
                if str(live.get('content', '')) == target_ip:
                    check()
                    client.patch_content(str(config['zone_id']), old['id'], old['content'])
            except Exception as rollback_exc:
                rollback_errors.append(f'{old["name"]}: {rollback_exc}')
        suffix = '' if not rollback_errors else '; rollback incomplete: ' + ' | '.join(rollback_errors)
        raise CFError(f'DNS switch to {target} failed after changing {changed}: {exc}{suffix}')

    check()
    return {
        'success': True,
        'configured': True,
        'target_node': target,
        'target_ip': target_ip,
        'changed': changed,
        'records': verified,
        'reason': args.reason,
        'at': utc_now(),
    }


def wait_ready(args: argparse.Namespace) -> dict[str, Any]:
    import primary
    client, document = load_installed(args)
    if not client or not document:
        raise CFError('Cloudflare is not configured on this node; run cloudflare setup first')
    node_id = (Path(args.runtime) / 'node-id').read_text().strip()
    cluster = load_json(Path(args.config))
    node = next(n for n in cluster['nodes'] if str(n['id']) == node_id)
    exact_one_leader(cluster, node_id)
    telemetry = primary.wait_for_primary(node_id, node, True, int(args.timeout))
    return {'success': True, 'node': node_id,
            'traffic_ready': bool((telemetry.get('ha') or {}).get('traffic_ready')),
            'edge': telemetry.get('edge')}


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', default=str(DEFAULT_CLUSTER))
    parser.add_argument('--env-file', default=str(DEFAULT_ENV))
    parser.add_argument('--token-path', default=str(DEFAULT_TOKEN))
    parser.add_argument('--cf-config', default=str(DEFAULT_CF_CONFIG))
    parser.add_argument('--runtime', default=str(DEFAULT_RUNTIME))
    sub = parser.add_subparsers(dest='cmd', required=True)
    setup_parser = sub.add_parser('setup')
    setup_parser.add_argument('--token-file')
    sub.add_parser('status')
    sub.add_parser('preflight')
    wait_parser = sub.add_parser('wait')
    wait_parser.add_argument('--timeout', type=int, default=600)
    reconcile_parser = sub.add_parser('reconcile')
    reconcile_parser.add_argument('--node', required=True)
    reconcile_parser.add_argument('--reason', default='agent')
    return parser


def main() -> int:
    args = parser().parse_args()
    try:
        if args.cmd == 'setup':
            out = setup(args)
        elif args.cmd == 'status':
            out = status(args)
        elif args.cmd == 'preflight':
            out = preflight(args)
        elif args.cmd == 'wait':
            out = wait_ready(args)
        elif args.cmd == 'reconcile':
            out = reconcile(args)
        else:
            raise CFError('unknown command')
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0 if out.get('success', False) else 3
    except Exception as exc:
        print(json.dumps({'success': False, 'error': str(exc)}, ensure_ascii=False, indent=2))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
