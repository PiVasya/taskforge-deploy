#!/usr/bin/env bash
# Operator-approved RabbitMQ healthcheck migration on a confirmed hot standby.
# Uses a short, expiring updater maintenance lease; promotion overrides it.
# Never removes any data volume, never stops non-RabbitMQ containers.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo 'error: run as root' >&2; exit 2; }
[ "$(tr -d '\r\n' < "$ROOT/REVISION")" = 410509 ] || { echo 'error: install v41.5.9 first' >&2; exit 2; }
[ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION 2>/dev/null || true)" = 410509 ] || { echo 'error: installed control is not v41.5.9' >&2; exit 2; }
exec 9>/run/lock/taskforge-server-migrate.lock
flock -n 9 || { echo 'error: migration or another infra operation is active' >&2; exit 2; }
RUNTIME="${TASKFORGE_CLUSTER_RUNTIME:-/var/lib/taskforge/cluster}"
[ ! -e "$RUNTIME/join-rehome-fence" ] && [ ! -e "$RUNTIME/migration-fence" ] || { echo 'error: migration/re-home fence is active' >&2; exit 2; }
node="$(tr -d '\r\n' < "$RUNTIME/node-id")"
case "$node" in A|B|C) ;; *) echo 'error: unknown node-id' >&2; exit 2 ;; esac
read -r ip port <<EOF
$(python3 -S - /etc/taskforge/cluster.json "$node" <<'PY'
import json,sys
nodes=json.load(open(sys.argv[1]))['nodes']
n=next(x for x in nodes if str(x.get('id'))==sys.argv[2])
print(n['wireguard']['ip'],int(n.get('health_port',9187)))
PY
)
EOF

# This preflight reports which exact condition failed, rather than telling a
# healthy Standby to keep retrying a command that races the updater worker.
check_standby() {
  local mode="${1:-before}" payload
  payload="$(curl -fsS --connect-timeout 2 --max-time 8 "http://${ip}:${port}/ha/telemetry")" || { echo 'error: Node Agent telemetry unavailable' >&2; return 2; }
  TF_EXPECT_NODE="$node" TF_OPT_MODE="$mode" TF_OPT_TOKEN="${token:-}" python3 -S - 3<<<"$payload" <<'PY'
import json,os
try:
    a=json.load(os.fdopen(3)); n=a.get('node') or {}; h=a.get('ha') or {}; u=a.get('update') or {}; d=a.get('docker') or {}
except (ValueError,TypeError,OSError) as err:
    raise SystemExit('error: cannot parse standby telemetry: '+str(type(err).__name__))
checks={
    'node-id':str(n.get('id'))==os.environ['TF_EXPECT_NODE'],
    'control-revision':str(n.get('bundle_revision'))=='410509',
    'standby-role':h.get('role')=='standby',
    'remote-leader':bool(h.get('leader')) and h.get('leader')!=os.environ['TF_EXPECT_NODE'],
    'warm-standby-mode':h.get('app_mode')=='warm-standby',
    'no-running-apps':int(d.get('running_app_count', -1))==0,
    'agent-reconciled':h.get('reconciled') is True,
    'hot-standby-ready':h.get('hot_start_ready') is True,
    'no-live-traffic':h.get('traffic_ready') is False,
    'not-control-fenced':h.get('control_plane_fenced') is False,
    'node-agent-updater':u.get('mode')=='node-agent',
    # Image-stage failures are independent of RabbitMQ data safety. A verified
    # idle hot standby can maintain RabbitMQ while the updater repairs them.
    # Keep guarding the global updater and require a maintenance ACK before
    # touching RabbitMQ; never hide application failures from status output.
    'no-updater-global-error':not bool(u.get('error')),
    'updater-running':u.get('running') is True,
}
if os.environ['TF_OPT_MODE']=='paused':
    checks['updater-maintenance-ack']=(u.get('status')=='maintenance' and
                                    u.get('maintenance_ack_token')==os.environ.get('TF_OPT_TOKEN') and
                                    bool(os.environ.get('TF_OPT_TOKEN')))
else:
    # Applying is allowed in the initial preflight: the script will request a
    # pause, then wait until the in-flight operation has fully completed.
    checks['updater-operational']=u.get('status') in {'monitoring','applying','reconciling','maintenance'}
bad=[name for name,valid in checks.items() if not valid]
if bad: raise SystemExit('error: standby-preflight-failed: '+','.join(bad))
print('standby_preflight=PASS node='+os.environ['TF_EXPECT_NODE']+' leader='+str(h['leader'])+' mode='+os.environ['TF_OPT_MODE']+' app_update_failures='+str(int(u.get('failed_count') or 0)))
PY
}

export DOCKER_CONFIG="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}/docker"
mkdir -p "$DOCKER_CONFIG"
check_standby before

# Respect the existing project and its persistent Docker storage identity.
project="$(python3 -S - /etc/taskforge/taskforge.env <<'PY'
import sys
project='taskforge-prod'
for raw in open(sys.argv[1],encoding='utf-8-sig'):
    line=raw.strip()
    if not line or line.startswith('#') or '=' not in line: continue
    key,value=line.split('=',1)
    if key.strip()=='COMPOSE_PROJECT_NAME': project=value.strip().strip('"\'')
print(project)
PY
)"
[[ "$project" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] || { echo 'error: invalid Compose project' >&2; exit 2; }
container="$project-rabbitmq-1"
volume_name() {
  docker inspect -f '{{range .Mounts}}{{if and (eq .Type "volume") (eq .Destination "/var/lib/rabbitmq")}}{{println .Name}}{{end}}{{end}}' "$container" | sed -n '1p'
}
old_volume="$(volume_name)"
[ -n "$old_volume" ] || { echo 'error: RabbitMQ persistent volume not found, refuse recreation' >&2; exit 2; }
old_state="$(docker inspect -f '{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}' "$container")"
[ "$old_state" = 'running|healthy' ] || { echo "error: RabbitMQ preflight state=$old_state" >&2; exit 2; }
# Docker's Go template renders time.Duration as "30s", while JSON renders
# it as a nanosecond integer; support both representation styles.
is_30s() { [ "$1" = 30s ] || [ "$1" = 30000000000 ]; }
old_interval="$(docker inspect -f '{{.Config.Healthcheck.Interval}}' "$container")"
old_retries="$(docker inspect -f '{{.Config.Healthcheck.Retries}}' "$container")"
echo "rabbitmq_volume=$old_volume old_interval=$old_interval retries=$old_retries desired_interval=30s desired_retries=10"
if is_30s "$old_interval" && [ "$old_retries" = 10 ]; then
  echo 'rabbitmq_healthcheck=ALREADY_OPTIMIZED; no container restart'
  exit 0
fi

# The lifecycle worker acknowledges only *after* its current Compose operation
# finishes. The 10-minute lease expires automatically if this script crashes.
# If promoted, Node Agent ignores the lease immediately and proceeds with HA.
token="$(python3 -S -c 'import secrets;print(secrets.token_hex(24))')"
lease_path="$RUNTIME/updater-maintenance.json"
lease_tmp="$RUNTIME/.updater-maintenance.$$"
TF_OPT_TOKEN="$token" python3 -S - "$lease_tmp" <<'PY'
import json,os,sys,time
with open(sys.argv[1],'x') as stream:
    os.fchmod(stream.fileno(),0o600)
    json.dump({'kind':'rabbitmq-standby','token':os.environ['TF_OPT_TOKEN'],
               'expires_at':time.time()+600},stream)
PY
mv -f -- "$lease_tmp" "$lease_path"
cleanup_lease() {
  TF_OPT_TOKEN="$token" python3 -S - "$lease_path" <<'PY' || true
import json,os,sys
try:
    with open(sys.argv[1]) as stream: data=json.load(stream)
    if data.get('token')==os.environ.get('TF_OPT_TOKEN'):os.unlink(sys.argv[1])
except (OSError,ValueError,TypeError):pass
PY
}
trap cleanup_lease EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo 'Waiting for Node Agent to finish any in-flight image preparation and acknowledge standby maintenance...'
ack=false
for i in $(seq 1 90); do
  if check_standby paused >/dev/null 2>&1; then ack=true;break;fi
  if (( i % 10 == 0 ));then echo "maintenance_wait_seconds=$((i*2))";fi
  sleep 2
done
[ "$ack" = true ] || { echo 'error: updater did not acknowledge standby maintenance within 180s; RabbitMQ untouched' >&2;check_standby paused || true;exit 2; }
check_standby paused
# A role may change while we are waiting; recheck immediately before mutation.
old_volume="$(volume_name)"
[ -n "$old_volume" ] || { echo 'error: RabbitMQ volume disappeared, abort' >&2; exit 2; }
echo 'Recreating ONLY standby RabbitMQ with the same named volume (no down, no rm -v)...'
timeout 150s bash "$ROOT/compose.sh" up -d --no-deps --pull never rabbitmq
new_volume="$(volume_name)"
[ "$new_volume" = "$old_volume" ] || { echo "error: persistent volume identity changed ($old_volume -> $new_volume)" >&2; exit 3; }
new_interval="$(docker inspect -f '{{.Config.Healthcheck.Interval}}' "$container")"
new_retries="$(docker inspect -f '{{.Config.Healthcheck.Retries}}' "$container")"
if ! is_30s "$new_interval" || [ "$new_retries" != 10 ]; then
  echo "error: expected interval=30s retries=10, got interval=$new_interval retries=$new_retries" >&2
  exit 3
fi
for i in $(seq 1 80);do
  state="$(docker inspect -f '{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}' "$container")"
  if [ "$state" = 'running|healthy' ];then
    echo "rabbitmq_healthcheck=PASS state=$state persistent_volume=$new_volume"
    for j in $(seq 1 25);do
      if check_standby paused >/dev/null 2>&1;then
        echo 'standby_hot_readiness=PASS'
        exit 0
      fi
      sleep 2
    done
    echo 'error: RabbitMQ healthy but standby readiness did not recover; inspect HA telemetry' >&2
    check_standby paused || true
    exit 3
  fi
  if (( i == 1 || i % 10 == 0 ));then echo "waiting_rabbitmq_health=${i}x2s state=$state";fi
  sleep 2
done
echo 'error: standby RabbitMQ did not become healthy; inspect Docker logs' >&2
exit 3
