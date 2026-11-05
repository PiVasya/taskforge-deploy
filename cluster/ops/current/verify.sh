#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
cluster_need_env; cluster_need_config
node="$(cluster_node_id)"; project="$(cluster_project)"; pg="$project-postgres-1"; wt="$project-watchtower-1"
read -r ip rest hp <<<"$(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" "$node" <<'PY'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2])
print(n['wireguard']['ip'],(n.get('postgres') or {}).get('patroni_rest_port',8008),n.get('health_port',9187))
PY
)"

[ -f "$TASKFORGE_CLUSTER_RUNTIME/enabled" ] || cluster_die 'stable quorum marker missing'
[ -f "$TASKFORGE_CLUSTER_RUNTIME/join-complete" ] || cluster_die 'stable join-complete marker missing'
[ -f "$TASKFORGE_CLUSTER_RUNTIME/hot-standby-complete" ] || cluster_die 'stable hot-standby marker missing'
[ ! -e "$TASKFORGE_CLUSTER_RUNTIME/join-rehome-fence" ] || cluster_die 'control fence still armed after commit'
[ ! -e "$TASKFORGE_CLUSTER_RUNTIME/migration-fence" ] || cluster_die 'migration fence still armed after commit'

docker inspect "$pg" >/dev/null 2>&1 || cluster_die 'PostgreSQL container missing'
role='unknown'
[ "$(cluster_http_code "http://$ip:$rest/primary")" = 200 ] && role=primary
[ "$(cluster_http_code "http://$ip:$rest/replica")" = 200 ] && role=standby
[ "$role" != unknown ] || cluster_die 'local Patroni role unknown'
if [ "$role" = standby ]; then
  [ "$(docker exec "$pg" sh -lc 'psql -X -U "$POSTGRES_USER" -d postgres -Atc "select status from pg_stat_wal_receiver limit 1;"' 2>/dev/null || true)" = streaming ] || cluster_die 'standby WAL receiver is not streaming'
fi

telemetry_matches(){
  local payload="$1" expected_role="$2"
  ROLE="$expected_role" python3 -S - 3<<<"$payload" <<'PY'
import json,os
try: x=json.load(os.fdopen(3))
except Exception: raise SystemExit(1)
n=x.get('node') or {}; h=x.get('ha') or {}; role=os.environ['ROLE']
if str(n.get('bundle_revision',''))!='410509': raise SystemExit(1)
if h.get('reconciled') is not True: raise SystemExit(1)
if h.get('control_plane_fenced') is not False: raise SystemExit(1)
if h.get('role') != role: raise SystemExit(1)
if not str(h.get('leader','')).strip(): raise SystemExit(1)
if 'telemetry-initializing' in (h.get('hot_start_blockers') or []): raise SystemExit(1)
if role=='standby' and h.get('hot_start_ready') is not True: raise SystemExit(1)
if role=='primary':
    if h.get('traffic_ready') is not True: raise SystemExit(1)
    if h.get('tls_live_ready') is not True: raise SystemExit(1)
PY
}

# /ha/live is only process liveness. Final verification waits for one complete
# reconciliation snapshot so the startup skeleton can never be mistaken for
# cluster state. Poll independently even if re-home already waited earlier.
telemetry=''
for _ in $(seq 1 120); do
  telemetry="$(curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$hp/ha/telemetry" 2>/dev/null || true)"
  if [ -n "$telemetry" ] && telemetry_matches "$telemetry" "$role"; then break; fi
  sleep 1
done
if [ -z "$telemetry" ] || ! telemetry_matches "$telemetry" "$role"; then
  echo '[taskforge] final Agent telemetry never reached a reconciled state:' >&2
  echo "local_telemetry_bytes=${#telemetry} (body suppressed)" >&2
  cluster_die 'Node Agent telemetry did not reconcile before final HA verification deadline'
fi

# Legacy Watchtower must NOT be a second updater, whether absent or stopped.
[ "$(docker inspect -f '{{.State.Status}}' "$wt" 2>/dev/null || true)" != running ] || cluster_die 'legacy Watchtower is running alongside Node Agent'
bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh >/dev/null

if [ "$role" = primary ]; then
  domain="$(cluster_read_env DOMAIN)"; [ -n "$domain" ] || cluster_die 'DOMAIN missing'
  curl -kfsS --connect-timeout 2 --max-time 5 --resolve "$domain:443:$ip" "https://$domain/health/gateway" >/dev/null || cluster_die 'local HTTPS gateway probe failed'
  while IFS='|' read -r rid rip rhp; do
    [ "$rid" = "$node" ] && continue
    remote=''
    for _ in $(seq 1 60); do
      remote="$(curl -fsS --connect-timeout 2 --max-time 5 "http://$rip:$rhp/ha/telemetry" 2>/dev/null || true)"
      if RID="$rid" python3 -S - 3<<<"$remote" <<'PY_REMOTE'
import json,os
try: x=json.load(os.fdopen(3))
except Exception: raise SystemExit(1)
n=x.get('node') or {}; h=x.get('ha') or {}
ok=(str(n.get('id',''))==os.environ['RID'] and str(n.get('bundle_revision','')).isdigit() and int(n.get('bundle_revision'))>=410400 and h.get('reconciled') is True and h.get('role')=='standby' and h.get('hot_start_ready') is True and h.get('control_plane_fenced') is False and bool(str(h.get('leader','')).strip()))
raise SystemExit(0 if ok else 1)
PY_REMOTE
      then break; fi
      sleep 1
    done
    if ! RID="$rid" python3 -S - 3<<<"$remote" <<'PY_REMOTE_FINAL'
import json,os
try: x=json.load(os.fdopen(3))
except Exception: raise SystemExit(1)
n=x.get('node') or {}; h=x.get('ha') or {}
ok=(str(n.get('id',''))==os.environ['RID'] and str(n.get('bundle_revision','')).isdigit() and int(n.get('bundle_revision'))>=410400 and h.get('reconciled') is True and h.get('role')=='standby' and h.get('hot_start_ready') is True and h.get('control_plane_fenced') is False and bool(str(h.get('leader','')).strip()))
raise SystemExit(0 if ok else 1)
PY_REMOTE_FINAL
    then
      echo "[taskforge] remote Agent $rid did not reach final reconciled standby state:" >&2
      echo "remote_telemetry_bytes=${#remote} (body suppressed)" >&2
      cluster_die "remote v41.5.9 Agent verification failed on $rid"
    fi
  done < <(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY_NODES'
import json,sys
for n in json.load(open(sys.argv[1]))['nodes']:
    print('|'.join([str(n['id']),str(n['wireguard']['ip']),str(n.get('health_port',9187))]))
PY_NODES
)
fi

echo "PASS: v41.5.9 local HA invariants node=$node role=$role reconciled=yes stable-runtime=yes"
