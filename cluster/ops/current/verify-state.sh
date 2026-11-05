#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
baseline="$TASKFORGE_CLUSTER_RUNTIME/storage-baseline-r55.json"
[ -s "$baseline" ] || cluster_die 'storage baseline missing'
project="$(cluster_project)"
python3 -S - "$baseline" "$project" <<'PY'
import json,subprocess,sys
baseline=json.load(open(sys.argv[1]));project=sys.argv[2]
if baseline.get('project')!=project: raise SystemExit(f'project changed: before={baseline.get("project")} after={project}')
checks=[('postgres','/var/lib/postgresql'),('rabbitmq','/var/lib/rabbitmq'),('redis','/data'),('minio','/data'),('etcd','/etcd-data')]
def inspect(name):
 p=subprocess.run(['docker','inspect',name],text=True,capture_output=True)
 if p.returncode: raise SystemExit('container missing after migration: '+name)
 return json.loads(p.stdout)[0]
def fp(name):
 p=subprocess.run(['docker','volume','inspect',name],text=True,capture_output=True)
 if p.returncode: raise SystemExit('Docker volume missing after migration: '+name)
 d=json.loads(p.stdout)[0];return {k:d.get(k,'') for k in ('Name','Driver','Mountpoint','CreatedAt','Scope')}
for svc,target in checks:
 d=inspect(f'{project}-{svc}-1');m=[x for x in d.get('Mounts',[]) if x.get('Destination')==target and x.get('Type')=='volume']
 if len(m)!=1: raise SystemExit(f'{svc} persistent mount invalid after migration: {m!r}')
 name=m[0].get('Name','');before=baseline['volumes'].get(name)
 if before is None: raise SystemExit(f'{svc} now uses an unproven volume: {name}')
 after=fp(name)
 if after!=before: raise SystemExit(f'{svc} volume fingerprint changed: before={before!r} after={after!r}')
for name,before in baseline.get('volumes',{}).items():
 after=fp(name)
 if after!=before: raise SystemExit(f'preserved volume fingerprint changed: {name} before={before!r} after={after!r}')
pg=f'{project}-postgres-1'
p=subprocess.run(['docker','exec',pg,'sh','-lc','psql -X -U "$POSTGRES_USER" -d postgres -Atc "select system_identifier from pg_control_system();"'],text=True,capture_output=True)
sid=p.stdout.strip() if p.returncode==0 else ''
if not sid or sid!=baseline.get('postgres_system_identifier'): raise SystemExit(f'PostgreSQL system_identifier changed: before={baseline.get("postgres_system_identifier")} after={sid}')
print('persistent_data_identity=PASS')
print('postgres_system_identifier='+sid)
PY
bash /opt/taskforge/control/cluster/ops/current/mount-audit.sh
