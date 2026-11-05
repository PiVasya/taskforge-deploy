#!/usr/bin/env python3
from __future__ import annotations
import json
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path('/opt/taskforge/control')
RUNTIME = Path('/var/lib/taskforge/cluster')
CLUSTER = Path('/etc/taskforge/cluster.json')
ENV = Path('/etc/taskforge/taskforge.env')


def env_values():
    out = {}
    if ENV.is_file():
        for raw in ENV.read_text(encoding='utf-8-sig').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            out[key.strip()] = value.strip().strip('"').strip("'")
    return out


def http_json(url: str):
    try:
        with urllib.request.urlopen(url, timeout=2.5) as response:
            raw = response.read()
            return response.status, json.loads(raw.decode()) if raw else {}
    except Exception:
        return 0, {}


def pg_role(project: str):
    name = f'{project}-postgres-1'
    proc = subprocess.run(
        ['docker', 'exec', name, 'sh', '-lc', 'psql -X -U "$POSTGRES_USER" -d postgres -Atc "select pg_is_in_recovery()"'],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    value = proc.stdout.strip() if proc.returncode == 0 else ''
    return 'primary' if value == 'f' else 'replica' if value == 't' else 'unknown'


def main():
    cfg = json.loads(CLUSTER.read_text(encoding='utf-8')) if CLUSTER.is_file() else {}
    node_id = (RUNTIME / 'node-id').read_text().strip() if (RUNTIME / 'node-id').is_file() else '?'
    node = next((n for n in cfg.get('nodes', []) if str(n.get('id')) == node_id), {})
    ip = str((node.get('wireguard') or {}).get('ip', ''))
    health_port = int(node.get('health_port', 9187) or 9187)
    patroni_port = int((node.get('postgres') or {}).get('patroni_rest_port', 8008) or 8008)
    mode = 'quorum' if (RUNTIME / 'enabled').is_file() else 'replica'
    env = env_values()
    project = env.get('COMPOSE_PROJECT_NAME', 'taskforge-prod')
    prole = pg_role(project)
    ph, patroni = http_json(f'http://{ip}:{patroni_port}/patroni') if ip else (0, {})
    ah, agent = http_json(f'http://{ip}:{health_port}/ha/telemetry') if ip else (0, {})
    ha = agent.get('ha', {}) if isinstance(agent, dict) else {}
    n = agent.get('node', {}) if isinstance(agent, dict) else {}
    edge = agent.get('edge', {}) if isinstance(agent, dict) else {}
    edge_state = edge.get('state') or {}
    print('========== TASKFORGE v41.5.9 STABLE STATUS ==========')
    print(f'node={node_id} deployment_mode={mode} postgres={prole}')
    print(f"patroni_http={ph} patroni_role={patroni.get('role','-') if isinstance(patroni,dict) else '-'}")
    print(f"agent_http={ah} agent_version={n.get('bundle_version','-')} agent_revision={n.get('bundle_revision','-')} reconciled={ha.get('reconciled',False)} role={ha.get('role','unknown')} leader={ha.get('leader','-')} traffic_ready={ha.get('traffic_ready',False)} hot_ready={ha.get('hot_start_ready',False)} local_cert_ready={ha.get('tls_assets_ready',False)} tls_live={ha.get('tls_live_ready',False)}")
    print(f"edge_provider={edge.get('provider','cloudflare-dns')} edge_configured={edge.get('configured',False)} edge_status={edge_state.get('status','-')} edge_target={edge_state.get('target_node','-')} edge_changed={edge_state.get('changed',[])}")
    print(f"edge_phase={edge_state.get('phase','-')} dns_synced={edge_state.get('dns_synced',False)} public_route_ready={edge_state.get('route_ready',False)} edge_tls_ready={edge_state.get('tls_ready',False)} confirmations={edge_state.get('route_confirmations',0)}/{edge_state.get('route_required',0)} retry_in_seconds={edge_state.get('retry_in_seconds',0)}")
    if edge_state.get('last_error'):
        print('edge_last_error='+str(edge_state['last_error']))
    if edge_state.get('warning'):
        print('edge_warning='+str(edge_state['warning']))
    print(f"traffic_blockers={ha.get('traffic_blockers',[])} hot_blockers={ha.get('hot_start_blockers',[])} degraded={ha.get('degraded_services',[])}")
    peer_health=agent.get('ha_health') or {}
    print('ha_connectivity=quorum:{quorum} healthy_voters:{healthy}/{voters} sampled:{sampled}'.format(
        quorum=peer_health.get('quorum_observed'),healthy=peer_health.get('healthy_voters_observed'),
        voters=peer_health.get('voters'),sampled=peer_health.get('sampled_at')))
    for peer in peer_health.get('nodes',[]):
        if peer.get('failed_checks'):
            print('ha_peer='+str(peer.get('node'))+' failures='+str(peer.get('failed_checks'))+
                  ' confirmed='+str(peer.get('confirmed_degraded'))+' samples='+str(peer.get('failure_samples'))+
                  ' seconds='+str(peer.get('failure_seconds')))
    policy_file=RUNTIME/'ha-timing-apply.json'
    if policy_file.is_file():
        try:
            policy=json.loads(policy_file.read_text())
            print('ha_timing_apply='+str(policy.get('status'))+' target='+str(policy.get('after')))
        except (ValueError,OSError):pass
    update = agent.get('update') or {}
    host = agent.get('host') or {}
    print(f"updater={update.get('mode','unknown')} running={update.get('running')} phase={update.get('status','-')} current_service={update.get('current_service','-')} pending={update.get('pending_count')} failed={update.get('failed_count')} last_scan={update.get('last_scan_at')} registry_active={update.get('registry_active')} registry_stalled={update.get('registry_stalled')} registry_thread_alive={update.get('registry_thread_alive')}")
    compose_calls = update.get('compose_calls') or {}
    if compose_calls:
        print('updater_compose=config:{config} prepare:{prepare} force_recreate:{force_recreate} reference_cache_hits:{reference_cache_hits}'.format(
            config=compose_calls.get('config', 0), prepare=compose_calls.get('prepare', 0),
            force_recreate=compose_calls.get('force_recreate', 0),
            reference_cache_hits=compose_calls.get('reference_cache_hits', 0)))
    observe = update.get('docker_observe') or {}
    if observe:
        print('updater_docker_inspect=fallbacks:{fallback_calls} recovered:{fallback_successes} missing:{missing_confirmed} error:{last_error} trigger:{last_fallback_reason}'.format(
            fallback_calls=observe.get('fallback_calls', 0), fallback_successes=observe.get('fallback_successes', 0),
            missing_confirmed=observe.get('missing_confirmed', 0), last_error=observe.get('last_error') or '-',
            last_fallback_reason=observe.get('last_fallback_reason') or '-'))
    print('updater_prepare=verified:{verified} mismatch:{mismatch} maintenance:{maintenance}'.format(
        verified=update.get('prepare_verified_count', 0), mismatch=update.get('prepare_mismatch_count', 0),
        maintenance=bool(update.get('maintenance_ack_token'))))
    failures=[f"{row.get('service')}:{row.get('pull_error') or row.get('error')}" for row in update.get('services',[]) if row.get('error') or row.get('pull_error')]
    if update.get('error'):
        failures.append('updater:'+str(update['error']))
    if update.get('registry_thread_alive') is False:
        failures.append('updater:registry-thread-exited')
    if failures:
        print('update_degraded='+str(failures))
    elif update.get('mode')=='node-agent':
        print('update_degraded=[]')
    print(f"host_sample={host.get('sampled_at')} cpu_percent={host.get('cpu_percent')} memory_percent={(host.get('memory') or {}).get('percent')} temperature_c={host.get('temperature_c')} steal_percent={host.get('steal_percent')}")
    from sql_status import report
    report(project,node_id,str((node.get('app') or {}).get('profile','full')),ha.get('role','unknown'))
    print(f'runtime={RUNTIME} config=/etc/taskforge control={ROOT}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
