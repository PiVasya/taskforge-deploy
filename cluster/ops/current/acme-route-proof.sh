#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
cluster_need_env; cluster_need_config
exec python3 "$CLUSTER_DIR/edge_lifecycle.py" route-check \
  --config "$TASKFORGE_CLUSTER_CONFIG" --env-file "$TASKFORGE_ENV_FILE" \
  --runtime "$TASKFORGE_CLUSTER_RUNTIME" --wait "${1:-180}"
