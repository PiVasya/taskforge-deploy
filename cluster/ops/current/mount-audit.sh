#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
project="$(cluster_project)"
ids="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
old=''
if [ -n "$ids" ]; then
  old="$(printf '%s\n' "$ids" | xargs -r docker inspect -f '{{.Name}}|{{range .Mounts}}{{if eq .Type "bind"}}{{.Source}}->{{.Destination}}|{{end}}{{end}}|wd={{index .Config.Labels "com.docker.compose.project.working_dir"}}|cfg={{index .Config.Labels "com.docker.compose.project.config_files"}}' 2>/dev/null | grep -E '(taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+)' || true)"
fi
[ -z "$old" ] || { echo 'error: versioned release paths still active in Docker:' >&2; printf '%s\n' "$old" >&2; exit 2; }
unit="$(systemctl cat taskforge-cluster.service 2>/dev/null || true)"
if printf '%s\n' "$unit" | grep -Eq '(taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+)'; then echo 'error: systemd Agent still references a versioned release' >&2; exit 2; fi
printf '%s\n' "$unit" | grep -Fq '/opt/taskforge/control/cluster/agent.py' || { echo 'error: systemd Agent is not installed from stable control path' >&2; exit 2; }
if systemctl is-active --quiet taskforge-minio-wg-proxy.service 2>/dev/null; then
  echo 'error: legacy taskforge-minio-wg-proxy.service is still active' >&2
  exit 2
fi
if systemctl is-enabled --quiet taskforge-minio-wg-proxy.service 2>/dev/null; then
  echo 'error: legacy taskforge-minio-wg-proxy.service is still enabled' >&2
  exit 2
fi
for f in "$TASKFORGE_ENV_FILE" "$TASKFORGE_CONFIG_ROOT/local.env" "$TASKFORGE_CONFIG_ROOT/cpu.env" "$TASKFORGE_CLUSTER_CONFIG" "$TASKFORGE_INVENTORY" "$TASKFORGE_CLUSTER_RUNTIME/node.env" "$TASKFORGE_CLUSTER_RUNTIME/app-routing.env" "$TASKFORGE_CLUSTER_RUNTIME/patroni.yml"; do
  [ -f "$f" ] || continue
  if grep -Eq '(taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+)' "$f"; then echo "error: active stable runtime references versioned release: $f" >&2; exit 2; fi
done
# Exact stable bind contracts that matter after the old unpacked directories are deleted.
pg="$(cluster_pg_container)"
if docker inspect "$pg" >/dev/null 2>&1; then
  docker inspect "$pg" | python3 -S -c 'import json,sys
want={"/run/taskforge-cluster":sys.argv[1],"/run/taskforge-patroni-wrapper.sh":sys.argv[2],"/docker-entrypoint-initdb.d":sys.argv[3]}
d=json.load(sys.stdin)[0]; seen={m.get("Destination"):m.get("Source") for m in d.get("Mounts",[]) if m.get("Type")=="bind"}; bad={k:(seen.get(k),v) for k,v in want.items() if seen.get(k)!=v};
assert not bad,"postgres stable bind mismatch: "+repr(bad)' "$TASKFORGE_STATE_ROOT/cluster" "$TASKFORGE_ROOT/cluster/ops/quorum/patroni-container-entrypoint.sh" "$TASKFORGE_ROOT/infrastructure/postgres/init"
fi
# TLS must use the project's original Compose volumes. r55 must never recreate
# the later /etc/taskforge-origin-tls override that broke standby HTTPS.
gateway="$(cluster_project)-gateway-1"
if docker inspect "$gateway" >/dev/null 2>&1; then
  docker inspect "$gateway" | python3 -S -c 'import json,sys
d=json.load(sys.stdin)[0]
mounts={m.get("Destination"):m for m in d.get("Mounts",[])}
for target in ("/etc/letsencrypt","/var/www/certbot"):
    m=mounts.get(target)
    assert m and m.get("Type")=="volume",f"gateway {target} must be a Docker named volume: {m!r}"
assert "/etc/taskforge-origin-tls" not in mounts,"legacy origin-tls mount is still active"
env=set(d.get("Config",{}).get("Env",[]) or [])
assert not any(x.startswith("GATEWAY_TLS_CERT_FILE=") or x.startswith("GATEWAY_TLS_KEY_FILE=") for x in env),"legacy gateway TLS path override is still active"'
fi
echo 'PASS: active Docker/systemd/runtime paths are stable, version-independent, and use native Certbot/Let"s Encrypt volumes'
