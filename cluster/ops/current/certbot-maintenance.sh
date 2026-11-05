#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
cluster_need_env; cluster_need_config
mode="${1:-ensure}"
case "$mode" in
  ensure|renew)
    # A manual invocation uses the same DNS/route/lease gates as the Agent.
    # 'renew' is intentionally not --force-renewal.
    exec python3 "$CLUSTER_DIR/edge_lifecycle.py" once \
      --config "$TASKFORGE_CLUSTER_CONFIG" --env-file "$TASKFORGE_ENV_FILE" \
      --runtime "$TASKFORGE_CLUSTER_RUNTIME"
    ;;
  route-check)
    exec python3 "$CLUSTER_DIR/edge_lifecycle.py" route-check \
      --config "$TASKFORGE_CLUSTER_CONFIG" --env-file "$TASKFORGE_ENV_FILE" \
      --runtime "$TASKFORGE_CLUSTER_RUNTIME"
    ;;
  *) cluster_die 'usage: certbot-maintenance.sh ensure|renew|route-check' ;;
esac
