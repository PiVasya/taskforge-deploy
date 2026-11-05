#!/usr/bin/env python3
import json, os, subprocess, tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SCRIPT=ROOT/'cluster/ops/current/reconcile-sql.sh'


def write_exe(path: Path, text: str):
    path.write_text(text)
    path.chmod(0o755)


def run_case(role='primary', node='A', profile='full', sql_enabled=True, soft=False):
    with tempfile.TemporaryDirectory(prefix='taskforge-r66-sql-') as raw:
        td=Path(raw); cfg=td/'etc'; runtime=td/'state/cluster'; bin_dir=td/'bin'; cfg.mkdir(); runtime.mkdir(parents=True); bin_dir.mkdir()
        (runtime/'node-id').write_text(node+'\n')
        cluster={'nodes':[{'id':node,'wireguard':{'ip':'10.80.0.1'},'health_port':9187,'app':{'profile':profile,'can_be_primary':True}}]}
        (cfg/'cluster.json').write_text(json.dumps(cluster))
        (cfg/'taskforge.env').write_text('COMPOSE_PROJECT_NAME=taskforge-prod\nTASKFORGE_APP_PROFILE='+profile+'\n')
        (cfg/'sql-runtime.env').write_text(('SQL_ENABLED=true\n' if sql_enabled else 'SQL_ENABLED=false\n')+'SQL_WORKER_ID=sql-'+node+'\n')
        log=td/'docker.log'
        write_exe(bin_dir/'curl', f'''#!/usr/bin/env bash\nprintf '%s\\n' '{json.dumps({'ha':{'role':role}})}'\n''')
        write_exe(bin_dir/'docker', f'''#!/usr/bin/env bash\nset -e\necho "$*" >> {str(log)!r}\nif [ "${{1:-}}" = compose ]; then\n  shift\n  args=("$@")\n  for ((i=0;i<${{#args[@]}};i++)); do\n    if [ "${{args[$i]}}" = ps ] && [ "${{args[$((i+1))]:-}}" = -q ]; then\n      svc="${{args[$((i+2))]:-}}"\n      echo "cid-$svc"\n      exit 0\n    fi\n  done\n  exit 0\nfi\nif [ "${{1:-}}" = inspect ]; then\n  echo 'running|healthy|false|0'\n  exit 0\nfi\nif [ "${{1:-}}" = exec ]; then\n  echo '{{"ok":true,"buildFingerprint":"test"}}'\n  exit 0\nfi\nif [ "${{1:-}}" = logs ]; then exit 0; fi\nexit 0\n''')
        env=os.environ.copy();env.update({'PATH':str(bin_dir)+os.pathsep+env.get('PATH',''),'TASKFORGE_SQL_MIGRATION_SOFT':'1' if soft else '0','TASKFORGE_CONTROL_ROOT':str(ROOT),'TASKFORGE_CONFIG_ROOT':str(cfg),'TASKFORGE_CLUSTER_RUNTIME':str(runtime),'TASKFORGE_STATE_ROOT':str(td/'state'),'TASKFORGE_ENV_FILE':str(cfg/'taskforge.env')})
        p=subprocess.run(['bash',str(SCRIPT)],text=True,capture_output=True,env=env,timeout=20)
        calls=log.read_text() if log.exists() else ''
        return p,calls

p,calls=run_case()
assert p.returncode==0,(p.stdout,p.stderr)
assert '[TFSQL RECONCILE] node=A role=primary action=start-engines mode=strict' in p.stdout
assert '[TFSQL RECONCILE] node=A role=primary action=start-worker' in p.stdout
assert '[TFSQL RECONCILE] node=A role=primary result=PASS' in p.stdout
assert 'up -d --no-deps --pull never sql-postgres sql-mysql' in calls
assert 'up -d --no-deps --pull never sql-worker' in calls
assert 'exec cid-sql-worker /app/sql-worker ready' in calls

p,calls=run_case(role='standby')
assert p.returncode==0
assert 'reason=primary-only-cache' in p.stdout
assert ' up ' not in (' '+calls+' ')

p,calls=run_case(node='C',profile='full')
assert p.returncode==0
assert 'reason=lite' in p.stdout
assert not calls

p,calls=run_case(sql_enabled=False)
assert p.returncode==0
assert 'reason=disabled' in p.stdout
assert not calls

p,calls=run_case(soft=True)
assert p.returncode==0
assert 'mode=migration-soft' in p.stdout
assert 'action=keep-running reason=already-healthy' in p.stdout
assert ' up ' not in (' '+calls+' ')

print('PASS: SQL reconcile is strict manually, bounded/soft during migration, and skips standby/lite nodes')
