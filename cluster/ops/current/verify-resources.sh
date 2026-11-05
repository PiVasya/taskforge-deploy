#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
cluster_need_env; cluster_need_config
node="${1:-$(cluster_node_id)}"
profile="$(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" "$node" <<'PY'
import json,sys
n=next(n for n in json.load(open(sys.argv[1]))['nodes'] if str(n['id'])==sys.argv[2])
print((n.get('app') or {}).get('profile','full'))
PY
)"
[ "$profile" = full ] || { echo "PASS: image-analyzer resource smoke skipped for profile=$profile"; exit 0; }
project="$(cluster_project)"; c="$project-image-analyzer-1"
docker inspect "$c" >/dev/null 2>&1 || cluster_die "image-analyzer container is not prepared: $c"
read -r mem swap <<<"$(docker inspect -f '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' "$c")"
min_mem=$((6*1024*1024*1024)); min_swap=$((8*1024*1024*1024))
[ "${mem:-0}" -ge "$min_mem" ] || cluster_die "image-analyzer memory contract too small: got=$mem required>=$min_mem"
[ "${swap:-0}" -ge "$min_swap" ] || cluster_die "image-analyzer memswap contract too small: got=$swap required>=$min_swap"
state="$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)"
[ "$state" != running ] || docker stop -t 20 "$c" >/dev/null
before_restart="$(docker inspect -f '{{.RestartCount}}' "$c")"
cleanup(){ docker stop -t 20 "$c" >/dev/null 2>&1 || true; }
trap cleanup EXIT
docker start "$c" >/dev/null || cluster_die 'cannot start image-analyzer for r55 smoke verification'
healthy=0
for _ in $(seq 1 120); do
  status="$(docker inspect -f '{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}|{{.State.OOMKilled}}|{{.RestartCount}}' "$c" 2>/dev/null || true)"
  IFS='|' read -r st health oom restarts <<<"$status"
  [ "$oom" != true ] || cluster_die 'image-analyzer was OOMKilled during model load'
  [ "${restarts:-0}" -le "${before_restart:-0}" ] || cluster_die "image-analyzer restarted during model load: before=$before_restart now=$restarts"
  if [ "$st" = running ] && [ "$health" = healthy ]; then healthy=1; break; fi
  [ "$st" = running ] || cluster_die "image-analyzer exited during model load: state=${st:-missing} health=${health:-missing}"
  sleep 2
done
[ "$healthy" -eq 1 ] || cluster_die 'image-analyzer did not become healthy within 240 seconds'
final="$(docker inspect -f '{{.State.OOMKilled}}|{{.RestartCount}}' "$c")"
IFS='|' read -r oom restarts <<<"$final"
[ "$oom" != true ] || cluster_die 'image-analyzer OOMKilled after health transition'
[ "$restarts" = "$before_restart" ] || cluster_die "image-analyzer restart count changed during smoke: before=$before_restart after=$restarts"
docker stop -t 20 "$c" >/dev/null
trap - EXIT
[ "$(docker inspect -f '{{.State.Status}}' "$c" 2>/dev/null || true)" != running ] || cluster_die 'image-analyzer remained running after standby smoke test'
echo "PASS: image-analyzer memory=$mem memswap=$swap health=healthy oom=false restarts=$restarts and returned to stopped standby state"
