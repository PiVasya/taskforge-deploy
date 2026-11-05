#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
[ "$(id -u)" -eq 0 ] || cluster_die 'rehome-node requires root'
cluster_need_env; cluster_need_config
node="$(cluster_node_id)"
current_leader(){ python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys,urllib.request
cfg=json.load(open(sys.argv[1]));found=[]
for n in cfg['nodes']:
    try:
        with urllib.request.urlopen(f"http://{n['wireguard']['ip']}:{(n.get('postgres') or {}).get('patroni_rest_port',8008)}/primary",timeout=2) as r:
            if r.status==200:found.append(str(n['id']))
    except Exception:pass
if len(found)!=1:raise SystemExit('cannot determine unique Patroni primary: '+repr(found))
print(found[0])
PY
}
archive_legacy_transactions(){
  mkdir -p "$TASKFORGE_CLUSTER_RUNTIME/easy/legacy-transactions"
  local f base
  shopt -s nullglob
  for f in "$TASKFORGE_CLUSTER_RUNTIME"/primary-rehome-r*.json "$TASKFORGE_CLUSTER_RUNTIME"/migrate-r*.json; do
    [ "$f" = "$TASKFORGE_CLUSTER_RUNTIME/primary-rehome-r55.json" ] && continue
    [ "$f" = "$TASKFORGE_CLUSTER_RUNTIME/migrate-r55.json" ] && continue
    base="$(basename "$f")"
    mv -f "$f" "$TASKFORGE_CLUSTER_RUNTIME/easy/legacy-transactions/${base%.json}-ignored-by-r55-$(date -u +%Y%m%dT%H%M%SZ).json" 2>/dev/null || true
  done
  shopt -u nullglob
}
adopt_stable_if_possible(){
  bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh >/dev/null 2>&1 || return 1
  rm -f "$TASKFORGE_CLUSTER_RUNTIME/join-rehome-fence" "$TASKFORGE_CLUSTER_RUNTIME/migration-fence"
  printf 'joined %s node=%s source=state-adopt\n' "$(date -u +%FT%TZ)" "$node" > "$TASKFORGE_CLUSTER_RUNTIME/join-complete"
  printf 'hot %s node=%s source=state-adopt\n' "$(date -u +%FT%TZ)" "$node" > "$TASKFORGE_CLUSTER_RUNTIME/hot-standby-complete"
  chmod 600 "$TASKFORGE_CLUSTER_RUNTIME/join-complete" "$TASKFORGE_CLUSTER_RUNTIME/hot-standby-complete"
  bash /opt/taskforge/control/cluster/ops/quorum/install-service.sh --no-start
  systemctl restart taskforge-cluster.service >/dev/null || cluster_die 'cannot restart v41.5.9 Agent while adopting stable runtime'
  if [ -s "$TASKFORGE_CLUSTER_RUNTIME/migrate-r55.json" ]; then
    mkdir -p "$TASKFORGE_CLUSTER_RUNTIME/easy"
    mv -f "$TASKFORGE_CLUSTER_RUNTIME/migrate-r55.json" "$TASKFORGE_CLUSTER_RUNTIME/easy/migrate-r55-state-adopted-$(date -u +%Y%m%dT%H%M%SZ).json"
  fi
  archive_legacy_transactions
  echo 'PASS: actual Docker/systemd mounts are already version-independent; no container re-home required'
  return 0
}

if [ -s "$TASKFORGE_CLUSTER_RUNTIME/primary-rehome-r55.json" ]; then
  bash "$CLUSTER_DIR/ops/current/rehome-primary.sh"
elif adopt_stable_if_possible; then
  :
else
  leader="$(current_leader)"
  echo "actual_primary=$leader local_node=$node stable_mounts=no"
  if [ "$leader" = "$node" ]; then bash "$CLUSTER_DIR/ops/current/rehome-primary.sh"; else bash "$CLUSTER_DIR/ops/current/rehome-replica.sh"; fi
  archive_legacy_transactions
fi
bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh
