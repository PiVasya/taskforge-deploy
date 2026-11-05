#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
[ "$(id -u)" -eq 0 ] || cluster_die 'apply-onec-runtime requires root'
cluster_need_env
cluster_need_config

node="$(cluster_node_id)"
project="$(cluster_project)"
services=(front tasks-api solutions-api execution-api execution-worker ai-worker code-analyzer)
onec_enabled=false

read_env(){ cluster_read_env "$1"; }

prepare_onec_volumes(){
  local image repo tag version sha source url source_file components license_dir work_dir
  local platform_volume runtime_volume cache_volume network_mode
  repo="$(read_env IMAGE_REPOSITORY)"; [ -n "$repo" ] || cluster_die 'IMAGE_REPOSITORY is required'
  tag="$(read_env IMAGE_TAG)"; tag="${tag:-develop}"
  image="$repo/onec-runner:$tag"
  version="$(read_env ONEC_PLATFORM_VERSION)"
  sha="$(read_env ONEC_INSTALL_SHA256)"
  source="$(read_env ONEC_INSTALL_SOURCE)"; source="${source:-auto}"
  url="$(read_env ONEC_INSTALL_URL)"
  source_file="$(read_env ONEC_INSTALL_FILE)"; source_file="${source_file:-/etc/taskforge/onec/installer.run}"
  components="$(read_env ONEC_INSTALL_COMPONENTS)"; components="${components:-client_full,client_thin_fib,ru}"
  license_dir="$(read_env ONEC_LICENSE_DIR)"; license_dir="${license_dir:-/etc/taskforge/onec/licenses}"
  work_dir="$(read_env TASKFORGE_ONEC_WORK_DIR)"; work_dir="${work_dir:-/var/lib/taskforge/onec-work}"

  [ -n "$version" ] || cluster_die 'ONEC_ENABLED=true requires ONEC_PLATFORM_VERSION'
  [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || cluster_die 'ONEC_PLATFORM_VERSION must use exact A.B.C.D numeric form'
  [ -n "$sha" ] || cluster_die 'ONEC_ENABLED=true requires ONEC_INSTALL_SHA256'
  case "$sha" in *[!0-9a-fA-F]*|'') cluster_die 'ONEC_INSTALL_SHA256 must be a hexadecimal SHA-256' ;; esac
  [ "${#sha}" -eq 64 ] || cluster_die 'ONEC_INSTALL_SHA256 must contain exactly 64 hex characters'

  case "$work_dir" in /var/lib/taskforge/*) ;; *) cluster_die 'TASKFORGE_ONEC_WORK_DIR must stay under /var/lib/taskforge/' ;; esac
  case "$license_dir" in /etc/taskforge/onec/*|/etc/taskforge/onec) ;; *) cluster_die 'ONEC_LICENSE_DIR must stay under /etc/taskforge/onec/' ;; esac
  mkdir -p "$license_dir" "$work_dir" "$(dirname "$source_file")"
  chown 65534:65534 "$work_dir"
  chmod 0700 "$work_dir"
  chown root:65534 "$license_dir"
  chmod 0750 "$license_dir"
  find "$license_dir" -maxdepth 1 -type f -exec chown root:65534 {} + -exec chmod 0640 {} + 2>/dev/null || true

  platform_volume="${project}_onec-platform"
  runtime_volume="${project}_onec-runtime"
  cache_volume="${project}_onec-installer-cache"
  docker volume create "$platform_volume" >/dev/null
  docker volume create "$runtime_volume" >/dev/null
  docker volume create "$cache_volume" >/dev/null

  # Prefer a supplied local installer in auto mode; otherwise the bootstrap
  # may fetch the configured URL. Only the one-shot bootstrap gets internet.
  network_mode=none
  local -a source_args=()
  case "$source" in
    file)
      [ -s "$source_file" ] || cluster_die "ONEC_INSTALL_SOURCE=file but installer is missing: $source_file"
      source_args=(-v "$source_file:/source/installer.run:ro" -e ONEC_INSTALL_FILE=/source/installer.run)
      ;;
    url)
      [ -n "$url" ] || cluster_die 'ONEC_INSTALL_SOURCE=url requires ONEC_INSTALL_URL'
      network_mode=bridge
      ;;
    auto)
      if [ -s "$source_file" ]; then
        source_args=(-v "$source_file:/source/installer.run:ro" -e ONEC_INSTALL_FILE=/source/installer.run)
      elif [ -n "$url" ]; then
        network_mode=bridge
      fi
      ;;
    *) cluster_die "unsupported ONEC_INSTALL_SOURCE=$source" ;;
  esac

  cluster_log "bootstrapping 1C platform node=$node version=$version source=$source"
  docker run --rm --user 0:0 --network "$network_mode" \
    -v "$platform_volume:/opt/1cv8" \
    -v "$runtime_volume:/opt/taskforge/onec/runtime" \
    -v "$cache_volume:/installer-cache" \
    -v "$license_dir:/var/1C/licenses:ro" \
    "${source_args[@]}" \
    -e "ONEC_PLATFORM_VERSION=$version" \
    -e "ONEC_INSTALL_SOURCE=$source" \
    -e "ONEC_INSTALL_URL=$url" \
    -e "ONEC_INSTALL_SHA256=$sha" \
    -e "ONEC_INSTALL_COMPONENTS=$components" \
    --entrypoint /opt/taskforge/onec/bootstrap \
    "$image"

  # Probe with the exact read-only mounts and unprivileged UID used by the real
  # service. This catches broken platform/template permissions before Compose.
  local -a runtime_mounts=(
    -v "$platform_volume:/opt/1cv8:ro"
    -v "$runtime_volume:/opt/taskforge/onec/runtime:ro"
    -v "$license_dir:/var/1C/licenses:ro"
    -v "$work_dir:/work"
    -e ONEC_EXECUTABLE=/opt/1cv8/taskforge/1cv8
    -e ONEC_TEMPLATE_DIR=/opt/taskforge/onec/runtime/templates/code
    -e ONEC_JUDGE_SOURCE_DIR=/opt/taskforge/onec/judge-src
    -e ONEC_WORK_ROOT=/work
    -e ONEC_USE_XVFB=true
  )

  docker run --rm --network none --user 65534:65534 \
    "${runtime_mounts[@]}" \
    --entrypoint /opt/taskforge/onec/driver \
    "$image" --probe

  # Do not accept a platform that merely has files in the right places. Compile
  # a real job-local EPF through Designer and execute it through ENTERPRISE using
  # the exact unprivileged/read-only runtime mounts the service will use.
  local smoke_request smoke_output
  smoke_request="$(python3 - <<'PYSMOKE'
import json
print(json.dumps({
  "schema": "taskforge-onec-driver-v1",
  "mode": "tests",
  "code": "Функция Решение(Вход) Экспорт\n    Возврат Вход;\nКонецФункции",
  "timeLimitMs": 20000,
  "tests": [{"input": "taskforge-onec-smoke", "expectedOutput": "taskforge-onec-smoke", "isHidden": True}],
}, ensure_ascii=False))
PYSMOKE
)"
  smoke_output="$(printf '%s\n' "$smoke_request" | docker run --rm -i --network none --user 65534:65534 \
    "${runtime_mounts[@]}" \
    --entrypoint /opt/taskforge/onec/driver \
    "$image")" || cluster_die '1C end-to-end smoke execution failed'
  printf '%s\n' "$smoke_output" | python3 -c 'import json,sys; x=json.load(sys.stdin); r=x.get("results") or []; assert len(r)==1 and r[0].get("passed") is True and r[0].get("status")=="ok"' \
    || cluster_die '1C end-to-end smoke returned an invalid verdict'
  cluster_log "1C persistent runtime ready node=$node version=$version smoke=PASS"
}

# Adopt cluster-wide 1C desired state and ensure this node has a verified
# installer copy in its own MinIO before deciding whether onec-runner belongs
# in the local application set. If the cluster has no 1C desired state this is
# a no-op and keeps ONEC_ENABLED=false.
bash "$CLUSTER_DIR/ops/current/onec-distribution.sh" sync

if grep -qi '^ONEC_ENABLED=true$' "$TASKFORGE_ENV_FILE" 2>/dev/null; then
  onec_enabled=true
  services+=(onec-runner)
fi

echo "[taskforge] 1C auto-install migration node=$node project=$project onec_enabled=$onec_enabled"
if [ "$onec_enabled" != true ]; then
  # v41.5.2 forcibly recreated seven perfectly healthy containers here, even
  # with 1C disabled. Image updates now belong exclusively to the Node Agent.
  echo '[taskforge] 1C disabled; no forced application pulls/recreations'
  exit 0
fi
echo '[taskforge] 1C enabled; preparing the licensed runtime and related services'
cluster_compose_force pull "${services[@]}"

if [ "$onec_enabled" = true ]; then
  prepare_onec_volumes
fi

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

if [ "$leader" = "$node" ]; then
  echo '[taskforge] recreating 1C-aware services on current Primary'
  cluster_compose_force stop execution-worker >/dev/null 2>&1 || true
  cluster_compose_force up -d --force-recreate --no-deps --pull never "${services[@]}"
  if [ "$onec_enabled" = true ]; then
    cname="$project-onec-runner-1"
    ready=false
    for _ in $(seq 1 120); do
      state="$(docker inspect -f '{{with index .State "Health"}}{{.Status}}{{else}}{{.State.Status}}{{end}}' "$cname" 2>/dev/null || true)"
      if [ "$state" = healthy ]; then ready=true; break; fi
      if [ "$state" = exited ] || [ "$state" = dead ]; then break; fi
      sleep 1
    done
    [ "$ready" = true ] || { docker logs --tail 160 "$cname" 2>&1 || true; cluster_die 'enabled 1C runner did not become healthy'; }
    echo '[taskforge] enabled 1C runner health=healthy'
  fi
else
  echo '[taskforge] preparing 1C-aware services on standby without starting them'
  for svc in "${services[@]}"; do
    ids="$(docker ps -aq \
      --filter "label=com.docker.compose.project=$project" \
      --filter "label=com.docker.compose.service=$svc")"
    [ -z "$ids" ] || docker rm -f $ids >/dev/null
  done
  cluster_compose_force up --no-start --force-recreate --no-deps --pull never "${services[@]}"
fi

echo '[taskforge] 1C auto-install runtime applied'
