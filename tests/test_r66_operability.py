#!/usr/bin/env python3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

sql=(ROOT/'cluster/ops/current/sql_status.py').read_text()
assert "'/app/sql-worker','ready'" in sql
assert "'python','-c'" not in sql
assert 'COMPAT_HEALTHY native-ready-command-not-yet-deployed' in sql

agent=(ROOT/'cluster/agent.py').read_text()
clusterctl=(ROOT/'cluster/clusterctl.py').read_text()
assert "DOCKER_CONFIG_ROOT=CONFIG_ROOT/'docker'" in agent
assert "os.environ['DOCKER_CONFIG']=str(DOCKER_CONFIG_ROOT)" in agent
assert "for n in self.cfg['nodes']" in agent
# This exact env key must use all nodes in BOTH the Agent and renderer. If the
# renderer excludes local while the Agent includes it, the shared-state push
# toggles app-routing.env every minute and continuously restarts Watchtower.
render_expr="';'.join(f'http://{n.wg_ip}:{n.health_port}' for n in c.nodes)"
assert clusterctl.count(render_expr) == 2
assert "for n in c.nodes if n.id!=node.id" not in clusterctl

cluster_sh=(ROOT/'cluster.sh').read_text()
assert 'TASKFORGE_DOCKER_CONFIG_DIR' in cluster_sh
assert 'export DOCKER_CONFIG="$TASKFORGE_DOCKER_CONFIG_DIR"' in cluster_sh

compose=(ROOT/'compose.sh').read_text()
assert 'export DOCKER_CONFIG="$CONFIG_ROOT/docker"' in compose
migrate=(ROOT/'cluster/ops/current/migrate.sh').read_text()
assert 'export DOCKER_CONFIG="$TASKFORGE_CONFIG_ROOT/docker"' in migrate
stage=(ROOT/'cluster/ops/current/stage-current-state.sh').read_text()
assert '[ -f "$src" ] && [ -s "$src" ]' in stage

diag=(ROOT/'scripts/diagnostics/collect-taskforge-diagnostics.sh').read_text()
for token in (
    'journal_since="$n $word ago"',
    'journalctl -u taskforge-cluster.service --since "$journal_since"',
    'docker logs --timestamps',
    'docker events "${event_args[@]}"',
    'docker stats --no-stream',
    '--max-log-mb',
    'tail -c "$max_bytes"',
    '--quick',
    '--full-logs',
    'owner="${SUDO_USER:-}"',
    'chown "$owner:$group" "$archive"',
    'safe_labels=',
    'com.centurylinklabs.watchtower.',
):
    assert token in diag, token
for forbidden in (
    'cat /etc/taskforge/taskforge.env',
    'cloudflare-api-token',
    '"Env":',
    '"Labels":cfg.get("Labels",{})',
):
    assert forbidden not in diag, forbidden

edge=(ROOT/'cluster/edge_route.py').read_text()
assert 'len(label) > 63' in edge
assert 'len(label) > 64' not in edge

print('PASS: r66 inherited Watchtower/routing, readiness, diagnostics and Docker-config invariants')
