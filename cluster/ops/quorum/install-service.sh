#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
start=1
while [ $# -gt 0 ]; do
  case "$1" in --no-start) start=0 ;; *) cluster_die "unknown install-service option: $1";; esac
  shift
done
[ "$(id -u)" -eq 0 ] || cluster_die 'run with sudo/root'
cluster_need_env; cluster_need_config; [ -s "$TASKFORGE_CLUSTER_RUNTIME/node-id" ] || cluster_die 'node-id missing'
cat > /etc/systemd/system/taskforge-cluster.service <<UNIT
[Unit]
Description=TaskForge Cluster Node Agent v41.5.9
After=docker.service network-online.target wg-quick@wg-taskforge.service
Wants=docker.service network-online.target wg-quick@wg-taskforge.service
Requires=docker.service

[Service]
Type=simple
Environment=TASKFORGE_STATE_ROOT=$TASKFORGE_STATE_ROOT
Environment=TASKFORGE_CONFIG_ROOT=$TASKFORGE_CONFIG_ROOT
Environment=TASKFORGE_CLUSTER_RUNTIME=$TASKFORGE_CLUSTER_RUNTIME
ExecStart=/usr/bin/python3 /opt/taskforge/control/cluster/agent.py --config $TASKFORGE_CLUSTER_CONFIG --env-file $TASKFORGE_ENV_FILE
ExecStopPost=/usr/bin/rm -f $TASKFORGE_CLUSTER_RUNTIME/readiness/traffic-ready $TASKFORGE_CLUSTER_RUNTIME/node-agent.sock
Restart=always
RestartSec=3
WorkingDirectory=/opt/taskforge/control
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable taskforge-cluster.service >/dev/null
if [ "$start" -eq 1 ]; then systemctl restart taskforge-cluster.service; else systemctl stop taskforge-cluster.service >/dev/null 2>&1 || true; fi
