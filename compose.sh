#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="${TASKFORGE_CONTROL_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"
STATE_ROOT="${TASKFORGE_STATE_ROOT:-/var/lib/taskforge}"
CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
ENV_FILE="${TASKFORGE_ENV_FILE:-$CONFIG_ROOT/taskforge.env}"
RUNTIME="${TASKFORGE_CLUSTER_RUNTIME:-$STATE_ROOT/cluster}"
[ -s "$ENV_FILE" ] || { echo "error: environment missing: $ENV_FILE" >&2; exit 2; }

set -a
source "$ENV_FILE"
for f in "$CONFIG_ROOT/sql-runtime.env" "$CONFIG_ROOT/local.env" "$CONFIG_ROOT/cpu.env" "$RUNTIME/node.env" "$RUNTIME/app-routing.env"; do [ ! -s "$f" ] || source "$f"; done
set +a

export TASKFORGE_CONTROL_ROOT="$ROOT"
export TASKFORGE_ROOT="$ROOT"
export TASKFORGE_STATE_ROOT="$STATE_ROOT"
export TASKFORGE_CONFIG_ROOT="$CONFIG_ROOT"
export TASKFORGE_CLUSTER_RUNTIME="$RUNTIME"
export TASKFORGE_CLUSTER_DIR="$ROOT/cluster"
export TASKFORGE_CLUSTER_CONFIG="$CONFIG_ROOT/cluster.json"
export TASKFORGE_INVENTORY="$CONFIG_ROOT/inventory.json"
export TASKFORGE_SQL_INIT_ROOT="$ROOT/infrastructure/sql"
append_profile(){
  local profile="$1"
  case ",${COMPOSE_PROFILES:-}," in
    *",$profile,"*) ;;
    *) export COMPOSE_PROFILES="${COMPOSE_PROFILES:+$COMPOSE_PROFILES,}$profile" ;;
  esac
}
if [ "${ONEC_ENABLED:-false}" = true ]; then
  append_profile onec
  export TASKFORGE_ONEC_WORK_DIR="${TASKFORGE_ONEC_WORK_DIR:-$STATE_ROOT/onec-work}"
  export ONEC_LICENSE_DIR="${ONEC_LICENSE_DIR:-$CONFIG_ROOT/onec/licenses}"
  export ONEC_INSTALL_FILE="${ONEC_INSTALL_FILE:-$CONFIG_ROOT/onec/installer.run}"
  mkdir -p "$TASKFORGE_ONEC_WORK_DIR" "$ONEC_LICENSE_DIR" "$(dirname "$ONEC_INSTALL_FILE")"
  if [ "$(id -u)" -eq 0 ]; then
    chown 65534:65534 "$TASKFORGE_ONEC_WORK_DIR"
    chmod 0700 "$TASKFORGE_ONEC_WORK_DIR"
    chown root:65534 "$ONEC_LICENSE_DIR"
    chmod 0750 "$ONEC_LICENSE_DIR"
    find "$ONEC_LICENSE_DIR" -maxdepth 1 -type f -exec chown root:65534 {} + -exec chmod 0640 {} + 2>/dev/null || true
  fi
fi
if [ "${TASKFORGE_APP_PROFILE:-full}" = full ] && [ "${SQL_ENABLED:-false}" = true ]; then
  append_profile sql
fi
export TASKFORGE_POSTGRES_INIT_DIR="$ROOT/infrastructure/postgres/init"
export TASKFORGE_IMAGE_ANALYZER_MODEL_CACHE_DIR="$STATE_ROOT/image-analyzer-model-cache"
export CODE_ANALYZER_PRIVATE_KEY_PATH="$CONFIG_ROOT/code-analyzer/code-analyzer-private.pem"
export CODE_ANALYZER_PUBLIC_KEY_PATH="$CONFIG_ROOT/code-analyzer/code-analyzer-public.pem"
export TASKFORGE_CONTENT_DP_DIR="$STATE_ROOT/data-protection/content-data-protection"
export TASKFORGE_QUIZ_DP_DIR="$STATE_ROOT/data-protection/quiz-data-protection"
export IMAGE_ANALYZER_MEM_LIMIT=6g
export IMAGE_ANALYZER_MEMSWAP_LIMIT=8g
mkdir -p "$STATE_ROOT/image-analyzer-model-cache" "$CONFIG_ROOT/code-analyzer" "$STATE_ROOT/data-protection/content-data-protection" "$STATE_ROOT/data-protection/quiz-data-protection" "$RUNTIME"
chmod 0777 "$STATE_ROOT/image-analyzer-model-cache" 2>/dev/null || true

DOCKER_CFG="$CONFIG_ROOT/docker/config.json"
mkdir -p "$CONFIG_ROOT/docker"
export DOCKER_CONFIG="$CONFIG_ROOT/docker"
if [ -f "$DOCKER_CFG" ]; then export TASKFORGE_WATCHTOWER_DOCKER_CONFIG="$DOCKER_CFG"; fi

args=(--project-name "${COMPOSE_PROJECT_NAME:-taskforge-prod}" --env-file "$ENV_FILE")
shopt -s nullglob
files=("$ROOT"/compose/*.yaml "$ROOT"/compose/*.yml)
[ "${#files[@]}" -gt 0 ] || { echo 'error: no compose fragments installed' >&2; exit 2; }
IFS=$'\n' files=($(printf '%s\n' "${files[@]}" | sort)); unset IFS
for f in "${files[@]}"; do args+=(-f "$f"); done

if [ -f "$DOCKER_CFG" ] && grep -Eq '^[[:space:]]{2}watchtower:' "${files[@]}"; then
  auth_override="$RUNTIME/watchtower-auth.yaml"; tmp="${auth_override}.tmp.$$"
  cat > "$tmp" <<'YAML'
services:
  watchtower:
    volumes:
      - ${TASKFORGE_WATCHTOWER_DOCKER_CONFIG:?set TASKFORGE_WATCHTOWER_DOCKER_CONFIG}:/config.json:ro
YAML
  chmod 600 "$tmp"; mv -f "$tmp" "$auth_override"; args+=(-f "$auth_override")
fi

if [ "${TASKFORGE_DISABLE_CLUSTER_OVERLAY:-0}" != 1 ] && { [ "${TASKFORGE_CLUSTER_FORCE:-0}" = 1 ] || [ -f "$RUNTIME/enabled" ]; }; then
  export TASKFORGE_CLUSTER_ENABLED=true
  args+=(-f "$ROOT/cluster/compose.cluster.yaml")
else
  export TASKFORGE_CLUSTER_ENABLED=false
fi
args+=(-f "$ROOT/cluster/compose.telemetry.yaml")
# State-based migration pins logical volumes to the exact live Docker volumes.
# This keeps data identity independent of the source release/project layout.
if [ -s "$RUNTIME/storage-bindings.yaml" ]; then args+=(-f "$RUNTIME/storage-bindings.yaml"); fi
# The application updater supplies an invocation-local immutable image override.
# It is appended after every cluster overlay; it never replaces persistent config.
if [ -n "${TASKFORGE_COMPOSE_OVERRIDE:-}" ]; then
  [ -f "$TASKFORGE_COMPOSE_OVERRIDE" ] || { echo 'error: update override missing' >&2; exit 2; }
  args+=(-f "$TASKFORGE_COMPOSE_OVERRIDE")
fi
exec docker compose "${args[@]}" "$@"
