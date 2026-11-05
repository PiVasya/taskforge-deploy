#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh migrate' >&2; exit 2; }
TASKFORGE_CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
mkdir -p "$TASKFORGE_CONFIG_ROOT/docker"
export DOCKER_CONFIG="$TASKFORGE_CONFIG_ROOT/docker"
[ "$(tr -d '\r\n' < "$ROOT/REVISION" 2>/dev/null || true)" = 410509 ] || { echo 'error: migrate must run from unpacked v41.5.9' >&2; exit 2; }
exec 9>/run/lock/taskforge-server-migrate.lock
flock -n 9 || { echo 'error: another TaskForge migration is running' >&2; exit 2; }
LOGDIR=/var/log/taskforge; mkdir -p "$LOGDIR"; LOG="$LOGDIR/migrate-v41.5.9-$(date -u +%Y%m%dT%H%M%SZ).log"; : > "$LOG"; chmod 600 "$LOG"
SOURCE_REV_BEFORE="$(tr -d '\r\n' < /opt/taskforge/control/REVISION 2>/dev/null || true)"; [ -n "$SOURCE_REV_BEFORE" ] || SOURCE_REV_BEFORE=legacy-or-versioned
step=0; total=18; last=startup
fail(){ rc="$1"; echo; echo 'v41.5.9 MIGRATION: FAIL'; echo "step=$last rc=$rc"; echo "log=$LOG"; echo; tail -n 50 "$LOG" 2>/dev/null | cut -c 1-480 || true; echo; echo 'No persistent PostgreSQL/MinIO/RabbitMQ/Redis/etcd Docker volume is deleted or recreated by v41.5.9 migration.'; echo 'Fix the reported blocker and run the SAME command again:'; echo '  sudo bash ./cluster.sh migrate'; exit "$rc"; }
run(){ title="$1"; shift; step=$((step+1)); last="$title"; printf '[%d/%d] %s ... ' "$step" "$total" "$title"; printf '\n========== %s ==========%s\n' "$title" "$(date -u +%FT%TZ)" >> "$LOG"; set +e; "$@" >> "$LOG" 2>&1; rc=$?; set -e; if [ "$rc" -ne 0 ]; then echo "FAIL (rc=$rc)"; fail "$rc"; fi; echo PASS; }
run_stream(){ title="$1"; shift; step=$((step+1)); last="$title"; printf '[%d/%d] %s ...\n' "$step" "$total" "$title"; printf '\n========== %s ==========%s\n' "$title" "$(date -u +%FT%TZ)" >> "$LOG"; set +e; "$@" 2>&1 | tee -a "$LOG"; rc=${PIPESTATUS[0]}; set -e; if [ "$rc" -ne 0 ]; then echo "FAIL (rc=$rc)"; fail "$rc"; fi; echo "[${step}/${total}] $title ... PASS"; }

package_integrity(){
  (cd "$ROOT" && sha256sum -c RELEASE_MANIFEST.sha256 >/dev/null)
  bash -n "$ROOT/cluster.sh" "$ROOT/compose.sh" "$ROOT/cluster/ops/current/"*.sh "$ROOT/cluster/ops/quorum/"*.sh
  python3 -S - "$ROOT" <<'PY'
from pathlib import Path
import sys
for p in Path(sys.argv[1]).rglob('*.py'):
    compile(p.read_text(encoding='utf-8'),str(p),'exec')
PY
  [ ! -e "$ROOT/compat-r48.tar.gz" ] || { echo 'error: v41.5.9 must not contain an embedded historical migration release' >&2; return 2; }
  [ ! -e "$ROOT/cluster/ops/current/compat-r47-bootstrap.sh" ] || { echo 'error: historical compatibility bootstrap must not exist in v41.5.9' >&2; return 2; }
}

stage_state(){ bash "$ROOT/cluster/ops/current/stage-current-state.sh"; }
quiesce_application_updates(){
  # No application or database container is stopped here. Patroni keeps running.
  # This independent fence survives re-home helpers clearing their own fences.
  install -m 600 /dev/null /var/lib/taskforge/cluster/application-updates-paused
  if [ "$(systemctl show taskforge-cluster.service -p LoadState --value 2>/dev/null || true)" != not-found ]; then
    systemctl stop taskforge-cluster.service
  fi
  local project
  project="$(bash -c 'source /etc/taskforge/taskforge.env; printf "%s" "${COMPOSE_PROJECT_NAME:-taskforge-prod}"')"
  if docker inspect "$project-watchtower-1" >/dev/null 2>&1; then
    # Disable restart BEFORE stopping, preventing the old controller racing us.
    docker update --restart=no "$project-watchtower-1" >/dev/null
    if [ "$(docker inspect -f '{{.State.Status}}' "$project-watchtower-1")" = running ]; then
      docker stop -t 20 "$project-watchtower-1" >/dev/null
    fi
    # No -v: never remove named/bind volumes. The stale disabled object only
    # confuses Docker dashboards and out-of-date cluster verifiers.
    docker rm "$project-watchtower-1" >/dev/null
  fi
  echo 'application updater paused; database containers unchanged'
}
normalize_policy(){ python3 "$ROOT/cluster/ops/current/normalize-primary-policy.py"; }
quiesce_incompatible_mysql(){
  local cid logs
  cid="$(docker ps -aq --filter name=taskforge-prod-sql-mysql-1 | head -n1)"
  [ -n "$cid" ] || return 0
  logs="$(docker logs --tail 20 "$cid" 2>&1 || true)"
  if printf '%s' "$logs" | grep -Fq 'CPU does not support x86-64-v2'; then
    echo '[TFSQL] detected incompatible x86-64-v2 MySQL crash loop; stopping disposable cache before repin'
    docker update --restart=no "$cid" >/dev/null 2>&1 || true
    docker stop "$cid" >/dev/null 2>&1 || true
  fi
}

prepare_sql(){
  quiesce_incompatible_mysql
  local node profile
  node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"
  profile="$(python3 -S - /etc/taskforge/cluster.json "$node" <<'PYPROFILE'
import json,sys
n=next(n for n in json.load(open(sys.argv[1]))['nodes'] if str(n['id'])==sys.argv[2])
print((n.get('app') or {}).get('profile','full'))
PYPROFILE
)"
  [ "$node" != C ] || profile=lite
  python3 "$ROOT/scripts/sql/prepare-runtime.py" --env-file /etc/taskforge/sql-runtime.env --base-env /etc/taskforge/taskforge.env --node "$node" --profile "$profile" --pull
  if grep -q '^SQL_ENABLED=true$' /etc/taskforge/sql-runtime.env; then
    TASKFORGE_CLUSTER_FORCE=1 TASKFORGE_CONTROL_ROOT="$ROOT" bash "$ROOT/compose.sh" pull sql-worker
  fi
}
config_preflight(){
  node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"; case "$node" in A|B|C) ;; *) echo "error: invalid node-id=$node" >&2; return 2;; esac
  python3 "$ROOT/cluster/clusterctl.py" --config /etc/taskforge/cluster.json --env-file /etc/taskforge/taskforge.env validate
  TASKFORGE_CLUSTER_RUNTIME=/var/lib/taskforge/cluster python3 "$ROOT/cluster/clusterctl.py" --config /etc/taskforge/cluster.json --env-file /etc/taskforge/taskforge.env render --node "$node" >/dev/null
  TASKFORGE_CLUSTER_FORCE=1 TASKFORGE_CONTROL_ROOT="$ROOT" TASKFORGE_STATE_ROOT=/var/lib/taskforge TASKFORGE_CONFIG_ROOT=/etc/taskforge TASKFORGE_ENV_FILE=/etc/taskforge/taskforge.env bash "$ROOT/compose.sh" config -q
  echo "node=$node state_model=live-facts source_revision=$SOURCE_REV_BEFORE"
}
install_control(){ bash "$ROOT/cluster/ops/current/install-control.sh"; }
rehome_actual_state(){ bash /opt/taskforge/control/cluster/ops/current/rehome-node.sh; }
apply_onec_runtime(){ bash /opt/taskforge/control/cluster/ops/current/apply-onec-runtime.sh; }
restart_agent(){ rm -f /var/lib/taskforge/cluster/application-updates-paused; bash /opt/taskforge/control/cluster/ops/quorum/install-service.sh --no-start; systemctl restart taskforge-cluster.service; }
reconcile_sql(){ TASKFORGE_SQL_MIGRATION_SOFT=1 TASKFORGE_CONTROL_ROOT=/opt/taskforge/control bash /opt/taskforge/control/cluster/ops/current/reconcile-sql.sh; }
wait_release(){
  node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"
  read -r ip hp <<<"$(python3 -S - /etc/taskforge/cluster.json "$node" <<'PY'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2]);print(n['wireguard']['ip'],n.get('health_port',9187))
PY
)"
  last=''
  for i in $(seq 1 90); do
    last="$(curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$hp/ha/telemetry" 2>/dev/null || true)"
    if NODE="$node" python3 -S - 3<<<"$last" <<'PY'
import json,os
try:t=json.load(os.fdopen(3))
except Exception:raise SystemExit(1)
n=t.get('node') or {};h=t.get('ha') or {}
ok=str(n.get('id',''))==os.environ['NODE'] and str(n.get('bundle_revision',''))=='410509' and h.get('reconciled') is True and h.get('control_plane_fenced') is False and h.get('role') in {'primary','standby'} and bool(str(h.get('leader','')).strip())
raise SystemExit(0 if ok else 1)
PY
    then echo "agent_reconciled=PASS node=$node revision=410509 telemetry_bytes=${#last}"; return 0; fi
    [ $((i%10)) -ne 0 ] || echo "waiting_for_release_agent=${i}s" >&2
    sleep 1
  done
  echo "agent_reconcile_timeout telemetry_bytes=${#last} (full telemetry suppressed)" >&2; return 2
}
updater_ownership_proof(){
  local node ip hp i
  node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"
  read -r ip hp <<<"$(python3 -S - /etc/taskforge/cluster.json "$node" <<'PY_NODE'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2])
print(n['wireguard']['ip'],n.get('health_port',9187))
PY_NODE
)"
  for i in $(seq 1 75); do
    if curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$hp/ha/telemetry" 2>/dev/null | python3 -S -c '
import json,sys
try:
  u=json.load(sys.stdin).get("update") or {}
  ok=(u.get("mode")=="node-agent" and u.get("running") is True and
      u.get("registry_thread_alive") is True and
      u.get("lifecycle_thread_alive") is True and
      u.get("status") not in {"taking-ownership","initializing","blocked","fenced","waiting-role"} and
      not str(u.get("error") or "").startswith("legacy-owner-check-failed:"))
except (ValueError,TypeError):ok=False
raise SystemExit(0 if ok else 1)
'; then
      echo "node_agent_update_owner=PASS node=$node (Watchtower disabled)"
      return 0
    fi
    sleep 1
  done
  echo 'error: Node Agent did not acquire update ownership; run cluster.sh status for update_degraded' >&2
  return 2
}

local_control_socket_proof(){
  local sock=/var/lib/taskforge/cluster/node-agent.sock
  [ -S "$sock" ] || { echo "error: local Node Agent socket missing: $sock" >&2; return 2; }
  [ "$(stat -c %a "$sock" 2>/dev/null || true)" = 600 ] || { echo "error: local Node Agent socket permissions must be 600" >&2; return 2; }
  python3 -S - "$sock" <<'PY_SOCKET'
import json,socket,sys
path=sys.argv[1]
s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(3);s.connect(path)
s.sendall(b'GET /ha/live HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
chunks=[]
while True:
    part=s.recv(65536)
    if not part:break
    chunks.append(part)
s.close();raw=b''.join(chunks)
head,_,body=raw.partition(b'\r\n\r\n')
if b' 200 ' not in head.split(b'\r\n',1)[0]: raise SystemExit('local socket /ha/live did not return HTTP 200')
payload=json.loads(body.decode('utf-8'))
if payload.get('ok') is not True or str(payload.get('revision'))!='410509': raise SystemExit('local socket returned wrong Agent revision')
print('local_agent_socket=PASS path='+path+' revision=410509')
PY_SOCKET
}

verify_state(){ bash /opt/taskforge/control/cluster/ops/current/verify-state.sh; }
edge_report(){
  node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"
  eligible="$(python3 -S - /etc/taskforge/cluster.json "$node" <<'PY'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2]);a=n.get('app') or {};print('yes' if a.get('can_be_primary',True) and str(n.get('public_host','')).strip() else 'no')
PY
)"
  if [ "$eligible" != yes ]; then echo "cloudflare_failover=NOT_REQUIRED node=$node"; return 0; fi
  if [ -s /etc/taskforge/cloudflare-api-token ] && [ -s /etc/taskforge/cloudflare-failover.json ]; then python3 /opt/taskforge/control/cluster/cloudflare_dns.py preflight; echo "cloudflare_failover=CONFIGURED node=$node"; else echo "cloudflare_failover=NOT_CONFIGURED node=$node"; fi
}
operator_readonly_diagnostics(){
  op_user="$(tr -d '\r\n' < /etc/taskforge/operator-user 2>/dev/null || true)"; [ -n "$op_user" ] || { echo 'error: operator-user missing' >&2; return 2; }
  id "$op_user" >/dev/null 2>&1 || { echo "error: operator user does not exist: $op_user" >&2; return 2; }
  runuser -u "$op_user" -- bash /opt/taskforge/control/cluster.sh status >/dev/null
  runuser -u "$op_user" -- bash /opt/taskforge/control/cluster.sh doctor >/dev/null
  echo "operator_readonly_diagnostics=PASS user=$op_user"
}
commit(){
  node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"; mkdir -p /var/lib/taskforge/cluster/easy
  printf 'complete %s node=%s source_revision=%s state_based=yes\n' "$(date -u +%FT%TZ)" "$node" "$SOURCE_REV_BEFORE" > /var/lib/taskforge/cluster/easy/release-v41.5.9-complete
  chmod 600 /var/lib/taskforge/cluster/easy/release-v41.5.9-complete
}

run 'package integrity' package_integrity
run_stream 'discover + preserve live state' stage_state
run 'quiesce competing application controllers' quiesce_application_updates
run 'normalize current topology policy' normalize_policy
run_stream 'prepare isolated SQL runtime (full nodes only)' prepare_sql
run 'current-state Compose preflight' config_preflight
run 'install v41.5.9 stable control' install_control
run_stream 'adopt/re-home actual runtime' rehome_actual_state
run_stream 'bootstrap + apply 1C auto-install runtime' apply_onec_runtime
run 'restart v41.5.9 Node Agent' restart_agent
run 'v41.5.9 reconciled Agent proof' wait_release
run 'Node Agent updater ownership proof' updater_ownership_proof
run 'local Node Agent Unix control socket proof' local_control_socket_proof
run_stream 'reconcile isolated SQL runtime on active full Primary' reconcile_sql
verify_and_edge(){ verify_state; edge_report; }
run 'persistent data + stable path proof' verify_and_edge
run 'operator status + doctor without sudo' operator_readonly_diagnostics
apply_ha_timing(){ python3 /opt/taskforge/control/cluster/ha_policy.py migrate; }
run_stream 'verify/apply cluster HA timing policy (Primary only)' apply_ha_timing
run 'commit v41.5.9' commit

echo
echo 'v41.5.9 MIGRATION: COMPLETE'
echo "node=$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"
echo "source_revision=$SOURCE_REV_BEFORE"
echo 'migration_model=state-based; no historical release is executed or required.'
echo 'Persistent data volumes were pinned to their exact pre-migration Docker volume identities and verified after migration.'
