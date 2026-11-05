#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
keep_migration_fence=0
[ "${1:-}" != '--keep-migration-fence' ] || { keep_migration_fence=1; shift; }
[ $# -eq 0 ] || cluster_die 'unexpected rehome-replica arguments'
[ "$(id -u)" -eq 0 ] || cluster_die 'rehome-replica requires root'
cluster_need_env; cluster_need_config
node="$(cluster_node_id)"; project="$(cluster_project)"; pg="$project-postgres-1"; wt="$project-watchtower-1"; etcd="$project-etcd-1"
TX="$TASKFORGE_CLUSTER_RUNTIME/migrate-r55.json"; JOIN_FENCE="$TASKFORGE_CLUSTER_RUNTIME/join-rehome-fence"; MIG_FENCE="$TASKFORGE_CLUSTER_RUNTIME/migration-fence"
mkdir -p "$TASKFORGE_CLUSTER_RUNTIME/easy"

read -r ip rest health profile <<<"$(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" "$node" <<'PY'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2]);print(n['wireguard']['ip'],(n.get('postgres') or {}).get('patroni_rest_port',8008),n.get('health_port',9187),(n.get('app') or {}).get('profile','full'))
PY
)"

find_leader(){
  python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys,urllib.request
c=json.load(open(sys.argv[1]))
h=[]
for n in c['nodes']:
    ip=n['wireguard']['ip']; port=(n.get('postgres') or {}).get('patroni_rest_port',8008)
    try:
        with urllib.request.urlopen(f'http://{ip}:{port}/primary',timeout=2) as r:
            if r.status==200:h.append(str(n['id']))
    except Exception: pass
if len(h)!=1: raise SystemExit('cannot determine unique Patroni leader: '+repr(h))
print(h[0])
PY
}
node_ip(){ python3 -S - "$TASKFORGE_CLUSTER_CONFIG" "$1" <<'PY'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2]);print(n['wireguard']['ip'],(n.get('postgres') or {}).get('patroni_rest_port',8008))
PY
}
leader_sid(){ local l="$1" lip lrest; read -r lip lrest <<<"$(node_ip "$l")"; curl -fsS "http://$lip:$lrest/patroni" | python3 -S -c 'import json,sys;print(json.load(sys.stdin).get("database_system_identifier",""))'; }
wal_status(){ docker exec "$pg" sh -lc 'psql -X -U "$POSTGRES_USER" -d postgres -Atc "select status from pg_stat_wal_receiver limit 1;"' 2>/dev/null || true; }
local_sid(){ curl -fsS "http://$ip:$rest/patroni" 2>/dev/null | python3 -S -c 'import json,sys;print(json.load(sys.stdin).get("database_system_identifier",""))' 2>/dev/null || true; }
local_replica_ok(){ [ "$(cluster_http_code "http://$ip:$rest/replica")" = 200 ] && [ "$(wal_status)" = streaming ]; }
pg_volume(){ docker inspect "$pg" 2>/dev/null | python3 -S -c 'import json,sys;a=json.load(sys.stdin);d=a[0] if a else {};m=[x for x in d.get("Mounts",[]) if x.get("Destination")=="/var/lib/postgresql" and x.get("Type")=="volume"];print(m[0].get("Name","") if len(m)==1 else "")' 2>/dev/null || true; }
volume_fp(){ local v="$1"; docker volume inspect "$v" 2>/dev/null | python3 -S -c 'import json,sys;a=json.load(sys.stdin);d=a[0] if a else {};print("|".join(str(d.get(k,"")) for k in ("Name","Driver","Mountpoint","CreatedAt","Scope")) if d.get("Name") else "")' 2>/dev/null || true; }

agent_snapshot_ok(){
  local expect_fenced="$1" expect_hot="$2" payload
  payload="$(curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$health/ha/telemetry" 2>/dev/null || true)"
  [ -n "$payload" ] || return 1
  EXPECT_FENCED="$expect_fenced" EXPECT_HOT="$expect_hot" python3 -S - 3<<<"$payload" <<'PY_AGENT_OK'
import json,os
try: x=json.load(os.fdopen(3))
except Exception: raise SystemExit(1)
n=x.get('node') or {}; h=x.get('ha') or {}
if str(n.get('bundle_revision',''))!='410509': raise SystemExit(1)
if h.get('reconciled') is not True: raise SystemExit(1)
if h.get('role')!='standby': raise SystemExit(1)
if not str(h.get('leader','')).strip(): raise SystemExit(1)
if bool(h.get('control_plane_fenced')) != (os.environ['EXPECT_FENCED']=='true'): raise SystemExit(1)
if os.environ['EXPECT_HOT']=='true' and h.get('hot_start_ready') is not True: raise SystemExit(1)
if 'telemetry-initializing' in (h.get('hot_start_blockers') or []): raise SystemExit(1)
PY_AGENT_OK
}
wait_agent_snapshot(){
  local expect_fenced="$1" expect_hot="$2" limit="${3:-120}" last=''
  for _ in $(seq 1 "$limit"); do
    if agent_snapshot_ok "$expect_fenced" "$expect_hot"; then return 0; fi
    last="$(curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$health/ha/telemetry" 2>/dev/null || true)"
    sleep 1
  done
  echo "[taskforge] Agent did not reach reconciled standby state fenced=$expect_fenced hot=$expect_hot" >&2
  [ -z "$last" ] || printf '%s\n' "$last" >&2
  return 1
}
already_stable_runtime(){
  [ -f "$TASKFORGE_CLUSTER_RUNTIME/join-complete" ] && [ -f "$TASKFORGE_CLUSTER_RUNTIME/hot-standby-complete" ] || return 1
  [ ! -e "$JOIN_FENCE" ] && [ ! -e "$MIG_FENCE" ] || return 1
  local_replica_ok || return 1
  bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh >/dev/null 2>&1 || return 1
  return 0
}
# A node that already completed the stable-runtime re-home in the previous
# release only needs the current Agent/control revision. Never recreate its
# PostgreSQL, infrastructure, or prepared applications just to upgrade an already-stable node to v41.5.9.
if [ ! -s "$TX" ] && already_stable_runtime; then
  bash /opt/taskforge/control/cluster/ops/quorum/install-service.sh --no-start
  systemctl restart taskforge-cluster.service >/dev/null || cluster_die 'cannot restart current stable Agent'
  wait_agent_snapshot false true 120 || cluster_die 'current Agent did not reconcile as a hot standby after revision-only upgrade'
  # Node Agent owns app updates. Never restart the retired legacy Watchtower.
  echo "PASS: node $node already had stable runtime; upgraded Agent/control to v41.5.9 without recreating PostgreSQL, infrastructure, or applications"
  exit 0
fi

tx_get(){ [ -s "$TX" ] || return 0; python3 -S - "$TX" "$1" <<'PY'
import json,sys
try:d=json.load(open(sys.argv[1]))
except Exception: raise SystemExit(2)
v=d.get(sys.argv[2],''); print('true' if v is True else 'false' if v is False else '' if v is None else v)
PY
}
tx_set(){ python3 -S - "$TX" "$@" <<'PY'
import json,sys,os,datetime
p=sys.argv[1]
try:d=json.load(open(p)) if os.path.exists(p) else {}
except Exception as e: raise SystemExit(e)
a=sys.argv[2:]
if len(a)%2: raise SystemExit('odd tx args')
for i in range(0,len(a),2): d[a[i]]=a[i+1]
d['updated_at']=datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()
t=p+'.tmp';open(t,'w').write(json.dumps(d,indent=2)+'\n');os.chmod(t,0o600);os.replace(t,p)
PY
}

phase="$(tx_get phase)"
if [ -z "$phase" ]; then
  leader="$(find_leader)"; [ "$leader" != "$node" ] || cluster_die "local node $node is still Patroni primary; controlled switchover is required before replica re-home"
  lsid="$(leader_sid "$leader")"; [ -n "$lsid" ] || cluster_die 'leader system identifier missing'
  local_replica_ok || cluster_die 'local PostgreSQL is not a healthy streaming Patroni replica'
  sid="$(local_sid)"; [ "$sid" = "$lsid" ] || cluster_die "system identifier mismatch local=$sid leader=$lsid"
  vol="$(pg_volume)"; [ -n "$vol" ] || cluster_die 'local PostgreSQL does not use a named HA volume'
  fp="$(volume_fp "$vol")"; [ -n "$fp" ] || cluster_die "cannot fingerprint HA volume $vol"
  tx_set node "$node" phase proven leader "$leader" system_id "$sid" data_volume "$vol" volume_fingerprint "$fp"
  phase=proven
  echo "[taskforge] proven replica node=$node leader=$leader system_id=$sid volume=$vol"
fi
[ "$(tx_get node)" = "$node" ] || cluster_die 'migration transaction belongs to another node'
vol="$(tx_get data_volume)"; sid="$(tx_get system_id)"; fp="$(tx_get volume_fingerprint)"
[ -n "$vol" ] && [ -n "$sid" ] && [ -n "$fp" ] || cluster_die 'migration transaction is incomplete'
[ "$(volume_fp "$vol")" = "$fp" ] || cluster_die 'proven PostgreSQL HA volume identity changed; refusing mutation'

stop_old_agent(){
  printf 'fenced %s node=%s\n' "$(date -u +%FT%TZ)" "$node" > "$JOIN_FENCE"; chmod 600 "$JOIN_FENCE"
  printf 'migrating %s node=%s\n' "$(date -u +%FT%TZ)" "$node" > "$MIG_FENCE"; chmod 600 "$MIG_FENCE"
  systemctl stop taskforge-cluster.service >/dev/null 2>&1 || true
  for _ in $(seq 1 40); do [ "$(systemctl is-active taskforge-cluster.service 2>/dev/null || true)" != active ] && break; sleep 0.25; done
  [ "$(systemctl is-active taskforge-cluster.service 2>/dev/null || true)" != active ] || cluster_die 'old Node Agent did not stop'
  cg="$(systemctl show taskforge-cluster.service -p ControlGroup --value 2>/dev/null || true)"; if [ -n "$cg" ] && [ -r "/sys/fs/cgroup$cg/cgroup.procs" ]; then pids="$(tr -d '[:space:]' < "/sys/fs/cgroup$cg/cgroup.procs" 2>/dev/null || true)"; [ -z "$pids" ] || cluster_die "old Agent cgroup still has processes: $pids"; fi
  bash /opt/taskforge/control/cluster/ops/quorum/install-service.sh --no-start
}

if [ "$phase" = proven ]; then
  stop_old_agent
  docker stop -t 20 "$wt" >/dev/null 2>&1 || true
  services="$(cluster_compose_force config --services)"
  while IFS= read -r s; do
    case "$s" in etcd|postgres|rabbitmq|redis|minio|watchtower) continue;; esac
    c="$project-$s-1"; [ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" != running ] || docker stop -t 30 "$c" >/dev/null 2>&1 || true
  done <<< "$services"
  tx_set phase fenced; phase=fenced
fi

wait_healthy(){
  local c="$1" limit="${2:-120}" h
  for _ in $(seq 1 "$limit"); do
    h="$(docker inspect -f '{{with index .State "Health"}}{{.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || true)"
    [ "$h" = healthy ] && return 0
    sleep 1
  done
  echo "[taskforge] container failed health deadline: $c" >&2
  docker inspect -f 'image={{.Config.Image}} status={{.State.Status}} health={{with index .State "Health"}}{{.Status}}{{else}}none{{end}} restarts={{.RestartCount}} exit={{.State.ExitCode}} oom={{.State.OOMKilled}} error={{.State.Error}}' "$c" 2>/dev/null >&2 || true
  docker logs --tail 100 "$c" 2>&1 | tail -n 100 >&2 || true
  return 1
}
remote_etcd_quorum(){
  local ok=0
  while read -r nid nip; do [ "$nid" = "$node" ] && continue; body="$(curl -fsS --connect-timeout 2 --max-time 4 "http://$nip:2379/health" 2>/dev/null || true)"; grep -Eq '"health"[[:space:]]*:[[:space:]]*"?true"?' <<<"$body" && ok=$((ok+1)); done < <(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys
for n in json.load(open(sys.argv[1]))['nodes']: print(n['id'],n['wireguard']['ip'])
PY
)
  [ "$ok" -ge 2 ]
}

if [ "$phase" = fenced ]; then
  remote_etcd_quorum || cluster_die 'both remote etcd voters must be healthy before local etcd re-home'
  docker stop -t 30 "$etcd" >/dev/null 2>&1 || true; docker rm "$etcd" >/dev/null 2>&1 || true
  cluster_compose_force up -d --no-deps --pull never etcd >/dev/null
  wait_healthy "$etcd" 90 || cluster_die 're-homed etcd did not become healthy'
  tx_set phase etcd-ready; phase=etcd-ready
fi

postgres_stable_mounts(){
  docker inspect "$pg" 2>/dev/null | python3 -S -c 'import json,sys
try:d=json.load(sys.stdin)[0]
except Exception:raise SystemExit(1)
w={"/run/taskforge-cluster":sys.argv[1],"/run/taskforge-patroni-wrapper.sh":sys.argv[2],"/docker-entrypoint-initdb.d":sys.argv[3]};s={m.get("Destination"):m.get("Source") for m in d.get("Mounts",[]) if m.get("Type")=="bind"};raise SystemExit(0 if all(s.get(k)==v for k,v in w.items()) else 1)' "$TASKFORGE_CLUSTER_RUNTIME" "$TASKFORGE_ROOT/cluster/ops/quorum/patroni-container-entrypoint.sh" "$TASKFORGE_ROOT/infrastructure/postgres/init"
}
verify_replica_after(){
  for _ in $(seq 1 120); do [ "$(cluster_http_code "http://$ip:$rest/replica")" = 200 ] && break; sleep 1; done
  [ "$(cluster_http_code "http://$ip:$rest/replica")" = 200 ] || cluster_die 'Patroni did not return as replica'
  [ "$(local_sid)" = "$sid" ] || cluster_die "PostgreSQL system identifier changed after re-home"
  for _ in $(seq 1 60); do [ "$(wal_status)" = streaming ] && break; sleep 1; done
  [ "$(wal_status)" = streaming ] || cluster_die 're-homed Patroni replica is not streaming'
  [ "$(pg_volume)" = "$vol" ] || cluster_die 're-homed PostgreSQL is not using the proven HA volume'
  [ "$(volume_fp "$vol")" = "$fp" ] || cluster_die 'HA volume fingerprint changed during container-only re-home'
  postgres_stable_mounts || cluster_die 'PostgreSQL still has non-stable bind sources'
}

if [ "$phase" = etcd-ready ] || [ "$phase" = postgres-rehome-started ]; then
  tx_set phase postgres-rehome-started; phase=postgres-rehome-started
  if local_replica_ok && postgres_stable_mounts && [ "$(local_sid)" = "$sid" ]; then :; else
    docker stop -t 60 "$pg" >/dev/null 2>&1 || true; docker rm "$pg" >/dev/null 2>&1 || true
    cluster_retire_replica_postgres_proxy "$ip" 5432
    [ "$(volume_fp "$vol")" = "$fp" ] || cluster_die 'proven HA volume changed before PostgreSQL recreate'
    cluster_render >/dev/null
    cluster_compose_force up -d --no-deps --pull never postgres >/dev/null
  fi
  verify_replica_after
  tx_set phase postgres-ready; phase=postgres-ready
fi

if [ "$phase" = postgres-ready ]; then
  # Select/persist the MinIO image before removing the current container.
  # On normal migrations this preserves the proven live image. If the known
  # x86-64-v2 crash is observed, MinIO may use the matching compatibility
  # -cpuv1 image before retrying.
  bash /opt/taskforge/control/cluster/ops/current/minio-image.sh
  if [ ! -x "$TASKFORGE_CLUSTER_RUNTIME/bin/mc" ]; then
    bash /opt/taskforge/control/cluster/ops/host/install-mc.sh >/dev/null || cluster_die 'cannot prepare MinIO mc before hot-standby commit'
  fi
  stable_healthy_infra(){
    local c="$1" h wd cfg
    h="$(docker inspect -f '{{with index .State "Health"}}{{.Status}}{{else}}{{.State.Status}}{{end}}' "$c" 2>/dev/null || true)"
    [ "$h" = healthy ] || return 1
    wd="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$c" 2>/dev/null || true)"
    cfg="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.config_files"}}' "$c" 2>/dev/null || true)"
    [ "$wd" = "$TASKFORGE_ROOT" ] || return 1
    grep -Fq "$TASKFORGE_ROOT/" <<<"$cfg" || return 1
    ! grep -Eq '(taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+)' <<<"$cfg"
  }
  for s in rabbitmq redis minio; do
    c="$project-$s-1"
    if stable_healthy_infra "$c"; then
      echo "[taskforge] preserving already-healthy stable $s container"
      continue
    fi
    docker stop -t 30 "$c" >/dev/null 2>&1 || true; docker rm "$c" >/dev/null 2>&1 || true
    if [ "$s" = minio ]; then
      # Legacy C installations used a systemd/socat WG proxy on 10.80.0.3:9000.
      # Direct Docker binding is now authoritative. Retire only that exact
      # known TaskForge unit and prove both MinIO host ports are free first.
      cluster_retire_replica_minio_proxy "$ip" 9000 9001
    fi
    cluster_compose_force up -d --no-deps --pull never "$s" >/dev/null
    wait_healthy "$c" 120 || cluster_die "$s did not become healthy after stable re-home"
  done
  tx_set phase infra-ready; phase=infra-ready
fi

APP_FULL=(telegram-quiz-bot gateway execution-worker observability-api execution-api tasks-api ai-worker support-bot javascript-runner image-analyzer solutions-api identity-api cpp-runner rating-worker minecraft-api notifications-api education-api code-analyzer support-api pascal-runner browser-api ai-api java-runner quiz-api files-api csharp-runner front-ct content-api front python-runner onec-runner sql-worker sql-postgres sql-mysql)
assigned=("${APP_FULL[@]}")
if [ "$profile" = lite ]; then assigned=(); for s in "${APP_FULL[@]}"; do case "$s" in browser-api|image-analyzer|sql-worker|sql-postgres|sql-mysql) ;; *) assigned+=("$s");; esac; done; fi
if ! grep -q "^SQL_ENABLED=true$" /etc/taskforge/sql-runtime.env 2>/dev/null; then filtered=(); for s in "${assigned[@]}"; do case "$s" in sql-worker|sql-postgres|sql-mysql) ;; *) filtered+=("$s");; esac; done; assigned=("${filtered[@]}"); fi
if ! grep -qi '^ONEC_ENABLED=true$' /etc/taskforge/taskforge.env 2>/dev/null; then filtered=(); for s in "${assigned[@]}"; do case "$s" in onec-runner) ;; *) filtered+=("$s");; esac; done; assigned=("${filtered[@]}"); fi
is_assigned(){ local x; for x in "${assigned[@]}"; do [ "$x" = "$1" ] && return 0; done; return 1; }

if [ "$phase" = infra-ready ] || [ "$phase" = apps-rehome-started ]; then
  tx_set phase apps-rehome-started; phase=apps-rehome-started
  for s in "${APP_FULL[@]}" certbot; do
    c="$project-$s-1"; st="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)"; [ "$st" != running ] || docker stop -t 30 "$c" >/dev/null 2>&1 || true; [ -z "$st" ] || docker rm "$c" >/dev/null 2>&1 || true
    if is_assigned "$s"; then cluster_compose_force up --no-start --no-deps --pull never "$s" >/dev/null || cluster_die "cannot prepare app $s"; fi
  done
  bash /opt/taskforge/control/cluster/ops/current/verify-resources.sh "$node"
  docker stop -t 20 "$wt" >/dev/null 2>&1 || true; docker rm "$wt" >/dev/null 2>&1 || true
  # Application updates are owned by the Node Agent; no legacy updater is created.
  tx_set phase apps-ready; phase=apps-ready
fi

wait_agent_live(){ for _ in $(seq 1 60); do [ "$(cluster_http_code "http://$ip:$health/ha/live")" = 200 ] && return 0; sleep 1; done; return 1; }

if [ "$phase" = apps-ready ] || [ "$phase" = agent-ready ]; then
  bash /opt/taskforge/control/cluster/ops/quorum/install-service.sh --no-start
  systemctl start taskforge-cluster.service >/dev/null || cluster_die 'cannot start stable v41.5.9 Agent'
  wait_agent_live || cluster_die 'v41.5.9 Agent did not become live'
  wait_agent_snapshot true true 120 || cluster_die 'v41.5.9 Agent did not reconcile as hot-ready while the control fence was armed'
  tx_set phase agent-ready; phase=agent-ready
fi

if [ "$phase" = agent-ready ]; then
  printf 'joined %s node=%s system_id=%s\n' "$(date -u +%FT%TZ)" "$node" "$sid" > "$TASKFORGE_CLUSTER_RUNTIME/join-complete"
  printf 'hot %s node=%s\n' "$(date -u +%FT%TZ)" "$node" > "$TASKFORGE_CLUSTER_RUNTIME/hot-standby-complete"
  chmod 600 "$TASKFORGE_CLUSTER_RUNTIME/join-complete" "$TASKFORGE_CLUSTER_RUNTIME/hot-standby-complete"
  rm -f "$JOIN_FENCE"
  systemctl restart taskforge-cluster.service >/dev/null
  wait_agent_live || cluster_die 'Agent did not return after fence removal'
  # /ha/live only proves the HTTP process is listening. The initial Agent
  # skeleton is deliberately NOT accepted as proof that the fence is gone.
  wait_agent_snapshot false true 120 || cluster_die 'Agent did not reconcile as unfenced hot standby after fence removal'
  tx_set phase watchtower-starting; phase=watchtower-starting
fi

if [ "$phase" = watchtower-starting ]; then
  docker stop -t 10 "$wt" >/dev/null 2>&1 || true
  [ "$keep_migration_fence" -eq 1 ] || rm -f "$MIG_FENCE"
  tx_set phase committed; phase=committed
fi

if [ "$phase" = committed ]; then
  bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh
  mv -f "$TX" "$TASKFORGE_CLUSTER_RUNTIME/easy/migrate-r55-complete.json" 2>/dev/null || true
  echo "PASS: node $node re-homed to stable r55 control/runtime without deleting PostgreSQL data"
fi
