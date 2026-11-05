#!/usr/bin/env bash
set -Eeuo pipefail
CLUSTER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLUSTER_DIR="$(cd "$CLUSTER_LIB_DIR/.." && pwd)"
TASKFORGE_ROOT="${TASKFORGE_CONTROL_ROOT:-$(cd "$CLUSTER_DIR/.." && pwd)}"
TASKFORGE_STATE_ROOT="${TASKFORGE_STATE_ROOT:-/var/lib/taskforge}"
TASKFORGE_CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
TASKFORGE_CLUSTER_RUNTIME="${TASKFORGE_CLUSTER_RUNTIME:-$TASKFORGE_STATE_ROOT/cluster}"
TASKFORGE_ENV_FILE="${TASKFORGE_ENV_FILE:-$TASKFORGE_CONFIG_ROOT/taskforge.env}"
TASKFORGE_CLUSTER_CONFIG="${TASKFORGE_CLUSTER_CONFIG:-$TASKFORGE_CONFIG_ROOT/cluster.json}"
TASKFORGE_INVENTORY="${TASKFORGE_INVENTORY:-$TASKFORGE_CONFIG_ROOT/inventory.json}"
TASKFORGE_CODE_ANALYZER_KEYS_DIR="${TASKFORGE_CODE_ANALYZER_KEYS_DIR:-$TASKFORGE_CONFIG_ROOT/code-analyzer}"
TASKFORGE_DOCKER_CONFIG_DIR="${TASKFORGE_DOCKER_CONFIG_DIR:-$TASKFORGE_CONFIG_ROOT/docker}"
TASKFORGE_IMAGE_ANALYZER_MODEL_CACHE_DIR="${TASKFORGE_IMAGE_ANALYZER_MODEL_CACHE_DIR:-$TASKFORGE_STATE_ROOT/image-analyzer-model-cache}"
TASKFORGE_DATA_PROTECTION_DIR="${TASKFORGE_DATA_PROTECTION_DIR:-$TASKFORGE_STATE_ROOT/data-protection}"

cluster_die(){ echo "error: $*" >&2; exit 2; }
cluster_warn(){ echo "warning: $*" >&2; }
cluster_log(){ echo "[taskforge] $*"; }
cluster_need_env(){ [ -s "$TASKFORGE_ENV_FILE" ] || cluster_die "environment missing: $TASKFORGE_ENV_FILE"; }
cluster_need_config(){ [ -s "$TASKFORGE_CLUSTER_CONFIG" ] || cluster_die "cluster config missing: $TASKFORGE_CLUSTER_CONFIG"; }
cluster_operator_user(){ [ -s "$TASKFORGE_CONFIG_ROOT/operator-user" ] && tr -d '\r\n' < "$TASKFORGE_CONFIG_ROOT/operator-user" || printf '%s\n' "${SUDO_USER:-root}"; }
cluster_operator_group(){ [ -s "$TASKFORGE_CONFIG_ROOT/operator-group" ] && tr -d '\r\n' < "$TASKFORGE_CONFIG_ROOT/operator-group" || id -gn "$(cluster_operator_user)" 2>/dev/null || printf 'root\n'; }
cluster_chown_owner(){ [ "$(id -u)" -eq 0 ] || return 0; chown "$(cluster_operator_user):$(cluster_operator_group)" "$@"; }
cluster_runtime_prepare(){ mkdir -p "$TASKFORGE_CLUSTER_RUNTIME"; chmod 700 "$TASKFORGE_CLUSTER_RUNTIME" 2>/dev/null || true; cluster_chown_owner "$TASKFORGE_STATE_ROOT" "$TASKFORGE_CLUSTER_RUNTIME" 2>/dev/null || true; }
cluster_mc_config_prepare(){
  export MC_CONFIG_DIR="$TASKFORGE_CLUSTER_RUNTIME/mc-config"
  mkdir -p "$MC_CONFIG_DIR"
  chmod 700 "$MC_CONFIG_DIR" 2>/dev/null || true
  cluster_chown_owner "$MC_CONFIG_DIR" 2>/dev/null || true
}
cluster_mc_config_finish(){
  [ -d "${MC_CONFIG_DIR:-}" ] || return 0
  cluster_chown_owner "$MC_CONFIG_DIR" 2>/dev/null || true
}
cluster_node_id(){ [ -s "$TASKFORGE_CLUSTER_RUNTIME/node-id" ] || cluster_die 'node-id missing'; tr -d '\r\n' < "$TASKFORGE_CLUSTER_RUNTIME/node-id"; }
cluster_read_env(){
  local key="$1"
  python3 -S - "$TASKFORGE_ENV_FILE" "$key" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); key=sys.argv[2]
if not p.exists(): raise SystemExit(0)
for raw in p.read_text(encoding='utf-8-sig').splitlines():
    if raw.lstrip().startswith('#') or '=' not in raw: continue
    k,v=raw.split('=',1)
    if k.strip()==key:
        v=v.strip()
        if len(v)>=2 and v[:1]==v[-1:] and v[0] in "\"'": v=v[1:-1]
        print(v); break
PY
}
cluster_project(){ local v; v="$(cluster_read_env COMPOSE_PROJECT_NAME)"; printf '%s\n' "${v:-taskforge-prod}"; }
clusterctl(){ python3 "$CLUSTER_DIR/clusterctl.py" --config "$TASKFORGE_CLUSTER_CONFIG" --env-file "$TASKFORGE_ENV_FILE" "$@"; }
cluster_render(){ cluster_runtime_prepare; clusterctl render --node "$(cluster_node_id)"; }
cluster_compose(){ TASKFORGE_CONTROL_ROOT="$TASKFORGE_ROOT" TASKFORGE_ENV_FILE="$TASKFORGE_ENV_FILE" bash "$TASKFORGE_ROOT/compose.sh" "$@"; }
cluster_compose_force(){ TASKFORGE_CLUSTER_FORCE=1 TASKFORGE_CONTROL_ROOT="$TASKFORGE_ROOT" TASKFORGE_ENV_FILE="$TASKFORGE_ENV_FILE" bash "$TASKFORGE_ROOT/compose.sh" "$@"; }
cluster_atomic_write(){
  local dst="$1" mode="${2:-600}" tmp
  mkdir -p "$(dirname "$dst")"; tmp="${dst}.tmp.$$"; cat > "$tmp"; chmod "$mode" "$tmp"; cluster_chown_owner "$tmp" 2>/dev/null || true; sync -f "$tmp" 2>/dev/null || true; mv -f "$tmp" "$dst"; sync -f "$(dirname "$dst")" 2>/dev/null || true
}
cluster_http_code(){ curl -sS -o /dev/null -w '%{http_code}' --connect-timeout 2 --max-time 5 "$1" 2>/dev/null || printf '000'; }
cluster_patroni_switchover(){
  local leader="$1" candidate="$2" bind_ip="$3" rest_port="$4" out="$5"
  python3 -S - "$TASKFORGE_ENV_FILE" "$bind_ip" "$rest_port" "$leader" "$candidate" "$out" <<'PY_PATRONI_POST'
import base64,hashlib,hmac,json,sys,urllib.error,urllib.request
from pathlib import Path

env_path,ip,port,leader,candidate,out=sys.argv[1:]
env={}
for raw in Path(env_path).read_text(encoding='utf-8-sig').splitlines():
    if not raw.strip() or raw.lstrip().startswith('#') or '=' not in raw:
        continue
    k,v=raw.split('=',1); v=v.strip()
    if len(v)>=2 and v[:1]==v[-1:] and v[0] in "\"'": v=v[1:-1]
    env[k.strip()]=v
key=env.get('TASKFORGE_INTERNAL_KEY','')
if len(key)<40:
    Path(out).write_text('TASKFORGE_INTERNAL_KEY missing/short; cannot authenticate Patroni REST\n',encoding='utf-8')
    print('000')
    raise SystemExit(0)
password=hmac.new(key.encode(),b'taskforge-patroni-rest-v2',hashlib.sha512).hexdigest()[:64]
token=base64.b64encode(f'taskforge_patroni:{password}'.encode()).decode()
payload=json.dumps({'leader':leader,'candidate':candidate},separators=(',',':')).encode()
req=urllib.request.Request(
    f'http://{ip}:{int(port)}/switchover',
    data=payload,
    method='POST',
    headers={'Content-Type':'application/json','Authorization':'Basic '+token},
)
code=0; body=b''
try:
    with urllib.request.urlopen(req,timeout=15) as r:
        code=int(r.status); body=r.read(65536)
except urllib.error.HTTPError as e:
    code=int(e.code); body=e.read(65536)
except Exception as e:
    body=(type(e).__name__+': '+str(e)).encode()
Path(out).write_bytes(body)
print(f'{code:03d}' if code else '000')
PY_PATRONI_POST
}
cluster_pg_container(){ printf '%s-postgres-1\n' "$(cluster_project)"; }
cluster_etcd_container(){ printf '%s-etcd-1\n' "$(cluster_project)"; }
cluster_listener_present(){ local bind_ip="$1" port="$2"; ss -H -ltn 2>/dev/null | awk '{print $4}' | grep -Fqx "$bind_ip:$port"; }
cluster_listener_owner(){ local bind_ip="$1" port="$2"; ss -H -ltnp 2>/dev/null | awk -v want="$bind_ip:$port" '$4==want {print; found=1} END{if(!found) exit 1}' || true; }
cluster_wait_listener_absent(){ local bind_ip="$1" port="$2" timeout="${3:-15}" deadline; deadline=$((SECONDS+timeout)); while [ "$SECONDS" -le "$deadline" ]; do cluster_listener_present "$bind_ip" "$port" || return 0; sleep 0.2; done; return 1; }
cluster_retire_replica_postgres_proxy(){
  local bind_ip="$1" port="${2:-5432}" unit='taskforge-postgres-wg-proxy.service' unit_file="/etc/systemd/system/taskforge-postgres-wg-proxy.service" owner
  if systemctl is-active --quiet "$unit" 2>/dev/null || [ -e "$unit_file" ] || [ -L "$unit_file" ]; then
    cluster_log "retiring TaskForge PostgreSQL WG proxy at $bind_ip:$port"
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    cluster_wait_listener_absent "$bind_ip" "$port" 15 || { owner="$(cluster_listener_owner "$bind_ip" "$port")"; cluster_die "managed proxy stopped but listener remains: ${owner:-unknown}"; }
    rm -f "$unit_file"; systemctl daemon-reload >/dev/null 2>&1 || true
  fi
  if cluster_listener_present "$bind_ip" "$port"; then owner="$(cluster_listener_owner "$bind_ip" "$port")"; cluster_die "refusing Patroni start: $bind_ip:$port occupied: ${owner:-unknown}"; fi
}
cluster_listener_conflict(){
  local bind_ip="$1" port="$2"
  ss -H -ltn 2>/dev/null | awk -v ip="$bind_ip" -v p="$port" '''
    {a=$4}
    a==ip ":" p || a=="0.0.0.0:" p || a=="*:" p || a=="[::]:" p || a==":::" p {found=1}
    END{exit found?0:1}'''
}
cluster_listener_conflict_owner(){
  local bind_ip="$1" port="$2"
  ss -H -ltnp 2>/dev/null | awk -v ip="$bind_ip" -v p="$port" '''
    {a=$4}
    a==ip ":" p || a=="0.0.0.0:" p || a=="*:" p || a=="[::]:" p || a==":::" p {print; found=1}
    END{if(!found) exit 1}''' || true
}
cluster_assert_listener_free(){
  local bind_ip="$1" port="$2" purpose="${3:-service}" owner
  if cluster_listener_conflict "$bind_ip" "$port"; then
    owner="$(cluster_listener_conflict_owner "$bind_ip" "$port")"
    cluster_die "refusing $purpose start: $bind_ip:$port occupied/conflicted: ${owner:-unknown}"
  fi
}
cluster_retire_replica_minio_proxy(){
  local bind_ip="$1" api_port="${2:-9000}" console_port="${3:-9001}"
  local unit='taskforge-minio-wg-proxy.service' unit_file='/etc/systemd/system/taskforge-minio-wg-proxy.service'
  local load fragment mainpid owner ownerpid cmdline cgroup proc_root="${TASKFORGE_PROC_ROOT:-/proc}"

  load="$(systemctl show "$unit" -p LoadState --value 2>/dev/null || true)"
  if systemctl is-active --quiet "$unit" 2>/dev/null || [[ -n "$load" && "$load" != not-found ]] || [ -e "$unit_file" ] || [ -L "$unit_file" ]; then
    # If the legacy listener is currently present, prove it belongs to the
    # exact known TaskForge unit before stopping anything. Unknown listeners
    # are never killed by migration code.
    if cluster_listener_present "$bind_ip" "$api_port"; then
      owner="$(cluster_listener_owner "$bind_ip" "$api_port")"
      mainpid="$(systemctl show "$unit" -p MainPID --value 2>/dev/null || true)"
      ownerpid="$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' <<<"$owner" | head -n1)"
      [ -n "$mainpid" ] && [ "$mainpid" != 0 ] || cluster_die "known MinIO WG proxy unit has no MainPID while $bind_ip:$api_port is occupied: ${owner:-unknown}"
      [ -z "$ownerpid" ] || [ "$ownerpid" = "$mainpid" ] || cluster_die "MinIO port owner PID $ownerpid does not match $unit MainPID $mainpid"
      cmdline="$(tr '\0' ' ' < "$proc_root/$mainpid/cmdline" 2>/dev/null || true)"
      cgroup="$(cat "$proc_root/$mainpid/cgroup" 2>/dev/null || true)"
      grep -Fq '/usr/bin/socat' <<<"$cmdline" || cluster_die "refusing to stop $unit: MainPID is not the expected socat process: $cmdline"
      grep -Fq "TCP-LISTEN:$api_port" <<<"$cmdline" || cluster_die "refusing to stop $unit: unexpected listener command: $cmdline"
      grep -Fq "bind=$bind_ip" <<<"$cmdline" || cluster_die "refusing to stop $unit: unexpected bind address: $cmdline"
      grep -Fq "TCP:127.0.0.1:$api_port" <<<"$cmdline" || cluster_die "refusing to stop $unit: unexpected proxy target: $cmdline"
      grep -Fq "/system.slice/$unit" <<<"$cgroup" || cluster_die "refusing to stop $unit: MainPID is outside the expected systemd cgroup"
    fi

    cluster_log "retiring TaskForge MinIO WG proxy at $bind_ip:$api_port"
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    cluster_wait_listener_absent "$bind_ip" "$api_port" 15 || {
      owner="$(cluster_listener_owner "$bind_ip" "$api_port")"
      cluster_die "managed MinIO proxy stopped but listener remains: ${owner:-unknown}"
    }
    fragment="$(systemctl show "$unit" -p FragmentPath --value 2>/dev/null || true)"
    if [ "$fragment" = "$unit_file" ] || [ -e "$unit_file" ] || [ -L "$unit_file" ]; then rm -f "$unit_file"; fi
    systemctl daemon-reload >/dev/null 2>&1 || true
  fi

  cluster_assert_listener_free "$bind_ip" "$api_port" 'MinIO API'
  cluster_assert_listener_free "$bind_ip" "$console_port" 'MinIO console'
}
