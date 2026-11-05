#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
source "$ROOT/cluster/lib/common.sh"
[ "$(id -u)" -eq 0 ] || cluster_die 'minio-image selection requires root'
cluster_need_env
CPU_ENV="$TASKFORGE_CONFIG_ROOT/cpu.env"
project="$(cluster_project)"
container="$project-minio-1"
default_image='minio/minio:RELEASE.2025-04-22T22-12-26Z'

read_key(){
  local file="$1" key="$2"
  [ -s "$file" ] || return 0
  python3 -S - "$file" "$key" <<'PY'
from pathlib import Path
import sys
p=Path(sys.argv[1]); key=sys.argv[2]
for raw in p.read_text(encoding='utf-8-sig').splitlines():
    if raw.lstrip().startswith('#') or '=' not in raw: continue
    k,v=raw.split('=',1)
    if k.strip()==key:
        v=v.strip()
        if len(v)>=2 and v[:1]==v[-1:] and v[0] in "\"'": v=v[1:-1]
        print(v); break
PY
}
write_cpu_override(){
  local image="$1"
  mkdir -p "$TASKFORGE_CONFIG_ROOT"
  python3 -S - "$CPU_ENV" "$image" <<'PY'
from pathlib import Path
import os,sys,tempfile
p=Path(sys.argv[1]); image=sys.argv[2]
rows=[]
if p.exists():
    rows=p.read_text(encoding='utf-8-sig').splitlines()
out=[]; done=False
for raw in rows:
    if '=' in raw and not raw.lstrip().startswith('#') and raw.split('=',1)[0].strip()=='MINIO_IMAGE':
        if not done: out.append('MINIO_IMAGE='+image); done=True
    else: out.append(raw)
if not done: out.append('MINIO_IMAGE='+image)
p.parent.mkdir(parents=True,exist_ok=True)
fd,tmp=tempfile.mkstemp(prefix='cpu.env.',dir=str(p.parent),text=True)
with os.fdopen(fd,'w') as f: f.write('\n'.join(out).rstrip()+'\n')
os.chmod(tmp,0o600); os.replace(tmp,p)
PY
  cluster_chown_owner "$CPU_ENV" 2>/dev/null || true
}
container_image(){ docker inspect -f '{{.Config.Image}}' "$container" 2>/dev/null || true; }
container_state(){ docker inspect -f '{{with index .State "Health"}}{{.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true; }
container_logs(){ docker logs --tail 80 "$container" 2>&1 || true; }
cpv1_variant(){
  local image="$1"
  case "$image" in
    *-cpuv1) printf '%s\n' "$image" ;;
    *@*) return 1 ;;
    *:*) printf '%s-cpuv1\n' "$image" ;;
    *) return 1 ;;
  esac
}
host_needs_cpuv1(){
  [ "$(uname -m)" = x86_64 ] || return 1
  local flags req
  flags=" $(awk -F: '/^flags[[:space:]]*:/{print $2; exit}' /proc/cpuinfo 2>/dev/null) "
  [ -n "$flags" ] || return 1
  for req in cx16 lahf_lm popcnt pni ssse3 sse4_1 sse4_2; do
    case "$flags" in *" $req "*) ;; *) return 0;; esac
  done
  return 1
}
ensure_local_image(){
  local image="$1"
  docker image inspect "$image" >/dev/null 2>&1 && return 0
  cluster_log "pulling required MinIO image $image"
  docker pull "$image" >/dev/null || cluster_die "required MinIO image is not local and pull failed: $image"
}

existing="$(read_key "$CPU_ENV" MINIO_IMAGE)"
live="$(container_image)"
state="$(container_state)"
selected=''; reason=''

# A currently healthy container is the strongest hardware-compatibility proof.
base="$(read_key "$TASKFORGE_ENV_FILE" MINIO_IMAGE)"; base="${base:-$default_image}"
logs="$(container_logs)"
if [ -n "$live" ] && [ "$state" = healthy ]; then
  selected="$live"; reason='proven-live-container'
elif grep -Fq 'CPU does not support x86-64-v2' <<<"$logs"; then
  selected="$(cpv1_variant "${live:-${existing:-$base}}")" || cluster_die "MinIO reports missing x86-64-v2 but cpuv1 variant cannot be derived"
  reason='observed-x86-64-v2-failure'
elif [ -n "$existing" ]; then
  if host_needs_cpuv1 && [[ "$existing" != *-cpuv1 ]]; then
    selected="$(cpv1_variant "$existing")" || cluster_die "stable MinIO override is incompatible with this CPU and cpuv1 variant cannot be derived from $existing"
    reason='corrected-stale-hardware-override'
  else
    selected="$existing"; reason='stable-hardware-override'
  fi
elif host_needs_cpuv1; then
  selected="$(cpv1_variant "$base")" || cluster_die "host lacks x86-64-v2 but cpuv1 variant cannot be derived from $base"
  reason='host-cpu-capability'
elif [ -n "$live" ]; then
  selected="$live"; reason='existing-container-image'
else
  selected="$base"; reason='configured-default'
fi

[ -n "$selected" ] || cluster_die 'cannot select MinIO image'
ensure_local_image "$selected"
write_cpu_override "$selected"
echo "PASS: MinIO image selected image=$selected reason=$reason state=${state:-missing}"
