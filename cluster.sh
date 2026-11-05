#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKFORGE_CONFIG_ROOT="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
TASKFORGE_DOCKER_CONFIG_DIR="${TASKFORGE_DOCKER_CONFIG_DIR:-$TASKFORGE_CONFIG_ROOT/docker}"
if [ -d "$TASKFORGE_DOCKER_CONFIG_DIR" ]; then
  export DOCKER_CONFIG="$TASKFORGE_DOCKER_CONFIG_DIR"
elif [ "$(id -u)" -eq 0 ]; then
  mkdir -p "$TASKFORGE_DOCKER_CONFIG_DIR"
  chmod 700 "$TASKFORGE_DOCKER_CONFIG_DIR" 2>/dev/null || true
  export DOCKER_CONFIG="$TASKFORGE_DOCKER_CONFIG_DIR"
fi
cmd="${1:-help}"; [ $# -eq 0 ] || shift
case "$cmd" in
  version) echo 'TaskForge Cluster Manager v41.5.9 (symmetric Primary control + Cloudflare DNS failover)' ;;
  migrate) bash "$ROOT/cluster/ops/current/migrate.sh" "$@" ;;
  status)
    if [ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ]; then python3 /opt/taskforge/control/cluster/ops/current/status.py; else echo 'v41.5.9 stable control is not installed yet; run sudo bash ./cluster.sh migrate' >&2; exit 2; fi ;;
  doctor)
    if [ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ]; then bash /opt/taskforge/control/cluster/ops/current/doctor.sh; else echo 'v41.5.9 stable control is not installed yet; run sudo bash ./cluster.sh migrate' >&2; exit 2; fi ;;
  primary)
    [ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh primary NODE' >&2; exit 2; }
    [ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ] || { echo 'error: v41.5.9 stable control is not installed yet; run sudo bash ./cluster.sh migrate' >&2; exit 2; }
    target="${1:-}"; [ -n "$target" ] || { echo 'usage: sudo bash ./cluster.sh primary A|B|C' >&2; exit 2; }
    shift || true
    exec python3 /opt/taskforge/control/cluster/primary.py "$target" "$@"
    ;;
  cloudflare)
    sub="${1:-status}"; [ $# -eq 0 ] || shift
    cf="$ROOT/cluster/cloudflare_dns.py"
    case "$sub" in
      setup)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh cloudflare setup' >&2; exit 2; }
        exec python3 "$cf" setup "$@"
        ;;
      status)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh cloudflare status' >&2; exit 2; }
        exec python3 "$cf" status "$@"
        ;;
      preflight|test)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run with sudo' >&2; exit 2; }
        exec python3 "$cf" preflight "$@"
        ;;
      wait)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run with sudo' >&2; exit 2; }
        [ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ] || { echo 'error: v41.5.9 stable control is not installed yet; run sudo bash ./cluster.sh migrate' >&2; exit 2; }
        exec python3 /opt/taskforge/control/cluster/cloudflare_dns.py wait "$@"
        ;;
      reconcile)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run with sudo' >&2; exit 2; }
        [ -s /var/lib/taskforge/cluster/node-id ] || { echo 'error: local node-id missing' >&2; exit 2; }
        node="$(tr -d '\r\n' < /var/lib/taskforge/cluster/node-id)"
        exec python3 "$cf" reconcile --node "$node" --reason manual "$@"
        ;;
      plan)
        python3 "$ROOT/cluster/clusterctl.py" --config /etc/taskforge/cluster.json --env-file /etc/taskforge/taskforge.env cloudflare
        ;;
      *) echo 'usage: sudo bash ./cluster.sh cloudflare setup|status|preflight|wait|reconcile|plan' >&2; exit 2 ;;
    esac
    ;;
  updates)
    exec python3 "$ROOT/cluster/updates_cli.py" "${1:-status}"
    ;;
  optimize)
    [ "$(id -u)" -eq 0 ] || { echo 'error: run with sudo' >&2; exit 2; }
    case "${1:-}" in
      rabbitmq-standby)
        exec bash /opt/taskforge/control/cluster/ops/current/optimize-rabbitmq-standby.sh ;;
      *) echo 'usage: sudo bash /opt/taskforge/control/cluster.sh optimize rabbitmq-standby' >&2; exit 2 ;;
    esac ;;
  compose)
    [ -s /opt/taskforge/control/compose.sh ] || { echo 'error: stable control not installed' >&2; exit 2; }
    exec bash /opt/taskforge/control/compose.sh "$@" ;;
  onec)
    sub="${1:-status}"; [ $# -eq 0 ] || shift
    helper="$ROOT/scripts/onec/configure-source.sh"
    [ -x "$helper" ] || { echo 'error: 1C helper is missing from release' >&2; exit 2; }
    exec bash "$helper" "$sub" "$@"
    ;;
  sql)
    sub="${1:-status}"; [ $# -eq 0 ] || shift
    case "$sub" in
      status)
        exec bash "$ROOT/cluster.sh" status
        ;;
      reconcile)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh sql reconcile' >&2; exit 2; }
        [ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ] || { echo 'error: v41.5.9 stable control is not installed yet; run sudo bash ./cluster.sh migrate' >&2; exit 2; }
        exec bash /opt/taskforge/control/cluster/ops/current/reconcile-sql.sh
        ;;
      *) echo 'usage: sudo bash ./cluster.sh sql reconcile | bash ./cluster.sh sql status' >&2; exit 2 ;;
    esac
    ;;
  logs)
    sub="${1:-clear}"; [ $# -eq 0 ] || shift
    case "$sub" in
      clear|cleanup)
        [ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh logs clear' >&2; exit 2; }
        [ -s /opt/taskforge/control/REVISION ] && [ "$(tr -d '\r\n' < /opt/taskforge/control/REVISION)" = 410509 ] || { echo 'error: v41.5.9 stable control is not installed yet; run sudo bash ./cluster.sh migrate' >&2; exit 2; }
        exec python3 /opt/taskforge/control/cluster/log_cleanup.py "$@"
        ;;
      *) echo 'usage: sudo bash ./cluster.sh logs clear' >&2; exit 2 ;;
    esac
    ;;
  ha)
    sub="${1:-status}"; [ $# -eq 0 ] || shift
    case "$sub" in
      status)
        python3 -m json.tool /var/lib/taskforge/cluster/ha-health.json
        ;;
      policy)
        exec python3 "$ROOT/cluster/ha_policy.py" "${1:-status}"
        ;;
      collect)
        exec python3 "$ROOT/scripts/diagnostics/collect-ha.py" "$@"
        ;;
      *) echo 'usage: cluster.sh ha status | ha policy status | ha policy apply | ha collect' >&2; exit 2 ;;
    esac
    ;;
  diagnostics)
    [ "$(id -u)" -eq 0 ] || { echo 'error: run: sudo bash ./cluster.sh diagnostics [--since 6h] [--out .]' >&2; exit 2; }
    exec bash "$ROOT/scripts/diagnostics/collect-taskforge-diagnostics.sh" "$@" ;;
  help|-h|--help)
cat <<'TXT'
TaskForge Cluster Manager v41.5.9

State-based migration:
  sudo bash ./cluster.sh migrate

v41.5.9 does not execute or require any historical TaskForge release. It discovers
actual live state, imports env/config/secrets when needed, pins the exact live
persistent Docker volumes, installs /opt/taskforge/control, and re-homes only
containers that still depend on an unpacked versioned directory.

Rolling update rule:
  use the SAME v41.5.9 archive everywhere;
  update standby nodes first and the CURRENT Patroni Primary last when possible.
  The migration discovers actual leadership instead of assuming A is Primary.

Primary policy:
  A = full, B = full, C = lite.
  A/B/C may all be Primary. There is no preferred-primary automatic failback.

Choose Primary from ANY v41.5.9 node:
  sudo bash ./cluster.sh primary A
  sudo bash ./cluster.sh primary B
  sudo bash ./cluster.sh primary C

Cloudflare DNS API failover:
  sudo bash ./cluster.sh cloudflare setup
  sudo bash ./cluster.sh cloudflare status
  sudo bash ./cluster.sh cloudflare preflight
  sudo bash ./cluster.sh cloudflare wait

Application updates (automatic on every node):
  sudo bash ./cluster.sh updates status
  sudo bash ./cluster.sh updates check

Diagnostics:
  bash ./cluster.sh status
  bash ./cluster.sh doctor
  sudo bash ./cluster.sh diagnostics --since 6h --out .
  sudo bash ./cluster.sh ha collect --since 24h
  sudo bash ./cluster.sh ha status
  sudo bash ./cluster.sh ha policy status
  sudo bash ./cluster.sh ha policy apply
  sudo bash ./cluster.sh ha policy rollback

HA retry policy (v41.5.9):
  migrate installs the Node Agent on each node; only the CURRENT Primary
  patches the cluster timing policy after two full all-node health checks.
  All agents must be v41.5.9+ first. Existing non-default custom timing is
  preserved until explicitly applying the new policy. No database restart.
  New default ttl=120, loop_wait=10, retry_timeout=45 (seconds).
  Genuine crash failover may therefore take longer. Quorum safety remains.
  ha collect creates its archive in the current working directory.

Server log cleanup (preserves PostgreSQL/etcd Docker logs, volumes and diagnostics archives):
  sudo bash ./cluster.sh logs clear

1C cluster distribution (seed once on current Primary, then normal migrate everywhere):
  sudo bash ./cluster.sh onec seed --file /path/setup-full.run --version X.Y.Z.BUILD
  sudo bash ./cluster.sh onec seed --url URL --version X.Y.Z.BUILD --sha256 HEX
  sudo bash ./cluster.sh onec status
  sudo bash ./cluster.sh onec disable

SQL local runtime repair (full Primary only):
  sudo bash ./cluster.sh sql reconcile
TXT
    ;;
  *) echo "error: unknown command: $cmd" >&2; exit 2 ;;
esac
