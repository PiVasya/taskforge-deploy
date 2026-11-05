#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh 2>/dev/null || source "$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/lib/common.sh"

[ "$(id -u)" -eq 0 ] || cluster_die 'onec-distribution requires root'
cluster_need_env
cluster_need_config

action="${1:-sync}"; [ $# -eq 0 ] || shift
case "$action" in seed|sync|status) ;; *) cluster_die 'usage: onec-distribution.sh seed|sync|status [options]' ;; esac

BUCKET="${ONEC_DISTRIBUTION_BUCKET:-$(cluster_read_env ONEC_DISTRIBUTION_BUCKET)}"; BUCKET="${BUCKET:-taskforge-runtime-cache}"
CHUNK_MB="${ONEC_DISTRIBUTION_CHUNK_SIZE_MB:-$(cluster_read_env ONEC_DISTRIBUTION_CHUNK_SIZE_MB)}"; CHUNK_MB="${CHUNK_MB:-32}"
MAX_SEEDS="${ONEC_DISTRIBUTION_MAX_SEEDS:-$(cluster_read_env ONEC_DISTRIBUTION_MAX_SEEDS)}"; MAX_SEEDS="${MAX_SEEDS:-4}"
PARALLEL="${ONEC_DISTRIBUTION_PARALLEL:-$(cluster_read_env ONEC_DISTRIBUTION_PARALLEL)}"; PARALLEL="${PARALLEL:-4}"
STATE_DIR="${ONEC_DISTRIBUTION_STATE_DIR:-$(cluster_read_env ONEC_DISTRIBUTION_STATE_DIR)}"; STATE_DIR="${STATE_DIR:-$TASKFORGE_STATE_ROOT/onec-distribution}"
DESIRED_OBJECT="onec/desired.json"
mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR" 2>/dev/null || true

case "$CHUNK_MB" in ''|*[!0-9]*) cluster_die 'ONEC_DISTRIBUTION_CHUNK_SIZE_MB must be numeric' ;; esac
case "$MAX_SEEDS" in ''|*[!0-9]*) cluster_die 'ONEC_DISTRIBUTION_MAX_SEEDS must be numeric' ;; esac
case "$PARALLEL" in ''|*[!0-9]*) cluster_die 'ONEC_DISTRIBUTION_PARALLEL must be numeric' ;; esac
[ "$CHUNK_MB" -ge 4 ] && [ "$CHUNK_MB" -le 256 ] || cluster_die 'ONEC_DISTRIBUTION_CHUNK_SIZE_MB must be between 4 and 256'
[ "$MAX_SEEDS" -ge 1 ] && [ "$MAX_SEEDS" -le 16 ] || cluster_die 'ONEC_DISTRIBUTION_MAX_SEEDS must be between 1 and 16'
[ "$PARALLEL" -ge 1 ] && [ "$PARALLEL" -le 16 ] || cluster_die 'ONEC_DISTRIBUTION_PARALLEL must be between 1 and 16'
case "$STATE_DIR" in /var/lib/taskforge/*|"$TASKFORGE_STATE_ROOT"/*) ;; *) cluster_die 'ONEC_DISTRIBUTION_STATE_DIR must stay under TaskForge state root' ;; esac

cluster_runtime_prepare
mcbin="$TASKFORGE_CLUSTER_RUNTIME/bin/mc"
[ -x "$mcbin" ] || bash "$CLUSTER_DIR/ops/host/install-mc.sh" >/dev/null
cluster_mc_config_prepare
trap 'cluster_mc_config_finish; rm -rf "${work:-}"' EXIT

user="$(cluster_read_env MINIO_ROOT_USER)"; user="${user:-taskforge}"
pass="$(cluster_read_env MINIO_ROOT_PASSWORD)"
[ -n "$pass" ] || cluster_die 'MINIO_ROOT_PASSWORD is required for cluster 1C distribution'
node="$(cluster_node_id)"
work="$(mktemp -d "$TASKFORGE_CLUSTER_RUNTIME/.onec-dist.XXXXXX")"

nodes_tsv(){
  python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1]))
for n in cfg.get('nodes',[]):
    nid=str(n.get('id','')).strip(); ip=str((n.get('wireguard') or {}).get('ip','')).strip(); port=int((n.get('minio') or {}).get('api_port',9000))
    if nid and ip: print(f"{nid}\t{ip}\t{port}")
PY
}

alias_for(){ printf 'tf-onec-%s' "$1"; }
configure_aliases(){
  while IFS=$'\t' read -r nid ip port; do
    alias="$(alias_for "$nid")"
    "$mcbin" alias set "$alias" "http://$ip:$port" "$user" "$pass" >/dev/null 2>&1 || true
  done < <(nodes_tsv)
}
configure_aliases
local_alias="$(alias_for "$node")"
"$mcbin" mb --ignore-existing "$local_alias/$BUCKET" >/dev/null 2>&1 || cluster_die 'local MinIO runtime-cache bucket is unavailable'

update_env(){
  python3 -S - "$TASKFORGE_ENV_FILE" "$@" <<'PY'
from pathlib import Path
import os,sys,tempfile
p=Path(sys.argv[1]); updates=dict(x.split('=',1) for x in sys.argv[2:])
lines=p.read_text(encoding='utf-8-sig').splitlines(); out=[]; seen=set()
for line in lines:
    s=line.strip()
    if s and not s.startswith('#') and '=' in line:
        k=line.split('=',1)[0].strip()
        if k in updates:
            out.append(f'{k}={updates[k]}'); seen.add(k); continue
    out.append(line)
for k,v in updates.items():
    if k not in seen: out.append(f'{k}={v}')
fd,tmp=tempfile.mkstemp(prefix=p.name+'.',dir=str(p.parent))
try: os.write(fd, ('\n'.join(out)+'\n').encode())
finally: os.close(fd)
os.chmod(tmp,0o600); os.replace(tmp,p)
PY
}

actual_primary(){
  python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys,urllib.request
cfg=json.load(open(sys.argv[1])); found=[]
for n in cfg.get('nodes',[]):
    ip=(n.get('wireguard') or {}).get('ip'); port=(n.get('postgres') or {}).get('patroni_rest_port',8008)
    if not ip: continue
    try:
        with urllib.request.urlopen(f'http://{ip}:{port}/primary',timeout=2) as r:
            if r.status==200: found.append(str(n['id']))
    except Exception: pass
if len(found)!=1: raise SystemExit('cannot determine unique Patroni primary: '+repr(found))
print(found[0])
PY
}

remote_copy(){
  local alias="$1" object="$2" dst="$3"
  "$mcbin" cp --quiet "$alias/$BUCKET/$object" "$dst"
}

first_desired(){
  local leader alias tmp canon
  leader="$(actual_primary 2>/dev/null || true)"
  [ -n "$leader" ] || return 1
  alias="$(alias_for "$leader")"
  tmp="$work/desired-primary.json"
  remote_copy "$alias" "$DESIRED_OBJECT" "$tmp" >/dev/null 2>&1 || return 1
  canon="$(python3 -S - "$tmp" <<'PY'
import json,re,sys
p=json.load(open(sys.argv[1]))
if p.get('schema')!='taskforge-onec-distribution-v1': raise SystemExit(2)
v=str(p.get('version','')); s=str(p.get('sha256','')).lower(); pref=str(p.get('prefix',''))
if not re.fullmatch(r'\d+\.\d+\.\d+\.\d+',v): raise SystemExit(2)
if not re.fullmatch(r'[0-9a-f]{64}',s): raise SystemExit(2)
if pref!=f'onec/{v}/{s}': raise SystemExit(2)
print(json.dumps({'schema':'taskforge-onec-distribution-v1','version':v,'sha256':s,'prefix':pref},sort_keys=True,separators=(',',':')))
PY
)" || return 1
  cp "$tmp" "$work/desired.json"
  printf '%s\n' "$leader" > "$work/desired-sources"
}
manifest_validate(){
  local manifest="$1" version="$2" sha="$3"
  python3 -S - "$manifest" "$version" "$sha" <<'PY'
import json,re,sys
p=json.load(open(sys.argv[1])); version=sys.argv[2]; sha=sys.argv[3]
assert p.get('schema')=='taskforge-onec-cache-manifest-v1'
assert p.get('version')==version and str(p.get('sha256','')).lower()==sha
size=int(p.get('size',0)); cs=int(p.get('chunk_size',0)); chunks=p.get('chunks')
assert size>0 and cs>0 and isinstance(chunks,list) and chunks
expect=0
for i,c in enumerate(chunks):
    assert int(c.get('index',-1))==i
    name=str(c.get('name','')); h=str(c.get('sha256','')).lower(); n=int(c.get('size',0))
    assert name==f'chunks/{i:08d}.part' and re.fullmatch(r'[0-9a-f]{64}',h) and n>0
    expect+=n
assert expect==size
print(len(chunks))
PY
}

seed_nodes(){
  local prefix="$1" version="$2" sha="$3" manifest_hash='' count=0
  : > "$work/seeds"
  : > "$work/seed-candidates"
  rm -f "$work/cache-manifest.json"
  while IFS=$'\t' read -r nid _ip _port; do
    alias="$(alias_for "$nid")"
    complete="$work/complete-$nid.json"; mani="$work/manifest-$nid.json"
    if remote_copy "$alias" "$prefix/complete.json" "$complete" >/dev/null 2>&1 \
       && remote_copy "$alias" "$prefix/manifest.json" "$mani" >/dev/null 2>&1; then
      python3 -S - "$complete" "$version" "$sha" <<'PY' >/dev/null 2>&1 || continue
import json,sys
p=json.load(open(sys.argv[1])); assert p.get('schema')=='taskforge-onec-cache-complete-v1'; assert p.get('version')==sys.argv[2]; assert str(p.get('sha256','')).lower()==sys.argv[3]
PY
      manifest_validate "$mani" "$version" "$sha" >/dev/null 2>&1 || continue
      mh="$(sha256sum "$mani" | awk '{print $1}')"
      if [ -n "$manifest_hash" ] && [ "$mh" != "$manifest_hash" ]; then
        cluster_die "1C cache manifest mismatch between seed nodes (node $nid)"
      fi
      manifest_hash="$mh"
      [ -s "$work/cache-manifest.json" ] || cp "$mani" "$work/cache-manifest.json"
      printf '%s\n' "$nid" >> "$work/seed-candidates"
      count=$((count+1))
    fi
  done < <(nodes_tsv)
  [ "$count" -gt 0 ] || return 1
  # Different requesting nodes choose different seed subsets. This prevents a
  # large cluster from always hammering the first few nodes when many replicas sync.
  python3 -S - "$work/seed-candidates" "$work/seeds" "$node" "$MAX_SEEDS" <<'PY'
import hashlib,sys
src,dst,node,limit=sys.argv[1],sys.argv[2],sys.argv[3],int(sys.argv[4])
ids=[x.strip() for x in open(src) if x.strip()]
ids.sort(key=lambda x: hashlib.sha256((node+'|'+x).encode()).digest())
open(dst,'w').write(''.join(x+'\n' for x in ids[:limit]))
PY
}
publish_cache(){
  local prefix="$1" manifest="$2" chunks_dir="$3" version="$4" sha="$5" size="$6"
  "$mcbin" mb --ignore-existing "$local_alias/$BUCKET" >/dev/null 2>&1
  "$mcbin" cp --quiet "$manifest" "$local_alias/$BUCKET/$prefix/manifest.json"
  for part in "$chunks_dir"/*.part; do
    [ -f "$part" ] || cluster_die 'no 1C installer chunks were produced'
    "$mcbin" cp --quiet "$part" "$local_alias/$BUCKET/$prefix/chunks/$(basename "$part")"
  done
  python3 -S - "$work/complete.json" "$node" "$version" "$sha" "$size" <<'PY'
import datetime,json,sys
path,node,version,sha,size=sys.argv[1:]
obj={'schema':'taskforge-onec-cache-complete-v1','node':node,'version':version,'sha256':sha,'size':int(size),'completed_at':datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()}
open(path,'w').write(json.dumps(obj,separators=(',',':'))+'\n')
PY
  "$mcbin" cp --quiet "$work/complete.json" "$local_alias/$BUCKET/$prefix/complete.json"
}

create_cache_from_file(){
  local file="$1" version="$2" sha="$3" prefix="$4"
  [ -s "$file" ] || cluster_die "1C installer missing: $file"
  actual="$(sha256sum "$file" | awk '{print tolower($1)}')"
  [ "$actual" = "$sha" ] || cluster_die "1C installer SHA-256 mismatch: expected=$sha actual=$actual"
  mkdir -p "$work/chunks"
  python3 -S - "$file" "$work/chunks" "$work/cache-manifest.json" "$version" "$sha" "$CHUNK_MB" <<'PY'
from pathlib import Path
import hashlib,json,sys
src=Path(sys.argv[1]); out=Path(sys.argv[2]); manifest=Path(sys.argv[3]); version=sys.argv[4]; sha=sys.argv[5]; chunk_size=int(sys.argv[6])*1024*1024
chunks=[]; total=0
with src.open('rb') as f:
    i=0
    while True:
        data=f.read(chunk_size)
        if not data: break
        name=f'{i:08d}.part'; p=out/name; p.write_bytes(data)
        chunks.append({'index':i,'name':f'chunks/{name}','size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        total+=len(data); i+=1
obj={'schema':'taskforge-onec-cache-manifest-v1','version':version,'sha256':sha,'size':total,'chunk_size':chunk_size,'chunks':chunks}
manifest.write_text(json.dumps(obj,separators=(',',':'))+'\n')
PY
  size="$(stat -c %s "$file")"
  publish_cache "$prefix" "$work/cache-manifest.json" "$work/chunks" "$version" "$sha" "$size"
}

publish_desired(){
  local version="$1" sha="$2" prefix="$3" local_ok=0
  python3 -S - "$work/desired-publish.json" "$version" "$sha" "$prefix" "$node" <<'PY'
import datetime,json,sys
path,version,sha,prefix,node=sys.argv[1:]
obj={'schema':'taskforge-onec-distribution-v1','version':version,'sha256':sha,'prefix':prefix,'published_by':node,'published_at':datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()}
open(path,'w').write(json.dumps(obj,separators=(',',':'))+'\n')
PY
  # Desired state is tiny, so mirror it immediately to every reachable node.
  # The heavy installer chunks still spread gradually/multi-source during sync.
  while IFS=$'\t' read -r nid _ip _port; do
    alias="$(alias_for "$nid")"
    if "$mcbin" mb --ignore-existing "$alias/$BUCKET" >/dev/null 2>&1 \
       && "$mcbin" cp --quiet "$work/desired-publish.json" "$alias/$BUCKET/$DESIRED_OBJECT" >/dev/null 2>&1; then
      [ "$nid" = "$node" ] && local_ok=1
    else
      cluster_warn "could not mirror 1C desired state to node=$nid; that node will adopt from peers when reachable"
    fi
  done < <(nodes_tsv)
  [ "$local_ok" -eq 1 ] || cluster_die 'failed to publish 1C desired state to local MinIO'
}
local_cache_ready(){
  local prefix="$1" version="$2" sha="$3"
  complete="$work/local-complete.json"; mani="$work/local-manifest.json"
  remote_copy "$local_alias" "$prefix/complete.json" "$complete" >/dev/null 2>&1 || return 1
  remote_copy "$local_alias" "$prefix/manifest.json" "$mani" >/dev/null 2>&1 || return 1
  python3 -S - "$complete" "$version" "$sha" <<'PY' >/dev/null 2>&1 || return 1
import json,sys
p=json.load(open(sys.argv[1])); assert p.get('schema')=='taskforge-onec-cache-complete-v1'; assert p.get('version')==sys.argv[2]; assert str(p.get('sha256','')).lower()==sys.argv[3]
PY
  manifest_validate "$mani" "$version" "$sha" >/dev/null 2>&1
}

download_chunks_multisource(){
  local prefix="$1" version="$2" sha="$3"
  seed_nodes "$prefix" "$version" "$sha" || cluster_die "no healthy MinIO seed contains 1C $version ($sha)"
  mapfile -t seeds < "$work/seeds"
  [ "${#seeds[@]}" -gt 0 ] || cluster_die 'no 1C seed nodes discovered'
  chunk_count="$(manifest_validate "$work/cache-manifest.json" "$version" "$sha")"
  mkdir -p "$work/downloaded"
  cluster_log "1C installer multi-source download node=$node seeds=${seeds[*]} chunks=$chunk_count parallel=$PARALLEL" >&2

  download_one(){
    local idx="$1" spec name expected expected_size start offset sid alias dst ok=0
    spec="$(python3 -S - "$work/cache-manifest.json" "$idx" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]))['chunks'][int(sys.argv[2])]
print(f"{c['name']}|{c['sha256']}|{c['size']}")
PY
)"
    IFS='|' read -r name expected expected_size <<<"$spec"
    dst="$work/downloaded/$(basename "$name")"
    start=$((idx % ${#seeds[@]}))
    for offset in $(seq 0 $((${#seeds[@]}-1))); do
      sid="${seeds[$(((start+offset)%${#seeds[@]}))]}"; alias="$(alias_for "$sid")"
      rm -f "$dst"
      if remote_copy "$alias" "$prefix/$name" "$dst" >/dev/null 2>&1 \
         && [ "$(stat -c %s "$dst" 2>/dev/null || echo 0)" = "$expected_size" ] \
         && [ "$(sha256sum "$dst" | awk '{print tolower($1)}')" = "$expected" ]; then
        printf '%s\t%s\n' "$idx" "$sid" >> "$work/chunk-sources"
        ok=1; break
      fi
    done
    [ "$ok" -eq 1 ] || { echo "failed chunk=$idx" >&2; return 2; }
  }
  export -f remote_copy alias_for download_one
  # Bash background workers share the immutable seed/manifest state and each writes a unique chunk.
  : > "$work/chunk-sources"
  running=0; failed=0
  for idx in $(seq 0 $((chunk_count-1))); do
    download_one "$idx" &
    running=$((running+1))
    if [ "$running" -ge "$PARALLEL" ]; then
      if ! wait -n; then failed=1; fi
      running=$((running-1))
    fi
  done
  while [ "$running" -gt 0 ]; do if ! wait -n; then failed=1; fi; running=$((running-1)); done
  [ "$failed" -eq 0 ] || cluster_die 'one or more 1C installer chunks failed from every seed'

  installer="$STATE_DIR/installer-$version-${sha:0:16}.run"
  tmp="$installer.tmp.$$"; : > "$tmp"
  for idx in $(seq 0 $((chunk_count-1))); do cat "$work/downloaded/$(printf '%08d.part' "$idx")" >> "$tmp"; done
  actual="$(sha256sum "$tmp" | awk '{print tolower($1)}')"
  [ "$actual" = "$sha" ] || cluster_die "reassembled 1C installer SHA-256 mismatch: expected=$sha actual=$actual"
  chmod 0600 "$tmp"; mv -f "$tmp" "$installer"
  size="$(stat -c %s "$installer")"
  publish_cache "$prefix" "$work/cache-manifest.json" "$work/downloaded" "$version" "$sha" "$size"
  cp "$work/desired.json" "$work/desired-local.json" 2>/dev/null || true
  [ -s "$work/desired-local.json" ] && "$mcbin" cp --quiet "$work/desired-local.json" "$local_alias/$BUCKET/$DESIRED_OBJECT" || true
  cluster_log "1C installer cached locally node=$node size=$size sha256=$sha" >&2
  printf '%s\n' "$installer"
}

materialize_local(){
  local prefix="$1" version="$2" sha="$3"
  mani="$work/materialize-manifest.json"
  remote_copy "$local_alias" "$prefix/manifest.json" "$mani" >/dev/null || cluster_die 'local 1C MinIO manifest missing'
  count="$(manifest_validate "$mani" "$version" "$sha")"
  mkdir -p "$work/materialized"
  for idx in $(seq 0 $((count-1))); do
    name="$(printf 'chunks/%08d.part' "$idx")"
    remote_copy "$local_alias" "$prefix/$name" "$work/materialized/$(basename "$name")" >/dev/null || cluster_die "local MinIO chunk missing: $name"
  done
  installer="$STATE_DIR/installer-$version-${sha:0:16}.run"; tmp="$installer.tmp.$$"; : > "$tmp"
  for idx in $(seq 0 $((count-1))); do cat "$work/materialized/$(printf '%08d.part' "$idx")" >> "$tmp"; done
  actual="$(sha256sum "$tmp" | awk '{print tolower($1)}')"; [ "$actual" = "$sha" ] || cluster_die 'local MinIO 1C cache failed full SHA-256 verification'
  chmod 0600 "$tmp"; mv -f "$tmp" "$installer"
  printf '%s\n' "$installer"
}

case "$action" in
  seed)
    leader="$(actual_primary)"; [ "$leader" = "$node" ] || cluster_die "1C external seed must run on current Patroni Primary (leader=$leader local=$node)"
    mode='' value='' version='' sha='' components='client_full,client_thin_fib,ru' curl_config=''
    while [ $# -gt 0 ]; do
      case "$1" in
        --file) [ $# -ge 2 ] || cluster_die '--file requires path'; [ -z "$mode" ] || cluster_die 'choose --file or --url'; mode=file; value="$2"; shift 2 ;;
        --url) [ $# -ge 2 ] || cluster_die '--url requires URL'; [ -z "$mode" ] || cluster_die 'choose --file or --url'; mode=url; value="$2"; shift 2 ;;
        --version) [ $# -ge 2 ] || cluster_die '--version requires value'; version="$2"; shift 2 ;;
        --sha256) [ $# -ge 2 ] || cluster_die '--sha256 requires value'; sha="${2,,}"; shift 2 ;;
        --components) [ $# -ge 2 ] || cluster_die '--components requires value'; components="$2"; shift 2 ;;
        --curl-config) [ $# -ge 2 ] || cluster_die '--curl-config requires path'; curl_config="$2"; shift 2 ;;
        *) cluster_die "unknown seed option: $1" ;;
      esac
    done
    [ -n "$mode" ] && [ -n "$version" ] || cluster_die 'seed requires --file/--url and --version'
    [[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || cluster_die '--version must use A.B.C.D numeric form'
    src="$work/installer.run"
    if [ "$mode" = file ]; then
      [ -s "$value" ] || cluster_die "installer not found: $value"; cp -f -- "$value" "$src"
      actual="$(sha256sum "$src" | awk '{print tolower($1)}')"; [ -n "$sha" ] || sha="$actual"; [ "$sha" = "$actual" ] || cluster_die 'provided --sha256 does not match local installer'
    else
      [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || cluster_die '--url requires exact 64-hex --sha256'
      cluster_log "downloading official/external 1C installer once on Primary node=$node"
      curl_args=(-fL --retry 4 --retry-delay 2 --connect-timeout 20 --max-time 7200)
      if [ -n "$curl_config" ]; then [ -r "$curl_config" ] || cluster_die "curl config not readable: $curl_config"; curl_args+=(--config "$curl_config"); fi
      curl "${curl_args[@]}" "$value" -o "$src" || cluster_die '1C external installer download failed'
      actual="$(sha256sum "$src" | awk '{print tolower($1)}')"; [ "$actual" = "$sha" ] || cluster_die "downloaded 1C installer SHA-256 mismatch expected=$sha actual=$actual"
    fi
    prefix="onec/$version/$sha"
    create_cache_from_file "$src" "$version" "$sha" "$prefix"
    publish_desired "$version" "$sha" "$prefix"
    update_env ONEC_ENABLED=true "ONEC_PLATFORM_VERSION=$version" ONEC_INSTALL_SOURCE=cluster ONEC_INSTALL_URL= "ONEC_INSTALL_SHA256=$sha" "ONEC_INSTALL_COMPONENTS=$components"
    echo "1C cluster seed ready: node=$node version=$version sha256=$sha bucket=$BUCKET prefix=$prefix"
    ;;
  sync)
    if ! first_desired; then
      enabled="$(cluster_read_env ONEC_ENABLED)"; enabled="${enabled:-false}"
      if [ "$enabled" = true ]; then
        version="$(cluster_read_env ONEC_PLATFORM_VERSION)"; sha="$(cluster_read_env ONEC_INSTALL_SHA256)"; url="$(cluster_read_env ONEC_INSTALL_URL)"; source_file="$(cluster_read_env ONEC_INSTALL_FILE)"
        [ -n "$version" ] && [ -n "$sha" ] || cluster_die 'local 1C is enabled but no cluster desired state and version/SHA are incomplete'
        prefix="onec/$version/$sha"
        if [ "$(actual_primary 2>/dev/null || true)" = "$node" ]; then
          if [ -s "$source_file" ]; then create_cache_from_file "$source_file" "$version" "$sha" "$prefix"; publish_desired "$version" "$sha" "$prefix"; first_desired
          elif [ -n "$url" ]; then
            curl -fL --retry 4 --retry-delay 2 --connect-timeout 20 --max-time 7200 "$url" -o "$work/installer.run" || cluster_die 'Primary could not download configured 1C installer URL'
            create_cache_from_file "$work/installer.run" "$version" "$sha" "$prefix"; publish_desired "$version" "$sha" "$prefix"; first_desired
          else cluster_die '1C enabled but cluster has no desired cache; seed it once on Primary with cluster.sh onec seed'; fi
        else
          cluster_die '1C enabled locally but cluster cache is not seeded yet; seed it on current Primary first'
        fi
      else
        echo '1C cluster desired state: absent (leaving ONEC_ENABLED=false)'
        exit 0
      fi
    fi
    read -r version sha prefix <<<"$(python3 -S - "$work/desired.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); print(p['version'],p['sha256'],p['prefix'])
PY
)"
    installer="$STATE_DIR/installer-$version-${sha:0:16}.run"
    if [ -s "$installer" ] && [ "$(sha256sum "$installer" | awk '{print tolower($1)}')" = "$sha" ]; then
      if ! local_cache_ready "$prefix" "$version" "$sha"; then
        cluster_log "re-publishing verified host installer into local MinIO cache node=$node"
        create_cache_from_file "$installer" "$version" "$sha" "$prefix"
      fi
      cluster_log "1C installer host cache ready node=$node version=$version"
    elif local_cache_ready "$prefix" "$version" "$sha"; then
      installer="$(materialize_local "$prefix" "$version" "$sha")"
      cluster_log "1C installer resolved from local MinIO node=$node version=$version"
    else
      installer="$(download_chunks_multisource "$prefix" "$version" "$sha")"
    fi
    "$mcbin" cp --quiet "$work/desired.json" "$local_alias/$BUCKET/$DESIRED_OBJECT" >/dev/null 2>&1 || true
    update_env ONEC_ENABLED=true "ONEC_PLATFORM_VERSION=$version" ONEC_INSTALL_SOURCE=file ONEC_INSTALL_URL= "ONEC_INSTALL_FILE=$installer" "ONEC_INSTALL_SHA256=$sha"
    echo "1C cluster distribution synced: node=$node version=$version installer=$installer"
    ;;
  status)
    if first_desired; then
      python3 -S - "$work/desired.json" <<'PY'
import json,sys
p=json.load(open(sys.argv[1])); print('cluster_desired=yes'); print('cluster_version='+p['version']); print('cluster_sha256='+p['sha256'])
PY
      read -r version sha prefix <<<"$(python3 -S - "$work/desired.json" <<'PY'
import json,sys;p=json.load(open(sys.argv[1]));print(p['version'],p['sha256'],p['prefix'])
PY
)"
      if seed_nodes "$prefix" "$version" "$sha"; then echo "cluster_seeds=$(paste -sd, "$work/seeds")"; else echo 'cluster_seeds=-'; fi
      if local_cache_ready "$prefix" "$version" "$sha"; then echo 'local_minio_cache=ready'; else echo 'local_minio_cache=missing'; fi
    else
      echo 'cluster_desired=no'; echo 'cluster_seeds=-'; echo 'local_minio_cache=missing'
    fi
    ;;
esac
