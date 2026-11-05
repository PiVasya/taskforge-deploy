#!/usr/bin/env bash
set -Eeuo pipefail
SRC=/run/taskforge-cluster/patroni.yml
DST=/tmp/taskforge-patroni.yml
[ -s "$SRC" ] || { echo "Patroni config missing: $SRC" >&2; exit 2; }
cp "$SRC" "$DST"
chown postgres:postgres "$DST"
chmod 600 "$DST"
run_as_postgres(){
  if command -v gosu >/dev/null 2>&1; then gosu postgres "$@"
  elif command -v runuser >/dev/null 2>&1; then runuser -u postgres -- "$@"
  else
    local cmd=''
    printf -v cmd '%q ' "$@"
    su -s /bin/bash postgres -c "$cmd"
  fi
}
PYBIN=/opt/patroni/bin/python
[ -x "$PYBIN" ] || PYBIN="$(command -v python3 || command -v python || true)"
PATRONI=/opt/patroni/bin/patroni
[ -x "$PATRONI" ] || PATRONI="$(command -v patroni || true)"
[ -n "$PYBIN" ] && [ -n "$PATRONI" ] || { echo 'Patroni/python not installed in image' >&2; exit 2; }
run_as_postgres "$PYBIN" -c 'import json; p=json.load(open("/tmp/taskforge-patroni.yml")); assert all(k in p for k in ("scope","name","restapi","etcd3","postgresql")); assert p["etcd3"].get("use_proxies") is True; assert any(str(x).startswith("etcd:") for x in p["etcd3"].get("hosts",[])); q=p["postgresql"]; assert q.get("remove_data_directory_on_rewind_failure") is False; assert q.get("remove_data_directory_on_diverged_timelines") is False; print("PATRONI_CONFIG_OK")'
if [ "${TASKFORGE_PATRONI_VALIDATE_ONLY:-0}" = 1 ]; then exit 0; fi
run_as_postgres "$PATRONI" "$DST"
