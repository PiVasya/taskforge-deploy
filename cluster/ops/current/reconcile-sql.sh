#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="${TASKFORGE_CONTROL_ROOT:-/opt/taskforge/control}"
CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
RUNTIME="${TASKFORGE_CLUSTER_RUNTIME:-/var/lib/taskforge/cluster}"
ENV_FILE="${TASKFORGE_ENV_FILE:-$CONFIG_ROOT/taskforge.env}"
SQL_ENV="$CONFIG_ROOT/sql-runtime.env"
SOFT_MIGRATION="${TASKFORGE_SQL_MIGRATION_SOFT:-0}"

[ "$(id -u)" -eq 0 ] || { echo 'error: SQL runtime reconcile requires root' >&2; exit 2; }
[ -s "$RUNTIME/node-id" ] || { echo 'error: local node-id missing' >&2; exit 2; }
[ -s "$CONFIG_ROOT/cluster.json" ] || { echo 'error: cluster config missing' >&2; exit 2; }
[ -s "$ENV_FILE" ] || { echo 'error: TaskForge environment missing' >&2; exit 2; }

node="$(tr -d '\r\n' < "$RUNTIME/node-id")"
read -r profile ip health_port <<<"$(python3 -S - "$CONFIG_ROOT/cluster.json" "$node" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
n=next(x for x in cfg['nodes'] if str(x['id'])==sys.argv[2])
app=n.get('app') or {}
print(app.get('profile','full'), n['wireguard']['ip'], int(n.get('health_port',9187)))
PY
)"
[ "$node" != C ] || profile=lite

if [ "$profile" != full ]; then
  echo "[TFSQL RECONCILE] node=$node profile=$profile action=skip reason=lite"
  exit 0
fi
if ! grep -q '^SQL_ENABLED=true$' "$SQL_ENV" 2>/dev/null; then
  echo "[TFSQL RECONCILE] node=$node profile=$profile action=skip reason=disabled"
  exit 0
fi

telemetry="$(curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$health_port/ha/telemetry" 2>/dev/null || true)"
role="$(python3 -S - 3<<<"$telemetry" <<'PY'
import json,os
try:
    t=json.load(os.fdopen(3))
except Exception:
    print('unknown')
else:
    print((t.get('ha') or {}).get('role') or 'unknown')
PY
)"
if [ "$role" != primary ]; then
  echo "[TFSQL RECONCILE] node=$node role=$role action=skip reason=primary-only-cache"
  exit 0
fi

compose(){
  TASKFORGE_CLUSTER_FORCE=1 TASKFORGE_CONTROL_ROOT="$ROOT" TASKFORGE_CONFIG_ROOT="$CONFIG_ROOT" TASKFORGE_CLUSTER_RUNTIME="$RUNTIME" TASKFORGE_ENV_FILE="$ENV_FILE" bash "$ROOT/compose.sh" "$@"
}

container_id(){ compose ps -q "$1" | head -n1; }
container_state(){
  local cid="$1"
  [ -n "$cid" ] || { echo 'missing|none|false|0'; return 0; }
  docker inspect -f '{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}|{{.State.OOMKilled}}|{{.RestartCount}}' "$cid" 2>/dev/null || echo 'missing|none|false|0'
}
wait_healthy(){
  local service="$1" timeout="$2" started now cid state status health oom restarts elapsed last_report=-5
  started="$(date +%s)"
  while :; do
    cid="$(container_id "$service" 2>/dev/null || true)"
    state="$(container_state "$cid")"
    IFS='|' read -r status health oom restarts <<<"$state"
    if [ "$status" = running ] && { [ "$health" = healthy ] || [ "$health" = none ]; }; then
      echo "[TFSQL RECONCILE] service=$service state=$state result=ready"
      return 0
    fi
    now="$(date +%s)"; elapsed=$((now-started))
    if [ "$elapsed" -ge $((last_report+5)) ]; then
      echo "[TFSQL RECONCILE] service=$service wait=${elapsed}s/${timeout}s status=$status health=$health restarts=${restarts:-0}"
      last_report="$elapsed"
    fi
    if [ "$SOFT_MIGRATION" = 1 ] && [ "$status" = restarting ] && [ "${restarts:-0}" -ge 3 ]; then
      echo "[TFSQL RECONCILE] service=$service result=degraded reason=restart-loop restarts=${restarts:-0} action=stop"
      compose stop "$service" >/dev/null 2>&1 || true
      return 1
    fi
    if [ "$elapsed" -ge "$timeout" ]; then
      echo "[TFSQL RECONCILE] service=$service result=degraded reason=readiness-timeout state=$state" >&2
      [ -z "$cid" ] || docker logs --tail 40 "$cid" 2>&1 >&2 || true
      return 1
    fi
    sleep 2
  done
}

if [ "$SOFT_MIGRATION" = 1 ]; then
  # A code-only controller upgrade must not bounce already healthy SQL caches.
  all_healthy=1
  for service in sql-postgres sql-mysql sql-worker; do
    cid="$(container_id "$service" 2>/dev/null || true)"
    state="$(container_state "$cid")"
    IFS='|' read -r status health oom restarts <<<"$state"
    if [ "$status" != running ] || [ "$health" != healthy ] || [ "$oom" = true ]; then
      all_healthy=0
      break
    fi
  done
  if [ "$all_healthy" = 1 ]; then
    echo "[TFSQL RECONCILE] node=$node role=$role mode=migration-soft action=keep-running reason=already-healthy no-container-recreate=yes"
    exit 0
  fi
fi

echo "[TFSQL RECONCILE] node=$node role=$role action=start-engines mode=$([ "$SOFT_MIGRATION" = 1 ] && echo migration-soft || echo strict)"
if ! compose up -d --no-deps --pull never sql-postgres sql-mysql; then
  if [ "$SOFT_MIGRATION" = 1 ]; then
    echo "[TFSQL RECONCILE] node=$node result=DEGRADED reason=engine-compose-start-failed optional=yes"
    exit 0
  fi
  exit 1
fi

pg_timeout=180; mysql_timeout=240; worker_timeout=120
if [ "$SOFT_MIGRATION" = 1 ]; then pg_timeout=25; mysql_timeout=25; worker_timeout=20; fi
pg_ready=1; mysql_ready=1
wait_healthy sql-postgres "$pg_timeout" || pg_ready=0
wait_healthy sql-mysql "$mysql_timeout" || mysql_ready=0

echo "[TFSQL RECONCILE] node=$node role=$role action=start-worker"
if ! compose up -d --no-deps --pull never sql-worker; then
  if [ "$SOFT_MIGRATION" = 1 ]; then
    echo "[TFSQL RECONCILE] node=$node result=DEGRADED reason=worker-compose-start-failed optional=yes"
    exit 0
  fi
  exit 1
fi
worker_ready=1
wait_healthy sql-worker "$worker_timeout" || worker_ready=0

if [ "$worker_ready" = 1 ]; then
  cid="$(container_id sql-worker)"
  err_file="/tmp/taskforge-sql-ready.err.$$"
  set +e
  ready="$(docker exec "$cid" /app/sql-worker ready 2>"$err_file")"
  rc=$?
  set -e
  if [ "$rc" -eq 0 ]; then
    READY="$ready" python3 -S - <<'PY_READY'
import json,os
x=json.loads(os.environ.get('READY',''))
if not isinstance(x,dict): raise SystemExit(1)
PY_READY
    rc=$?
  fi
  if [ "$rc" -ne 0 ]; then
    cat "$err_file" >&2 || true
    worker_ready=0
  fi
  rm -f "$err_file"
fi

if [ "$pg_ready" = 1 ] && [ "$mysql_ready" = 1 ] && [ "$worker_ready" = 1 ]; then
  echo "[TFSQL RECONCILE] node=$node role=$role result=PASS"
  exit 0
fi
if [ "$SOFT_MIGRATION" = 1 ]; then
  echo "[TFSQL RECONCILE] node=$node role=$role result=DEGRADED optional=yes postgres_ready=$pg_ready mysql_ready=$mysql_ready worker_ready=$worker_ready"
  exit 0
fi
echo "error: SQL runtime did not become fully ready (postgres=$pg_ready mysql=$mysql_ready worker=$worker_ready)" >&2
exit 1
