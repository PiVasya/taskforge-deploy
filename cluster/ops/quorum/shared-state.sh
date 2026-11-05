#!/usr/bin/env bash
set -Eeuo pipefail
source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"
cluster_need_env
cluster_need_config
action="${1:-}"
case "$action" in push|pull) ;; *) cluster_die "usage: ./cluster/ops/quorum/shared-state.sh push|pull";; esac
cluster_runtime_prepare
cluster_render >/dev/null
mcbin="$TASKFORGE_CLUSTER_RUNTIME/bin/mc"
[ -x "$mcbin" ] || bash "$CLUSTER_DIR/ops/host/install-mc.sh" >/dev/null
cluster_mc_config_prepare
user="$(cluster_read_env MINIO_ROOT_USER)"; user="${user:-taskforge}"
pass="$(cluster_read_env MINIO_ROOT_PASSWORD)"
node_json="$(clusterctl node-info)"
ip="$(printf '%s' "$node_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["wireguard"]["ip"])')"
port="$(printf '%s' "$node_json" | python3 -c 'import json,sys; print(json.load(sys.stdin)["minio"]["api_port"])')"
node="$(cluster_node_id)"
"$mcbin" alias set tf-local "http://$ip:$port" "$user" "$pass" >/dev/null
"$mcbin" mb --ignore-existing tf-local/taskforge-cluster-state >/dev/null

work="$(mktemp -d "$TASKFORGE_CLUSTER_RUNTIME/.shared-state-r55.XXXXXX")"
trap 'rm -rf "$work"' EXIT
shared="$TASKFORGE_STATE_ROOT/data-protection"
mkdir -p "$shared/content-data-protection" "$shared/quiz-data-protection"
legacy_object="tf-local/taskforge-cluster-state/shared/data-protection.tar.gz"
legacy_hash_object="tf-local/taskforge-cluster-state/shared/data-protection.sha256"
current_object="tf-local/taskforge-cluster-state/shared/current.json"
local_hash_file="$TASKFORGE_CLUSTER_RUNTIME/shared-state-last-pushed.sha256"

state_hash(){
  python3 - "$1" <<'PY_HASH'
from pathlib import Path
import hashlib,sys
root=Path(sys.argv[1])
h=hashlib.sha256()
for dirname in ('content-data-protection','quiz-data-protection'):
    base=root/dirname
    if not base.exists():
        continue
    for path in sorted(p for p in base.rglob('*') if p.is_file()):
        rel=path.relative_to(root).as_posix().encode('utf-8')
        h.update(len(rel).to_bytes(4,'big')); h.update(rel)
        data=path.read_bytes()
        h.update(len(data).to_bytes(8,'big')); h.update(data)
print(h.hexdigest())
PY_HASH
}

validate_archive(){
  python3 - "$1" <<'PY_SAFE_TAR'
import sys,tarfile
from pathlib import PurePosixPath
allowed={'content-data-protection','quiz-data-protection'}
seen=set()
with tarfile.open(sys.argv[1], 'r:gz') as tf:
    for member in tf.getmembers():
        name=member.name.replace('\\','/')
        path=PurePosixPath(name)
        if path.is_absolute() or '..' in path.parts:
            raise SystemExit(f'unsafe shared-state archive path: {member.name}')
        if member.issym() or member.islnk():
            raise SystemExit(f'links are not allowed in shared-state archive: {member.name}')
        if path.parts:
            if path.parts[0] not in allowed:
                raise SystemExit(f'unexpected shared-state top-level path: {member.name}')
            seen.add(path.parts[0])
missing=allowed-seen
if missing:
    raise SystemExit('shared-state archive missing: '+','.join(sorted(missing)))
PY_SAFE_TAR
}

install_extracted(){
  local extracted="$1"
  python3 - "$shared" "$extracted" <<'PY_INSTALL'
from pathlib import Path
import os,shutil,sys,uuid
shared=Path(sys.argv[1]); src=Path(sys.argv[2])
names=('content-data-protection','quiz-data-protection')
backups=[]; installed=[]
try:
    for name in names:
        incoming=src/name
        if not incoming.is_dir():
            raise RuntimeError(f'missing extracted directory {name}')
        target=shared/name
        backup=shared/f'.r55-backup-{name}-{uuid.uuid4().hex}'
        if target.exists():
            os.replace(target,backup); backups.append((target,backup))
        os.replace(incoming,target); installed.append(target)
    for _,backup in backups:
        shutil.rmtree(backup,ignore_errors=True)
except Exception:
    for target in reversed(installed):
        shutil.rmtree(target,ignore_errors=True)
    for target,backup in reversed(backups):
        if backup.exists(): os.replace(backup,target)
    raise
PY_INSTALL
}

restore_tar(){
  local archive="$1" expected_state="$2" expected_archive="${3:-}"
  [ -z "$expected_archive" ] || {
    actual_archive="$(sha256sum "$archive" | awk '{print $1}')"
    [ "$actual_archive" = "$expected_archive" ] || cluster_die "shared-state archive checksum mismatch"
  }
  validate_archive "$archive"
  rm -rf "$work/extracted"; mkdir -p "$work/extracted"
  tar --no-same-owner --no-same-permissions -C "$work/extracted" -xzf "$archive"
  restored_hash="$(state_hash "$work/extracted")"
  [ -z "$expected_state" ] || [ "$restored_hash" = "$expected_state" ] || cluster_die "shared-state content checksum mismatch after staged restore"
  install_extracted "$work/extracted"
  printf '%s\n' "$restored_hash" | cluster_atomic_write "$local_hash_file" 600
  cluster_mc_config_finish
  echo "shared DataProtection state restored sha256=$restored_hash"
}

legacy_pull(){
  "$mcbin" stat "$legacy_object" >/dev/null 2>&1 || cluster_die "shared DataProtection state is missing; refusing HA application activation"
  "$mcbin" cp --quiet "$legacy_object" "$work/legacy.tar.gz"
  expected=''
  if "$mcbin" stat "$legacy_hash_object" >/dev/null 2>&1; then
    "$mcbin" cp --quiet "$legacy_hash_object" "$work/legacy.sha256"
    expected="$(tr -d '\r\n' < "$work/legacy.sha256")"
  fi
  restore_tar "$work/legacy.tar.gz" "$expected" ""
}

if [ "$action" = push ]; then
  current_hash="$(state_hash "$shared")"
  previous_hash="$(cat "$local_hash_file" 2>/dev/null || true)"
  tar -C "$shared" -czf "$work/data-protection.tar.gz" content-data-protection quiz-data-protection
  archive_hash="$(sha256sum "$work/data-protection.tar.gz" | awk '{print $1}')"
  generation="$(date -u +%Y%m%dT%H%M%SZ)-${node}-${current_hash:0:12}"
  generation_prefix="tf-local/taskforge-cluster-state/shared/generations/$generation"

  if [ "$current_hash" = "$previous_hash" ] \
      && "$mcbin" stat "$current_object" >/dev/null 2>&1; then
    cluster_mc_config_finish
    echo "shared DataProtection state is unchanged"
    exit 0
  fi

  python3 - "$work/manifest.json" "$generation" "$node" "$current_hash" "$archive_hash" <<'PY_MANIFEST'
import datetime,json,sys
path,generation,node,state_hash,archive_hash=sys.argv[1:]
doc={
  'schema_version':1,
  'generation':generation,
  'node':node,
  'created_at':datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat(),
  'state_sha256':state_hash,
  'archive_sha256':archive_hash,
}
open(path,'w',encoding='utf-8').write(json.dumps(doc,separators=(',',':'))+'\n')
PY_MANIFEST
  "$mcbin" cp --quiet "$work/data-protection.tar.gz" "$generation_prefix/data-protection.tar.gz"
  "$mcbin" cp --quiet "$work/manifest.json" "$generation_prefix/manifest.json"
  "$mcbin" stat "$generation_prefix/data-protection.tar.gz" >/dev/null
  "$mcbin" stat "$generation_prefix/manifest.json" >/dev/null

  # Publish the pointer LAST. A remote node either sees the old complete
  # generation or this new immutable generation; it never trusts a half-written pair.
  cp "$work/manifest.json" "$work/current.json"
  "$mcbin" cp --quiet "$work/current.json" "$current_object"

  # Rolling compatibility: legacy nodes still consume the historical objects.
  printf '%s\n' "$current_hash" > "$work/data-protection.sha256"
  "$mcbin" cp --quiet "$work/data-protection.tar.gz" "$legacy_object"
  "$mcbin" cp --quiet "$work/data-protection.sha256" "$legacy_hash_object"
  printf '%s\n' "node=$node timestamp=$(date -u +%FT%TZ) sha256=$current_hash generation=$generation" > "$work/meta.txt"
  "$mcbin" cp --quiet "$work/meta.txt" tf-local/taskforge-cluster-state/shared/meta.txt
  printf '%s\n' "$current_hash" | cluster_atomic_write "$local_hash_file" 600
  cluster_mc_config_finish
  echo "shared DataProtection generation uploaded generation=$generation sha256=$current_hash"
else
  if "$mcbin" stat "$current_object" >/dev/null 2>&1; then
    "$mcbin" cp --quiet "$current_object" "$work/current.json"
    row="$(python3 - "$work/current.json" <<'PY_CURRENT'
import json,re,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
g=str(p.get('generation',''))
if not re.fullmatch(r'[A-Za-z0-9._-]{1,160}',g): raise SystemExit('invalid generation id')
print('|'.join([g,str(p.get('state_sha256','')),str(p.get('archive_sha256',''))]))
PY_CURRENT
)" || cluster_die "invalid shared-state current.json"
    IFS='|' read -r generation expected_state expected_archive <<<"$row"
    prefix="tf-local/taskforge-cluster-state/shared/generations/$generation"
    fetched=0
    for _ in 1 2 3 4 5 6; do
      if "$mcbin" cp --quiet "$prefix/manifest.json" "$work/manifest.json" >/dev/null 2>&1 \
          && "$mcbin" cp --quiet "$prefix/data-protection.tar.gz" "$work/data-protection.tar.gz" >/dev/null 2>&1; then
        fetched=1; break
      fi
      sleep 2
    done
    if [ "$fetched" -eq 1 ]; then
      manifest_row="$(python3 - "$work/manifest.json" <<'PY_VERIFY_MANIFEST'
import json,sys
p=json.load(open(sys.argv[1],encoding='utf-8'))
print('|'.join([str(p.get('generation','')),str(p.get('state_sha256','')),str(p.get('archive_sha256',''))]))
PY_VERIFY_MANIFEST
)"
      IFS='|' read -r mg ms ma <<<"$manifest_row"
      if [ "$mg" = "$generation" ] && [ "$ms" = "$expected_state" ] && [ "$ma" = "$expected_archive" ]; then
        restore_tar "$work/data-protection.tar.gz" "$expected_state" "$expected_archive"
        exit 0
      fi
      echo "warning: immutable generation manifest mismatch; trying verified legacy copy" >&2
    else
      echo "warning: current generation not replicated locally yet; trying verified legacy copy" >&2
    fi
  fi
  legacy_pull
fi
