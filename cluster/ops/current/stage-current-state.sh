#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
[ "$(id -u)" -eq 0 ] || { echo 'error: stage-current-state requires root' >&2; exit 2; }
CFG="${TASKFORGE_CONFIG_ROOT:-/etc/taskforge}"
STATE="${TASKFORGE_STATE_ROOT:-/var/lib/taskforge}"
RUNTIME="${TASKFORGE_CLUSTER_RUNTIME:-$STATE/cluster}"
mkdir -p "$CFG" "$CFG/code-analyzer" "$CFG/docker" "$STATE/image-analyzer-model-cache" "$STATE/data-protection/content-data-protection" "$STATE/data-protection/quiz-data-protection" "$RUNTIME/readiness" "$RUNTIME/easy"

valid_json(){ python3 -S - "$1" <<'PY' >/dev/null 2>&1
import json,sys
json.load(open(sys.argv[1],encoding='utf-8'))
PY
}

active_source_root(){
  local unit roots docker_roots all
  unit="$(systemctl show taskforge-cluster.service -p ExecStart -p WorkingDirectory 2>/dev/null || true)"
  roots="$(UNIT_TEXT="$unit" python3 -S - <<'PY'
import os,re
s=os.environ.get('UNIT_TEXT','')
seen=[]
for x in re.findall(r'(/[^\s;{}]*(?:taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+))',s):
    x=x.rstrip('/')
    if x not in seen: seen.append(x)
print('\n'.join(seen))
PY
)"
  docker_roots="$(docker ps -aq --filter 'label=com.docker.compose.project' 2>/dev/null | xargs -r docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' 2>/dev/null | grep -E '(taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+)$' | sort -u || true)"
  all="$(printf '%s\n%s\n' "$roots" "$docker_roots" | sed '/^$/d' | awk '!seen[$0]++')"
  while IFS= read -r p; do
    [ -n "$p" ] || continue
    [ -s "$p/.env" ] || continue
    printf '%s\n' "$p"
    return 0
  done <<<"$all"
  return 1
}

source_root="$(active_source_root || true)"
source_label=stable
if [ ! -s "$CFG/taskforge.env" ]; then
  [ -n "$source_root" ] && [ -s "$source_root/.env" ] || { echo 'error: cannot find live TaskForge environment. Need /etc/taskforge/taskforge.env or an active old release with .env' >&2; exit 2; }
  cp -f "$source_root/.env" "$CFG/taskforge.env"
  source_label=historical-import
fi

op_user=''
[ ! -f "$CFG/operator-user" ] || op_user="$(tr -d '\r\n' < "$CFG/operator-user")"
if [ -z "$op_user" ]; then
  op_user="${SUDO_USER:-}"
  [ -n "$op_user" ] && [ "$op_user" != root ] || { [ -n "$source_root" ] && op_user="$(stat -c '%U' "$source_root" 2>/dev/null || true)"; }
  [ -n "$op_user" ] && [ "$op_user" != root ] || op_user=root
fi
op_group=''
[ ! -f "$CFG/operator-group" ] || op_group="$(tr -d '\r\n' < "$CFG/operator-group")"
[ -n "$op_group" ] || op_group="$(id -gn "$op_user" 2>/dev/null || echo root)"
printf '%s\n' "$op_user" > "$CFG/operator-user"
printf '%s\n' "$op_group" > "$CFG/operator-group"
chmod 644 "$CFG/operator-user" "$CFG/operator-group"

pick_json(){
  local dst="$1" rel="$2" def="$3"
  if [ -s "$dst" ] && valid_json "$dst"; then return 0; fi
  if [ -n "$source_root" ] && [ -s "$source_root/$rel" ] && valid_json "$source_root/$rel"; then cp -f "$source_root/$rel" "$dst"; return 0; fi
  cp -f "$ROOT/$def" "$dst"
}
pick_json "$CFG/cluster.json" cluster/cluster.json cluster/cluster.default.json
pick_json "$CFG/inventory.json" cluster/inventory.json cluster/inventory.default.json

node=''
[ ! -f "$RUNTIME/node-id" ] || node="$(tr -d '\r\n' < "$RUNTIME/node-id")"
if ! [[ "$node" =~ ^[ABC]$ ]]; then
  if [ -n "$source_root" ] && [ -f "$source_root/.runtime/cluster/node-id" ]; then node="$(tr -d '\r\n' < "$source_root/.runtime/cluster/node-id")"; fi
fi
if ! [[ "$node" =~ ^[ABC]$ ]]; then
  node="$(python3 -S - "$CFG/cluster.json" <<'PY'
import json,subprocess,sys
c=json.load(open(sys.argv[1])); out=subprocess.check_output(['ip','-4','-o','addr','show'],text=True)
ips={tok.split('/')[0] for line in out.splitlines() for tok in line.split() if '/' in tok and tok[:1].isdigit()}
h=[str(n['id']) for n in c['nodes'] if str((n.get('wireguard') or {}).get('ip','')) in ips]
if len(h)!=1: raise SystemExit('cannot infer local node from WireGuard addresses: '+repr(h))
print(h[0])
PY
)"
fi
printf '%s\n' "$node" > "$RUNTIME/node-id"
chmod 600 "$RUNTIME/node-id"

# Preserve optional local tuning from stable state first, then the active old release.
for name in local.env cpu.env; do
  if [ ! -s "$CFG/$name" ] && [ -n "$source_root" ] && [ -s "$source_root/.runtime/cluster/$name" ]; then cp -f "$source_root/.runtime/cluster/$name" "$CFG/$name"; fi
done

# Strip obsolete release-path overrides. Current control owns all managed paths.
python3 -S - "$CFG/taskforge.env" "$CFG/local.env" "$CFG/cpu.env" <<'PY'
from pathlib import Path
import re,sys
managed={'TASKFORGE_ROOT','TASKFORGE_CONTROL_ROOT','TASKFORGE_STATE_ROOT','TASKFORGE_CONFIG_ROOT','TASKFORGE_CLUSTER_DIR','TASKFORGE_INVENTORY','TASKFORGE_CLUSTER_CONFIG','TASKFORGE_POSTGRES_INIT_DIR','TASKFORGE_IMAGE_ANALYZER_MODEL_CACHE_DIR','TASKFORGE_TLS_DIR','GATEWAY_TLS_CERT_FILE','GATEWAY_TLS_KEY_FILE','CODE_ANALYZER_PRIVATE_KEY_PATH','CODE_ANALYZER_PUBLIC_KEY_PATH','DOCKER_CONFIG','TASKFORGE_WATCHTOWER_DOCKER_CONFIG','IMAGE_ANALYZER_MEM_LIMIT','IMAGE_ANALYZER_MEMSWAP_LIMIT'}
old=re.compile(r'(?:taskforge-prod-linux-v40-cluster-control-final-r[0-9]+|taskforge-server-v[0-9]+\.[0-9]+\.[0-9]+)')
for raw in sys.argv[1:]:
    p=Path(raw)
    if not p.is_file(): continue
    out=[]
    for line in p.read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.lstrip().startswith('#'):
            k=line.split('=',1)[0].strip()
            if k in managed or old.search(line): continue
        out.append(line)
    p.write_text('\n'.join(out)+'\n',encoding='utf-8')
PY

project="$(python3 -S - "$CFG/taskforge.env" <<'PY'
from pathlib import Path
import sys
v='taskforge-prod'
for raw in Path(sys.argv[1]).read_text(encoding='utf-8-sig').splitlines():
    if raw.lstrip().startswith('#') or '=' not in raw: continue
    k,x=raw.split('=',1)
    if k.strip()=='COMPOSE_PROJECT_NAME': v=x.strip().strip('"\'') or v; break
print(v)
PY
)"

mount_source(){
  local svc="$1" target="$2" cname
  cname="$project-$svc-1"
  docker inspect "$cname" 2>/dev/null | python3 -S -c 'import json,sys;a=json.load(sys.stdin);t=sys.argv[1];d=a[0] if a else {};m=[x for x in d.get("Mounts",[]) if x.get("Destination")==t];print(m[0].get("Source","") if len(m)==1 else "")' "$target" 2>/dev/null || true
}
copy_dir(){ local src="$1" dst="$2"; [ -d "$src" ] || return 1; mkdir -p "$dst"; [ "$(readlink -f "$src")" = "$(readlink -f "$dst")" ] || cp -a -u "$src/." "$dst/"; }
copy_file(){ local src="$1" dst="$2" mode="$3"; [ -f "$src" ] && [ -s "$src" ] || return 1; mkdir -p "$(dirname "$dst")"; [ "$(readlink -f "$src")" = "$(readlink -f "$dst" 2>/dev/null || true)" ] || cp -L -f "$src" "$dst"; chmod "$mode" "$dst"; }

priv_src="$(mount_source code-analyzer /run/secrets/code-analyzer-private.pem)"
pub_src=''
for s in cpp-runner csharp-runner java-runner javascript-runner pascal-runner python-runner onec-runner; do pub_src="$(mount_source "$s" /run/secrets/code-analyzer-public.pem)"; [ -n "$pub_src" ] && break; done
[ -n "$priv_src" ] || [ ! -s "$CFG/code-analyzer/code-analyzer-private.pem" ] || priv_src="$CFG/code-analyzer/code-analyzer-private.pem"
[ -n "$pub_src" ] || [ ! -s "$CFG/code-analyzer/code-analyzer-public.pem" ] || pub_src="$CFG/code-analyzer/code-analyzer-public.pem"
if [ -n "$source_root" ]; then
  [ -n "$priv_src" ] || priv_src="$source_root/.runtime/code-analyzer-keys/code-analyzer-private.pem"
  [ -n "$pub_src" ] || pub_src="$source_root/.runtime/code-analyzer-keys/code-analyzer-public.pem"
fi
copy_file "$priv_src" "$CFG/code-analyzer/code-analyzer-private.pem" 600 || { echo 'error: code-analyzer private key not found in live state or old release' >&2; exit 2; }
copy_file "$pub_src" "$CFG/code-analyzer/code-analyzer-public.pem" 644 || { echo 'error: code-analyzer public key not found in live state or old release' >&2; exit 2; }

dir_has_files(){ local d="$1"; [ -d "$d" ] && find "$d" -mindepth 1 -print -quit 2>/dev/null | grep -q .; }
content_dst="$STATE/data-protection/content-data-protection"
quiz_dst="$STATE/data-protection/quiz-data-protection"
content_src="$(mount_source content-api /root/.aspnet/DataProtection-Keys)"
quiz_src="$(mount_source quiz-api /root/.aspnet/DataProtection-Keys)"
if [ -z "$content_src" ] && dir_has_files "$content_dst"; then content_src="$content_dst"; fi
if [ -z "$quiz_src" ] && dir_has_files "$quiz_dst"; then quiz_src="$quiz_dst"; fi
if [ -z "$content_src" ] && [ -n "$source_root" ] && dir_has_files "$source_root/.runtime/cluster/shared/content-data-protection"; then content_src="$source_root/.runtime/cluster/shared/content-data-protection"; fi
if [ -z "$quiz_src" ] && [ -n "$source_root" ] && dir_has_files "$source_root/.runtime/cluster/shared/quiz-data-protection"; then quiz_src="$source_root/.runtime/cluster/shared/quiz-data-protection"; fi
[ -z "$content_src" ] || copy_dir "$content_src" "$content_dst" || true
[ -z "$quiz_src" ] || copy_dir "$quiz_src" "$quiz_dst" || true

cache_dst="$STATE/image-analyzer-model-cache"
cache_src="$(mount_source image-analyzer /models/openclip)"
if [ -z "$cache_src" ] && dir_has_files "$cache_dst"; then cache_src="$cache_dst"; fi
if [ -z "$cache_src" ] && [ -n "$source_root" ] && dir_has_files "$source_root/.runtime/image-analyzer-model-cache"; then cache_src="$source_root/.runtime/image-analyzer-model-cache"; fi
[ -z "$cache_src" ] || copy_dir "$cache_src" "$cache_dst" || true

docker_cfg="$CFG/docker/config.json"
if [ -e "$docker_cfg" ] && { [ ! -f "$docker_cfg" ] || ! valid_json "$docker_cfg"; }; then
  bad="$CFG/docker/config.invalid.$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$docker_cfg" "$bad" 2>/dev/null || rm -rf "$docker_cfg"
  echo "warning: quarantined invalid managed Docker config at $bad" >&2
fi
if [ ! -s "$docker_cfg" ]; then
  docker_src="$(mount_source watchtower /config.json)"
  if [ -z "$docker_src" ] && [ -n "$source_root" ]; then
    [ ! -f "$source_root/.docker/config.json" ] || [ ! -s "$source_root/.docker/config.json" ] || docker_src="$source_root/.docker/config.json"
    [ -n "$docker_src" ] || { [ ! -f "$source_root/config.json" ] || [ ! -s "$source_root/config.json" ] || docker_src="$source_root/config.json"; }
  fi
  if [ -n "$docker_src" ] && valid_json "$docker_src"; then copy_file "$docker_src" "$docker_cfg" 600 || true; fi
fi
if [ ! -x "$RUNTIME/bin/mc" ] && [ -n "$source_root" ] && [ -x "$source_root/.runtime/cluster/bin/mc" ]; then mkdir -p "$RUNTIME/bin"; cp -f "$source_root/.runtime/cluster/bin/mc" "$RUNTIME/bin/mc"; chmod 755 "$RUNTIME/bin/mc"; fi

# Capture every persistent volume that must survive this migration, then pin current Compose logical names to those exact volumes.
python3 -S - "$project" "$RUNTIME/storage-baseline-r55.json" "$RUNTIME/storage-bindings.yaml" "$CFG/cluster.json" <<'PY'
import datetime,json,subprocess,sys,os,urllib.request
project,baseline_path,override_path,cluster_path=sys.argv[1:]
items=[
 ('postgres','/var/lib/postgresql',['postgres-data','postgres-cluster-data'],True),
 ('rabbitmq','/var/lib/rabbitmq',['rabbitmq-data'],True),
 ('redis','/data',['redis-data'],True),
 ('minio','/data',['minio-data'],True),
 ('etcd','/etcd-data',['etcd-data'],True),
 ('gateway','/etc/letsencrypt',['letsencrypt'],False),
 ('gateway','/var/www/certbot',['certbot-webroot'],False),
 ('gateway','/var/log/taskforge-ai',['gateway-ai-telemetry'],False),
]
def inspect(name):
    p=subprocess.run(['docker','inspect',name],text=True,capture_output=True)
    if p.returncode: return None
    a=json.loads(p.stdout); return a[0] if a else None
def vol_fp(name):
    p=subprocess.run(['docker','volume','inspect',name],text=True,capture_output=True)
    if p.returncode: raise SystemExit('cannot inspect Docker volume '+name)
    d=json.loads(p.stdout)[0]
    return {k:d.get(k,'') for k in ('Name','Driver','Mountpoint','CreatedAt','Scope')}
def volume_exists(name):
    return subprocess.run(['docker','volume','inspect',name],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
def discover_compose_volume(logical_names):
    candidates=[]
    for logical in logical_names:
        p=subprocess.run(['docker','volume','ls','-q','--filter',f'label=com.docker.compose.project={project}','--filter',f'label=com.docker.compose.volume={logical}'],text=True,capture_output=True)
        if p.returncode==0:
            candidates += [x.strip() for x in p.stdout.splitlines() if x.strip()]
        for guess in (f'{project}_{logical}',f'{project}-{logical}'):
            if volume_exists(guess): candidates.append(guess)
    unique=[]
    for x in candidates:
        if x not in unique: unique.append(x)
    if len(unique)>1: raise SystemExit('ambiguous preserved Docker volumes for '+','.join(logical_names)+': '+repr(unique))
    return unique[0] if unique else ''
def remote_system_identifier():
    try: cfg=json.load(open(cluster_path))
    except Exception: return ''
    found=[]
    for n in cfg.get('nodes',[]):
        ip=str((n.get('wireguard') or {}).get('ip','')).strip(); port=int((n.get('postgres') or {}).get('patroni_rest_port',8008) or 8008)
        if not ip: continue
        try:
            with urllib.request.urlopen(f'http://{ip}:{port}/patroni',timeout=2) as r:
                d=json.load(r); sid=str(d.get('database_system_identifier','')).strip()
                if sid and sid not in found: found.append(sid)
        except Exception: pass
    if len(found)>1: raise SystemExit('reachable Patroni nodes disagree on PostgreSQL system identifier: '+repr(found))
    return found[0] if found else ''
old=json.load(open(baseline_path)) if os.path.isfile(baseline_path) else None
if old and old.get('project')!=project: raise SystemExit(f'persisted migration baseline project mismatch: {old.get("project")} != {project}')
current_vols={}; current_logical={}; missing=[]
for svc,target,names,required in items:
    d=inspect(f'{project}-{svc}-1')
    m=[] if d is None else [x for x in d.get('Mounts',[]) if x.get('Destination')==target and x.get('Type')=='volume']
    if len(m)==1 and m[0].get('Name'):
        name=m[0]['Name']
    elif len(m)>1:
        raise SystemExit(f'ambiguous live persistent mounts for {svc}:{target}: {m!r}')
    else:
        name=discover_compose_volume(names)
    if name:
        current_vols[name]=vol_fp(name)
        for logical_name in names: current_logical[logical_name]=name
    elif required and old is None:
        missing.append(f'{svc}:{target}')
if missing: raise SystemExit('required live persistent volume mount(s) missing before first r55 attempt: '+', '.join(missing))
pg=f'{project}-postgres-1'; sid=''
d=inspect(pg)
if d is not None:
    p=subprocess.run(['docker','exec',pg,'sh','-lc','psql -X -U "$POSTGRES_USER" -d postgres -Atc "select system_identifier from pg_control_system();"'],text=True,capture_output=True)
    if p.returncode==0: sid=p.stdout.strip()
if not sid: sid=remote_system_identifier()
if old:
    expected_sid=old.get('postgres_system_identifier','')
    if sid and sid!=expected_sid: raise SystemExit(f'PostgreSQL system_identifier changed since first r55 attempt: {expected_sid} -> {sid}')
    for name,before in old.get('volumes',{}).items():
        if vol_fp(name)!=before: raise SystemExit('persistent volume fingerprint changed since first r55 attempt: '+name)
    for logical_name,expected_name in old.get('logical_volumes',{}).items():
        actual=current_logical.get(logical_name)
        if actual and actual!=expected_name: raise SystemExit(f'live volume mapping changed since first r55 attempt: {logical_name} {expected_name} -> {actual}')
    baseline=old; logical=old.get('logical_volumes',{}); vols=old.get('volumes',{})
    print('persistent_volume_baseline=reused')
else:
    if not sid: raise SystemExit('cannot read PostgreSQL system_identifier from live container before first r55 attempt')
    baseline={'created_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'project':project,'postgres_system_identifier':sid,'volumes':current_vols,'logical_volumes':current_logical}
    tmp=baseline_path+'.tmp'; open(tmp,'w').write(json.dumps(baseline,indent=2)+'\n'); os.chmod(tmp,0o600); os.replace(tmp,baseline_path)
    logical=current_logical; vols=current_vols
    print('persistent_volume_baseline=created')
lines=['volumes:']
for logical_name,name in sorted(logical.items()):
    lines += [f'  {logical_name}:','    external: true',f'    name: {name}']
tmp=override_path+'.tmp'; open(tmp,'w').write('\n'.join(lines)+'\n'); os.chmod(tmp,0o600); os.replace(tmp,override_path)
print('persistent_volumes='+','.join(sorted(vols)))
PY
# Current stable runtime becomes authoritative. Old release files are only an import source.
printf 'enabled %s node=%s source=%s\n' "$(date -u +%FT%TZ)" "$node" "$source_label" > "$RUNTIME/enabled"
chmod 600 "$RUNTIME/enabled" "$CFG/taskforge.env" "$CFG/cluster.json" "$CFG/inventory.json" 2>/dev/null || true
chown -R "$op_user:$op_group" "$CFG" "$STATE" 2>/dev/null || true
chmod 700 "$CFG" "$CFG/code-analyzer" "$CFG/docker" "$RUNTIME" "$RUNTIME/easy" 2>/dev/null || true
# Edge API credentials remain root-only even though read-only operational config belongs to the deployment operator.
for secret in "$CFG/cloudflare-api-token" "$CFG/cloudflare-failover.json"; do [ ! -e "$secret" ] || { chown root:root "$secret"; chmod 600 "$secret"; }; done
chmod 0777 "$STATE/image-analyzer-model-cache" 2>/dev/null || true

echo "state_source=$source_label"
echo "node=$node operator=$op_user:$op_group"
echo 'PASS: live state staged without requiring any source revision'
