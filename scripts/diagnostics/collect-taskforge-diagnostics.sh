#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
out='.'
since='6h'
quick=0
full_history=0
max_log_mb=32
progress_file=''
CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
DOCKER_CONFIG_DIR="${TASKFORGE_DOCKER_CONFIG_DIR:-$CONFIG_ROOT/docker}"
mkdir -p "$DOCKER_CONFIG_DIR"
chmod 700 "$DOCKER_CONFIG_DIR" 2>/dev/null || true
export DOCKER_CONFIG="$DOCKER_CONFIG_DIR"

usage(){
  cat <<'TXT'
Usage: sudo bash ./scripts/diagnostics/collect-taskforge-diagnostics.sh [--out DIR] [--since 6h] [--quick] [--full-logs] [--max-log-mb 32] [--progress-file FILE]

Default: one redacted archive with host/cluster state, Node Agent and Docker journals,
Docker events/resources, safe inspect summaries and bounded recent logs for every TaskForge container.
--quick skips container logs/events.
--full-logs ignores --since and log-size caps for container logs and may produce a very large archive.
--max-log-mb limits each normal container log after collection; the newest bytes are retained.
--progress-file writes an atomic machine-readable progress snapshot for Node Agent/UI polling.
TXT
}

while [ $# -gt 0 ]; do
  case "$1" in
    --out) shift; [ $# -gt 0 ] || { echo 'error: --out requires a value' >&2; exit 2; }; out="$1" ;;
    --since) shift; [ $# -gt 0 ] || { echo 'error: --since requires a value' >&2; exit 2; }; since="$1" ;;
    --quick) quick=1 ;;
    --full-logs) full_history=1 ;;
    --max-log-mb) shift; [ $# -gt 0 ] || { echo 'error: --max-log-mb requires a value' >&2; exit 2; }; max_log_mb="$1" ;;
    --progress-file) shift; [ $# -gt 0 ] || { echo 'error: --progress-file requires a value' >&2; exit 2; }; progress_file="$1" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown diagnostic option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ "$max_log_mb" =~ ^[0-9]+$ ]] && [ "$max_log_mb" -ge 1 ] || { echo 'error: --max-log-mb must be a positive integer' >&2; exit 2; }

journal_since="$since"
if [[ "$since" =~ ^([0-9]+)([smhdw])$ ]]; then
  n="${BASH_REMATCH[1]}"; u="${BASH_REMATCH[2]}"
  case "$u" in
    s) word=seconds;; m) word=minutes;; h) word=hours;; d) word=days;; w) word=weeks;;
  esac
  journal_since="$n $word ago"
fi

stamp="$(date -u +%Y%m%d_%H%M%S)"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
d="$tmp/taskforge_diagnostics_$stamp"
mkdir -p "$d/containers"

completed_items=0
total_items=0
containers_total=0
containers_completed=0
current_index=0
archive_bytes=0

measure_payload(){
  files_collected="$(find "$d" -type f 2>/dev/null | wc -l | tr -d ' ')"
  bytes_collected="$(du -sb "$d" 2>/dev/null | awk '{print $1}' || true)"
  [[ "$files_collected" =~ ^[0-9]+$ ]] || files_collected=0
  [[ "$bytes_collected" =~ ^[0-9]+$ ]] || bytes_collected=0
}

write_progress(){
  local phase="$1" step="$2" current_item="${3:-}" current_index_arg="${4:-0}"
  measure_payload
  local percent_json='null'
  if [ "$total_items" -gt 0 ]; then
    local pct=$(( completed_items * 100 / total_items ))
    [ "$pct" -gt 99 ] && [ "$completed_items" -lt "$total_items" ] && pct=99
    percent_json="$pct"
  fi
  current_index="$current_index_arg"
  if [ -n "$progress_file" ]; then
    mkdir -p "$(dirname "$progress_file")"
    local pf_tmp="${progress_file}.tmp.$$"
    cat > "$pf_tmp" <<EOF
{"version":1,"phase":"$phase","step":"$step","completed":$completed_items,"total":$total_items,"percent":$percent_json,"current_item":"$current_item","current_index":$current_index,"containers_completed":$containers_completed,"containers_total":$containers_total,"files_collected":$files_collected,"bytes_collected":$bytes_collected,"archive_bytes":$archive_bytes,"updated_at":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF
    chmod 600 "$pf_tmp" 2>/dev/null || true
    mv -f "$pf_tmp" "$progress_file"
  fi
  if [ -z "$progress_file" ] || [ "${TASKFORGE_DIAGNOSTICS_PROGRESS_STDERR:-0}" = 1 ]; then
    if [ "$phase" = containers ] && [ "$current_index" -gt 0 ]; then
      printf '[diagnostics] container %s/%s name=%s step=%s completed=%s/%s files=%s bytes=%s\n' "$current_index" "$containers_total" "$current_item" "$step" "$completed_items" "$total_items" "$files_collected" "$bytes_collected" >&2
    elif [ "$phase" = archive ]; then
      printf '[diagnostics] phase=archive step=%s archive_bytes=%s completed=%s/%s\n' "$step" "$archive_bytes" "$completed_items" "$total_items" >&2
    else
      printf '[diagnostics] phase=%s step=%s completed=%s/%s files=%s bytes=%s\n' "$phase" "$step" "$completed_items" "$total_items" "$files_collected" "$bytes_collected" >&2
    fi
  fi
}

write_progress prepare initialize '' 0
{
  echo "collected_at=$(date -Is)"
  echo "hostname=$(hostname)"
  echo "since=$since"
  echo "journal_since=$journal_since"
  echo "quick=$quick"
  echo "full_history=$full_history"
  echo "max_log_mb=$max_log_mb"
  echo "collector_revision=$(tr -d '\r\n' < "$ROOT/REVISION" 2>/dev/null || echo unknown)"
} > "$d/manifest.txt"
completed_items=$((completed_items+1)); write_progress prepare manifest '' 0

{ date -Is; hostname; uptime; free -h; df -h; } > "$d/host.txt" 2>&1 || true
completed_items=$((completed_items+1)); write_progress host host-state '' 0

ip -brief addr > "$d/ip.txt" 2>&1 || true
ip route > "$d/routes.txt" 2>&1 || true
wg show wg-taskforge > "$d/wireguard.txt" 2>&1 || true
completed_items=$((completed_items+1)); write_progress network network-state '' 0

systemctl show taskforge-cluster.service > "$d/agent-systemctl.txt" 2>&1 || true
journalctl -u taskforge-cluster.service --since "$journal_since" --no-pager > "$d/agent-journal.txt" 2>&1 || true
journalctl -u docker.service --since "$journal_since" --no-pager > "$d/docker-journal.txt" 2>&1 || true
completed_items=$((completed_items+1)); write_progress systemd journals '' 0

bash "$ROOT/cluster.sh" status > "$d/status.txt" 2>&1 || true
bash "$ROOT/cluster.sh" doctor > "$d/doctor.txt" 2>&1 || true
completed_items=$((completed_items+1)); write_progress cluster status-doctor '' 0

docker ps -a --no-trunc --filter label=com.docker.compose.project=taskforge-prod > "$d/docker-ps.txt" 2>&1 || true
mapfile -t containers < <(docker ps -a --filter label=com.docker.compose.project=taskforge-prod --format '{{.Names}}' 2>/dev/null | sort)
containers_total="${#containers[@]}"
container_units=1
[ "$quick" -eq 0 ] && container_units=2
base_remaining=2
[ "$quick" -eq 0 ] && base_remaining=3
total_items=$((completed_items + 1 + 1 + containers_total * container_units + base_remaining))
write_progress docker-inventory enumerate '' 0
printf '%s\n' "${containers[@]}" > "$d/container-list.txt"
if [ "${#containers[@]}" -gt 0 ]; then
  docker stats --no-stream --no-trunc "${containers[@]}" > "$d/docker-stats.txt" 2>&1 || true
  docker inspect "${containers[@]}" --format '{{.Name}}|{{.Config.Image}}|{{.Image}}|{{.State.Status}}|{{.State.StartedAt}}|{{.RestartCount}}' > "$d/docker-image-state.txt" 2>&1 || true
else
  : > "$d/docker-stats.txt"; : > "$d/docker-image-state.txt"
fi
docker images --digests --no-trunc > "$d/docker-images.txt" 2>&1 || true
docker volume ls > "$d/docker-volumes.txt" 2>&1 || true
docker network ls > "$d/docker-networks.txt" 2>&1 || true
docker info > "$d/docker-info.txt" 2>&1 || true
completed_items=$((completed_items+1)); write_progress docker-inventory complete '' 0

for f in /var/log/taskforge/migrate-r*.log; do
  [ -f "$f" ] || continue
  tail -n 1500 "$f" >> "$d/migration-logs.txt" 2>/dev/null || true
done
completed_items=$((completed_items+1)); write_progress migration-logs collect '' 0

i=0
for c in "${containers[@]}"; do
  [ -n "$c" ] || continue
  i=$((i+1))
  safe="${c//[^A-Za-z0-9_.-]/_}"
  write_progress containers inspect "$c" "$i"
  docker inspect "$c" 2>/dev/null | python3 -S -c '
import json,sys
raw=json.load(sys.stdin)[0]
st=raw.get("State") or {}; hc=st.get("Health") or {}; cfg=raw.get("Config") or {}; host=raw.get("HostConfig") or {}; net=raw.get("NetworkSettings") or {}
labels=cfg.get("Labels") or {}
safe_labels={k:v for k,v in labels.items() if k.startswith("com.docker.compose.") or k.startswith("com.centurylinklabs.watchtower.")}
out={
 "Name":raw.get("Name"),"ConfiguredImage":cfg.get("Image"),"ImageId":raw.get("Image"),
 "Status":st.get("Status"),"Running":st.get("Running"),"Restarting":st.get("Restarting"),"OOMKilled":st.get("OOMKilled"),"Dead":st.get("Dead"),"ExitCode":st.get("ExitCode"),"Error":st.get("Error"),"StartedAt":st.get("StartedAt"),"FinishedAt":st.get("FinishedAt"),"RestartCount":raw.get("RestartCount"),
 "Health":hc.get("Status","none"),"HealthLog":hc.get("Log",[]),"ReadonlyRootfs":host.get("ReadonlyRootfs"),"RestartPolicy":host.get("RestartPolicy"),"Memory":host.get("Memory"),"MemorySwap":host.get("MemorySwap"),"NanoCpus":host.get("NanoCpus"),
 "Mounts":raw.get("Mounts",[]),"Networks":net.get("Networks",{}),"Labels":safe_labels
}
json.dump(out,sys.stdout,ensure_ascii=False,indent=2);sys.stdout.write("\n")
' > "$d/containers/$safe.inspect.json" 2>&1 || true
  completed_items=$((completed_items+1)); write_progress containers inspect-done "$c" "$i"

  if [ "$quick" -eq 0 ]; then
    write_progress containers logs "$c" "$i"
    log="$d/containers/$safe.log"
    if [ "$full_history" -eq 1 ]; then
      docker logs --timestamps "$c" > "$log" 2>&1 || true
    else
      docker logs --timestamps --since "$since" "$c" > "$log" 2>&1 || true
      max_bytes=$((max_log_mb * 1024 * 1024))
      if [ -f "$log" ] && [ "$(wc -c < "$log")" -gt "$max_bytes" ]; then
        tail -c "$max_bytes" "$log" > "$log.tail" || true
        { printf '[taskforge diagnostics: older log bytes truncated; newest %s MiB retained]\n' "$max_log_mb"; cat "$log.tail"; } > "$log.new"
        mv -f "$log.new" "$log"; rm -f "$log.tail"
      fi
    fi
    completed_items=$((completed_items+1))
  fi
  containers_completed="$i"
  write_progress containers container-done "$c" "$i"
done

if [ "$quick" -eq 0 ]; then
  write_progress docker-events collect '' 0
  if [ "${#containers[@]}" -gt 0 ]; then
    event_args=(--since "$since" --until 1s --filter type=container)
    for c in "${containers[@]}"; do event_args+=(--filter "container=$c"); done
    docker events "${event_args[@]}" --format '{{.Time}} {{.Type}} {{.Action}} {{json .Actor.Attributes}}' > "$d/docker-events.log" 2>&1 || true
  else
    : > "$d/docker-events.log"
  fi
  completed_items=$((completed_items+1)); write_progress docker-events complete '' 0
fi

write_progress redaction sanitize '' 0
python3 -S - "$d" <<'PY'
from pathlib import Path
import re,sys
root=Path(sys.argv[1])
patterns=[
    (re.compile(r'(?i)(\b(?:password|passwd|secret|token|api[_-]?key|authorization|cookie|set-cookie)[A-Za-z0-9_.-]*\b\s*[=:]\s*)([^\s,;\]\[}\{]+)'), r'\1<redacted>'),
    (re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+'), r'\1 <redacted>'),
    (re.compile(r'(?i)(postgres(?:ql)?|mysql|redis|amqp)(://[^:/\s]+:)([^@/\s]+)(@)'), r'\1\2<redacted>\4'),
    (re.compile(r'\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\b'), '<redacted-jwt>'),
]
for p in root.rglob('*'):
    if not p.is_file(): continue
    try: text=p.read_text(encoding='utf-8',errors='replace')
    except OSError: continue
    for rx,repl in patterns: text=rx.sub(repl,text)
    p.write_text(text,encoding='utf-8')
PY
completed_items=$((completed_items+1)); write_progress redaction complete '' 0

mkdir -p "$out"
archive="$out/taskforge_diagnostics_$stamp.tar.gz"
write_progress archive compress '' 0
tar -C "$tmp" -czf "$archive" "taskforge_diagnostics_$stamp" &
tar_pid=$!
while kill -0 "$tar_pid" 2>/dev/null; do
  archive_bytes="$(stat -c '%s' "$archive" 2>/dev/null || echo 0)"
  [[ "$archive_bytes" =~ ^[0-9]+$ ]] || archive_bytes=0
  write_progress archive compress '' 0
  sleep 0.5
done
wait "$tar_pid"
archive_bytes="$(stat -c '%s' "$archive" 2>/dev/null || echo 0)"
[[ "$archive_bytes" =~ ^[0-9]+$ ]] || archive_bytes=0
completed_items=$((completed_items+1)); write_progress archive complete '' 0

owner="${SUDO_USER:-}"
if [ -z "$owner" ] || [ "$owner" = root ]; then
  [ -s "$CONFIG_ROOT/operator-user" ] && owner="$(tr -d '\r\n' < "$CONFIG_ROOT/operator-user")"
fi
if [ -n "$owner" ] && [ "$owner" != root ] && id "$owner" >/dev/null 2>&1; then
  group="$(id -gn "$owner" 2>/dev/null || echo "$owner")"
  chown "$owner:$group" "$archive" 2>/dev/null || true
  chmod 640 "$archive" 2>/dev/null || true
else
  chmod 600 "$archive" 2>/dev/null || true
fi
write_progress completed ready '' 0
printf '%s\n' "$archive"
