"""Bounded, read-only optional SQL diagnostics; no engine credentials are displayed."""
import json
import subprocess


def _container_state(project, service):
    try:
        p = subprocess.run(
            ['docker','inspect','-f','{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}|{{.State.OOMKilled}}', project+'-'+service+'-1'],
            capture_output=True,text=True,timeout=3)
        return p.stdout.strip() if p.returncode == 0 else 'not-prepared'
    except (OSError, subprocess.TimeoutExpired):
        return 'diagnostic-unavailable'


def _healthy(state):
    parts = state.split('|')
    return len(parts) >= 2 and parts[0] == 'running' and parts[1] == 'healthy'


def report(project, node, profile, role):
    if node == 'C' or profile != 'full':
        print('sql_execution=DISABLED_LITE required_for_primary=no'); return
    print('sql_execution=OPTIONAL local_cache=disposable required_for_primary=no')
    states = {}
    for service in ('sql-postgres', 'sql-mysql', 'sql-worker'):
        state = _container_state(project, service)
        states[service] = state
        print(service+'='+state+(' standby-cache-not-required' if role == 'standby' else ''))
    if role != 'primary': return
    worker_state = states.get('sql-worker','')
    try:
        p = subprocess.run(
            ['docker','exec',project+'-sql-worker-1','/app/sql-worker','ready'],
            capture_output=True,text=True,timeout=4)
        if p.returncode == 0:
            data = json.loads(p.stdout)
            print('sql_worker_readiness='+json.dumps(data,separators=(',',':')))
        elif _healthy(worker_state) and ('SQL_UNKNOWN_COMMAND' in p.stderr or 'SQL_UNKNOWN_COMMAND' in p.stdout or p.returncode == 2):
            # Compatibility inherited from r64: the control bundle can be installed before Watchtower has replaced
            # an older healthy sql-worker image. The Docker health probe remains true,
            # but only the new image can expose exact /ready engine details.
            print('sql_worker_readiness=COMPAT_HEALTHY native-ready-command-not-yet-deployed')
        elif _healthy(worker_state):
            print('sql_worker_readiness=DEGRADED native-ready-probe-failed')
        else:
            print('sql_worker_readiness=DEGRADED (does not affect Primary)')
    except subprocess.TimeoutExpired:
        print('sql_worker_readiness=DEGRADED native-ready-probe-timeout')
    except (OSError, ValueError):
        print('sql_worker_readiness=UNKNOWN')
