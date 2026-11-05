#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
[ "$(id -u)" -eq 0 ] || cluster_die 'apply-unified-runners requires root'
cluster_need_env
cluster_need_config

node="$(cluster_node_id)"
project="$(cluster_project)"
services=(front tasks-api solutions-api execution-api execution-worker cpp-runner pascal-runner python-runner)
legacy_services=(image-cpp-runner image-pascal-runner image-python-runner)
legacy_networks=(image-cpp-runner-net image-pascal-runner-net image-python-runner-net)

echo "[taskforge] unified-runner migration node=$node project=$project"
echo "[taskforge] pulling refreshed application/runner images"
cluster_compose_force pull "${services[@]}"

leader="$(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys,urllib.request
cfg=json.load(open(sys.argv[1])); found=[]
for n in cfg['nodes']:
    try:
        port=(n.get('postgres') or {}).get('patroni_rest_port',8008)
        with urllib.request.urlopen(f"http://{n['wireguard']['ip']}:{port}/primary",timeout=2) as r:
            if r.status==200: found.append(str(n['id']))
    except Exception:
        pass
if len(found)!=1:
    raise SystemExit('cannot determine unique Patroni primary: '+repr(found))
print(found[0])
PY
)"
echo "[taskforge] actual_primary=$leader local_node=$node"

# Recreate every service whose image or network contract changed.  Primary
# stays online; standby nodes only prepare stopped containers for hot start.
if [ "$leader" = "$node" ]; then
  echo '[taskforge] recreating unified-runner services on current Primary'
  cluster_compose_force up -d --force-recreate --no-deps --pull never "${services[@]}"
else
  echo '[taskforge] preparing unified-runner services on standby without starting them'
  for svc in "${services[@]}"; do
    ids="$(docker ps -aq \
      --filter "label=com.docker.compose.project=$project" \
      --filter "label=com.docker.compose.service=$svc")"
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
  done
  cluster_compose_force up --no-start --force-recreate --no-deps --pull never "${services[@]}"
fi

# Retire the three old always-on image runners.  They no longer exist in the
# Compose model; render endpoints are served by cpp/python/pascal-runner.
for svc in "${legacy_services[@]}"; do
  ids="$(docker ps -aq \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.service=$svc")"
  if [ -n "$ids" ]; then
    echo "[taskforge] removing legacy service=$svc"
    docker rm -f $ids >/dev/null
  fi
done

# Old dedicated networks should now be empty.  Remove only networks belonging
# to this exact Compose project and exact legacy Compose network label.
for net in "${legacy_networks[@]}"; do
  ids="$(docker network ls -q \
    --filter "label=com.docker.compose.project=$project" \
    --filter "label=com.docker.compose.network=$net")"
  [ -z "$ids" ] || docker network rm $ids >/dev/null 2>&1 || true
done

echo '[taskforge] unified-runner topology applied'
