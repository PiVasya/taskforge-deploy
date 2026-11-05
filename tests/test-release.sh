#!/usr/bin/env bash
set -Eeuo pipefail
export PYTHONDONTWRITEBYTECODE=1
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
[ "$(tr -d '\r\n' < REVISION)" = 410509 ]
[ "$(tr -d '\r\n' < VERSION)" = v41.5.9 ]
sha256sum -c RELEASE_MANIFEST.sha256 >/dev/null
echo 'PASS: v41.5.9 release manifest validates'
[ ! -e compat-r48.tar.gz ]
[ ! -e cluster/ops/current/compat-r47-bootstrap.sh ]
if find . -type f \( -name '*r48*.tar.gz' -o -name '*r47*.tar.gz' \) | grep -q .; then echo 'FAIL: historical release tarball embedded' >&2; exit 1; fi
echo 'PASS: no historical release payload is embedded'
[ -z "$(find . -type f -name '*.pyc' -print -quit)" ]
[ -z "$(find . -type d -name '__pycache__' -print -quit)" ]
echo 'PASS: no Python cache artifacts are packaged'
find . -type f -name '*.sh' -print0 | xargs -0 -n1 bash -n
python3 -S - <<'PY'
from pathlib import Path
for p in Path('.').rglob('*.py'):
    compile(p.read_text(encoding='utf-8'),str(p),'exec')
print('PASS: shell/Python syntax')
PY

# Topology policy is symmetric for Primary eligibility while C remains lite.
python3 -S - <<'PY'
import json
from pathlib import Path
c=json.loads(Path('cluster/cluster.default.json').read_text())
assert 'preferred_primary' not in c and 'automatic_failback' not in c
n={str(x['id']):x for x in c['nodes']}
assert n['A']['app']=={'profile':'full','can_be_primary':True}
assert n['B']['app']=={'profile':'full','can_be_primary':True}
assert n['C']['app']=={'profile':'lite','can_be_primary':True}
assert '_maybe_automatic_failback' not in Path('cluster/agent.py').read_text()
print('PASS: A/B full, C lite, all may be Primary, no preferred failback')
PY

grep -Fq 'ClusterDiagnosticsStorage__RetentionDays: ${CLUSTER_DIAGNOSTICS_RETENTION_DAYS:-7}' compose/50-integrations.yaml
grep -Fq 'ClusterDiagnosticsStorage__Prefix: ${CLUSTER_DIAGNOSTICS_S3_PREFIX:-cluster-diagnostics}' compose/50-integrations.yaml
grep -Fq 'CLUSTER_DIAGNOSTICS_RETENTION_DAYS=7' .env.example
echo 'PASS: diagnostics MinIO retention configuration is present'

# 1C runtime must remain opt-in, auto-installable and cluster-aware.
python3 -S - <<'PY_ONEC'
from pathlib import Path
compose=Path('compose/30-execution.yaml').read_text()
assert '\n  onec-runner:\n' in compose
assert '    - onec\n' in compose
assert '/onec-runner:${IMAGE_TAG:-develop}' in compose
assert '\n  onec-runner-net:\n    internal: true\n' in compose
assert 'onec-platform:/opt/1cv8:ro' in compose
assert 'onec-runtime:/opt/taskforge/onec/runtime:ro' in compose
assert 'ONEC_JUDGE_SOURCE_DIR' in compose and 'ONEC_JUDGE_EPF' not in compose
env=Path('.env.example').read_text()
assert 'ONEC_ENABLED=false' in env
for item in ('ONEC_PLATFORM_VERSION=','ONEC_INSTALL_SOURCE=auto','ONEC_INSTALL_SHA256=',
             'ONEC_RUNNER_SLOTS=1','ONEC_RUNNER_TMPFS_SIZE=128m',
             'ONEC_LICENSE_DIR=/etc/taskforge/onec/licenses','TASKFORGE_ONEC_WORK_DIR=/var/lib/taskforge/onec-work'):
    assert item in env
assert 'ONEC_WORK_ROOT: /work' in compose
wrapper=Path('compose.sh').read_text(); assert 'append_profile onec' in wrapper and '$STATE_ROOT/onec-work' in wrapper and '$CONFIG_ROOT/onec/licenses' in wrapper
assert 'chown 65534:65534 "$TASKFORGE_ONEC_WORK_DIR"' in wrapper and 'chmod 0700 "$TASKFORGE_ONEC_WORK_DIR"' in wrapper
assert 'chown root:65534 "$ONEC_LICENSE_DIR"' in wrapper and 'chmod 0750 "$ONEC_LICENSE_DIR"' in wrapper
agent=Path('cluster/agent.py').read_text(); assert "'onec-runner'" in agent and "excluded.add('onec-runner')" in agent
rehome=Path('cluster/ops/current/rehome-replica.sh').read_text(); assert 'onec-runner' in rehome and 'ONEC_ENABLED=true' in rehome
apply=Path('cluster/ops/current/apply-onec-runtime.sh').read_text()
for needle in ('prepare_onec_volumes','ONEC_PLATFORM_VERSION','ONEC_INSTALL_SHA256','docker volume create',
               '/opt/taskforge/onec/bootstrap','onec-platform','onec-runtime','onec-installer-cache',
               '--network none --user 65534:65534','services+=(onec-runner)',
               'taskforge-onec-smoke','1C end-to-end smoke execution failed',
               'enabled 1C runner did not become healthy',
               'ONEC_PLATFORM_VERSION must use exact A.B.C.D numeric form',
               'TASKFORGE_ONEC_WORK_DIR must stay under /var/lib/taskforge/',
               'chown root:65534 "$license_dir"'):
    assert needle in apply, needle
assert 'develop|latest' not in apply
helper=Path('scripts/onec/configure-source.sh').read_text()
cluster=Path('cluster.sh').read_text()
dist=Path('cluster/ops/current/onec-distribution.sh').read_text()
for needle in ('--file','--url','--version','--sha256','ONEC_ENABLED=true','ONEC_INSTALL_SOURCE=file','ONEC_INSTALL_SOURCE=url','sha256sum','--version must use exact A.B.C.D numeric form'):
    assert needle in helper, needle
for needle in ('taskforge-runtime-cache','taskforge-onec-distribution-v1','taskforge-onec-cache-manifest-v1',
               'taskforge-onec-cache-complete-v1','ONEC_DISTRIBUTION_MAX_SEEDS','ONEC_DISTRIBUTION_PARALLEL',
               'different requesting nodes choose different seed subsets' if False else 'seed subsets',
               'multi-source download','cluster seed ready','current Patroni Primary'):
    assert needle in dist, needle
assert 'onec)' in cluster and 'scripts/onec/configure-source.sh' in cluster and 'onec seed --url' in cluster
assert 'onec-distribution.sh" sync' in apply
print('PASS: 1C runtime is opt-in, persistent-volume auto-installed and cluster-distributed via per-node MinIO')
PY_ONEC

# Normalization both removes old policy and restores the exact desired node profiles.
perm_td="$(mktemp -d /tmp/taskforge-r55-policy.XXXXXX)"; chmod 755 "$perm_td"
printf 'nobody\n' > "$perm_td/operator-user"; printf 'nogroup\n' > "$perm_td/operator-group"
python3 -S - "$perm_td/cluster.json" <<'PY'
import json,sys
c=json.load(open('cluster/cluster.default.json'))
c['preferred_primary']='A';c['automatic_failback']=True;c['nodes'][2]['app']={'profile':'full','can_be_primary':False}
open(sys.argv[1],'w').write(json.dumps(c)+'\n')
PY
printf '%s\n' '{"preferred_primary":"A","nodes":[]}' > "$perm_td/inventory.json"
chmod 600 "$perm_td/cluster.json" "$perm_td/inventory.json"; chown root:root "$perm_td/cluster.json" "$perm_td/inventory.json"
python3 cluster/ops/current/normalize-primary-policy.py --cluster "$perm_td/cluster.json" --inventory "$perm_td/inventory.json" --operator-user-file "$perm_td/operator-user" --operator-group-file "$perm_td/operator-group" >/dev/null
[ "$(stat -c '%U:%G %a' "$perm_td/cluster.json")" = 'nobody:nogroup 600' ]
runuser -u nobody -- cat "$perm_td/cluster.json" >/dev/null
python3 -S - "$perm_td/cluster.json" <<'PY'
import json,sys
c=json.load(open(sys.argv[1]));n={x['id']:x for x in c['nodes']}
assert 'preferred_primary' not in c and 'automatic_failback' not in c
assert n['A']['app']['profile']=='full' and n['B']['app']['profile']=='full' and n['C']['app']['profile']=='lite'
assert all(n[x]['app']['can_be_primary'] is True for x in 'ABC')
PY
rm -rf "$perm_td"
echo 'PASS: topology normalization and operator ownership'

# State import test: source release number is deliberately arbitrary and never interpreted.
td="$(mktemp -d /tmp/taskforge-r55-state.XXXXXX)"; old="$td/taskforge-prod-linux-v40-cluster-control-final-r12"; cfg="$td/etc"; state="$td/var"; bin="$td/bin"
mkdir -p "$old/cluster" "$old/.runtime/cluster/shared/content-data-protection" "$old/.runtime/cluster/shared/quiz-data-protection" "$old/.runtime/code-analyzer-keys" "$old/.docker" "$bin"
cat > "$old/.env" <<'ENV'
COMPOSE_PROJECT_NAME=taskforge-prod
DOMAIN=taskforge.by
CT_DOMAIN=ct.taskforge.by
LETSENCRYPT_EMAIL=test@example.com
POSTGRES_USER=taskforge
POSTGRES_PASSWORD=x
RABBITMQ_DEFAULT_PASS=x
REDIS_PASSWORD=x
MINIO_ROOT_PASSWORD=x
TASKFORGE_INTERNAL_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
IMAGE_REPOSITORY=ghcr.io/example
S3_PUBLIC_ENDPOINT=https://taskforge.by
JWT_SIGNING_KEY=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
ENV
cp cluster/cluster.default.json "$old/cluster/cluster.json"; cp cluster/inventory.default.json "$old/cluster/inventory.json"
printf 'B\n' > "$old/.runtime/cluster/node-id"
printf 'PRIVATE\n' > "$old/.runtime/code-analyzer-keys/code-analyzer-private.pem"; printf 'PUBLIC\n' > "$old/.runtime/code-analyzer-keys/code-analyzer-public.pem"
printf '<key>content</key>\n' > "$old/.runtime/cluster/shared/content-data-protection/key.xml"; printf '<key>quiz</key>\n' > "$old/.runtime/cluster/shared/quiz-data-protection/key.xml"
printf '{"auths":{}}\n' > "$old/.docker/config.json"
cat > "$bin/systemctl" <<EOS
#!/usr/bin/env bash
if [ "\${1:-}" = show ]; then printf 'ExecStart={ path=python3 ; argv[]=python3 $old/cluster/agent.py ; }\\nWorkingDirectory=$old\\n'; exit 0; fi
exit 0
EOS
cat > "$bin/docker" <<'EOS'
#!/usr/bin/env bash
set -e
cmd="${1:-}"; shift || true
if [ "$cmd" = ps ]; then exit 0; fi
if [ "$cmd" = inspect ]; then
  name="${1:-}"
  svc="${name#taskforge-prod-}"; svc="${svc%-1}"
  target=''; vol=''
  case "$svc" in
    postgres) [ "${FAKE_HIDE_POSTGRES_MOUNT:-0}" = 0 ] || { printf '[{"Mounts":[]}]\n'; exit 0; }; target=/var/lib/postgresql; vol=tf_pg_live ;;
    rabbitmq) target=/var/lib/rabbitmq; vol=tf_rabbit_live ;;
    redis) target=/data; vol=tf_redis_live ;;
    minio) target=/data; vol=tf_minio_live ;;
    etcd) target=/etcd-data; vol=tf_etcd_live ;;
    gateway) printf '[{"Mounts":[{"Type":"volume","Name":"tf_le_live","Source":"/v/tf_le_live","Destination":"/etc/letsencrypt"},{"Type":"volume","Name":"tf_webroot_live","Source":"/v/tf_webroot_live","Destination":"/var/www/certbot"}]}]\n'; exit 0 ;;
    *) printf '[{"Mounts":[]}]\n'; exit 0 ;;
  esac
  printf '[{"Mounts":[{"Type":"volume","Name":"%s","Source":"/v/%s","Destination":"%s"}]}]\n' "$vol" "$vol" "$target"
  exit 0
fi
if [ "$cmd" = volume ] && [ "${1:-}" = ls ]; then
  args="$*"
  case "$args" in
    *com.docker.compose.volume=postgres-data*|*com.docker.compose.volume=postgres-cluster-data*) printf 'tf_pg_live\n' ;;
  esac
  exit 0
fi
if [ "$cmd" = volume ] && [ "${1:-}" = inspect ]; then
  name="${2:-}"
  case "$name" in tf_pg_live|tf_rabbit_live|tf_redis_live|tf_minio_live|tf_etcd_live|tf_le_live|tf_webroot_live) ;; *) exit 1 ;; esac
  stamp=2026-01-01T00:00:00Z; [ "${FAKE_MUTATE:-0}" = 0 ] || { [ "$name" != tf_pg_live ] || stamp=2026-02-02T00:00:00Z; }
  printf '[{"Name":"%s","Driver":"local","Mountpoint":"/v/%s","CreatedAt":"%s","Scope":"local"}]\n' "$name" "$name" "$stamp"; exit 0
fi
if [ "$cmd" = exec ]; then printf '7777777777777777777\n'; exit 0; fi
exit 1
EOS
chmod +x "$bin/systemctl" "$bin/docker"
PATH="$bin:$PATH" SUDO_USER=root TASKFORGE_CONFIG_ROOT="$cfg" TASKFORGE_STATE_ROOT="$state" bash cluster/ops/current/stage-current-state.sh > "$td/stage.out"
[ "$(cat "$state/cluster/node-id")" = B ]
cmp "$old/.env" "$cfg/taskforge.env"
grep -Fq 'name: tf_pg_live' "$state/cluster/storage-bindings.yaml"
grep -Fq 'name: tf_etcd_live' "$state/cluster/storage-bindings.yaml"
grep -Fq 'name: tf_le_live' "$state/cluster/storage-bindings.yaml"
[ "$(cat "$cfg/code-analyzer/code-analyzer-private.pem")" = PRIVATE ]
python3 -S - "$state/cluster/storage-baseline-r55.json" <<'PY'
import json,sys
b=json.load(open(sys.argv[1]));assert b['postgres_system_identifier']=='7777777777777777777';assert b['logical_volumes']['postgres-cluster-data']=='tf_pg_live';assert b['logical_volumes']['etcd-data']=='tf_etcd_live'
PY
# If a core container mount is temporarily absent, recover the exact Compose volume by its Docker labels instead of inventing a new one.
PATH="$bin:$PATH" FAKE_HIDE_POSTGRES_MOUNT=1 SUDO_USER=root TASKFORGE_CONFIG_ROOT="$cfg" TASKFORGE_STATE_ROOT="$state" bash cluster/ops/current/stage-current-state.sh >/dev/null
! grep -Fq "$old" "$state/cluster/enabled"
# Retry reuses the first baseline and detects storage identity drift instead of accepting it.
if PATH="$bin:$PATH" FAKE_MUTATE=1 SUDO_USER=root TASKFORGE_CONFIG_ROOT="$cfg" TASKFORGE_STATE_ROOT="$state" bash cluster/ops/current/stage-current-state.sh >/dev/null 2>&1; then echo 'FAIL: changed volume fingerprint was accepted' >&2; exit 1; fi
rm -rf "$td"
echo 'PASS: arbitrary old release state import + immutable persistent-volume baseline'

# Migration logic must be based on state, never a source revision allow-list.
python3 -S - <<'PY'
from pathlib import Path
m=Path('cluster/ops/current/migrate.sh').read_text(); st=Path('cluster/ops/current/stage-current-state.sh').read_text(); c=Path('compose.sh').read_text(); rr=Path('cluster/ops/current/rehome-replica.sh').read_text(); rp=Path('cluster/ops/current/rehome-primary.sh').read_text()
assert 'case "$rev"' not in m and 'unsupported stable revision' not in m
assert 'compat_r47_bootstrap' not in m and 'embedded r48' not in m
assert 'active_source_root' in st and 'storage-baseline-r55.json' in st and 'storage-bindings.yaml' in st
assert 'storage-bindings.yaml' in c
assert 'migrate-r55.json' in rr and 'migrate-r48.json' not in rr and 'migrate-r47.json' not in rr
assert 'primary-rehome-r55.json' in rp and 'preferred_primary' not in rp
print('PASS: migration is state-based and contains no historical migration engine')
PY

# Migration code must never delete/create persistent volumes, and Primary re-home may use a healthy peer regardless of its old control revision.
python3 -S - <<'PY'
from pathlib import Path
paths=[Path('cluster/ops/current/migrate.sh'),Path('cluster/ops/current/stage-current-state.sh'),Path('cluster/ops/current/rehome-node.sh'),Path('cluster/ops/current/rehome-replica.sh'),Path('cluster/ops/current/rehome-primary.sh')]
text='\n'.join(p.read_text() for p in paths)
for bad in ('docker volume rm','docker volume create','docker volume prune','down -v'):
    assert bad not in text,bad
primary=Path('cluster/ops/current/rehome-primary.sh').read_text()
assert 'bundle_revision' not in primary
assert 'current_leader' in primary and 'select_candidate' in primary
print('PASS: persistent-volume mutation is absent and temporary Primary selection is revision-agnostic')
PY

# Re-home app inventory must cover every current non-storage service exactly.
python3 - <<'PY'
import re,yaml
from pathlib import Path
root=Path('.')
sv=set()
for p in list((root/'compose').glob('*.yaml'))+[root/'cluster/compose.cluster.yaml',root/'cluster/compose.telemetry.yaml']:
    sv.update(((yaml.safe_load(p.read_text()) or {}).get('services') or {}).keys())
text=(root/'cluster/ops/current/rehome-replica.sh').read_text();m=re.search(r'APP_FULL=\((.*?)\)\nassigned=',text,re.S);apps=set(m.group(1).split())
expected=sv-{'postgres','etcd','rabbitmq','redis','minio','watchtower','certbot'}
assert apps==expected,(sorted(expected-apps),sorted(apps-expected))
print('PASS: re-home service inventory matches current Compose')
PY

plan="$(python3 cluster/clusterctl.py --config cluster/cluster.default.json --env-file .env.example cloudflare)"
PLAN="$plan" python3 -S - <<'PY'
import json,os
p=json.loads(os.environ['PLAN']);assert p['paid_load_balancing_required'] is False;assert [x['node'] for x in p['public_candidates']]==['A','B','C']
print('PASS: Cloudflare edge candidates A/B/C')
PY

# Manual Primary switch remains symmetric, including C/lite.
python3 -S - <<'PY'
import importlib.util,json,tempfile
from pathlib import Path
from types import SimpleNamespace
spec=importlib.util.spec_from_file_location('primary','cluster/primary.py');pm=importlib.util.module_from_spec(spec);spec.loader.exec_module(pm)
with tempfile.TemporaryDirectory() as raw:
 td=Path(raw);rt=td/'runtime';rt.mkdir();(rt/'node-id').write_text('B')
 cluster={'maximum_lag_on_switchover_bytes':16777216,'nodes':[{'id':'A','public_host':'1.1.1.1','app':{'profile':'full','can_be_primary':True},'wireguard':{'ip':'10.0.0.1'},'postgres':{},'health_port':9187},{'id':'B','public_host':'2.2.2.2','app':{'profile':'full','can_be_primary':True},'wireguard':{'ip':'10.0.0.2'},'postgres':{},'health_port':9187},{'id':'C','public_host':'3.3.3.3','app':{'profile':'lite','can_be_primary':True},'wireguard':{'ip':'10.0.0.3'},'postgres':{},'health_port':9187}]}
 cp=td/'cluster.json';cp.write_text(json.dumps(cluster));ep=td/'env';ep.write_text('TASKFORGE_INTERNAL_KEY='+'x'*64+'\n')
 view={'members':[{'name':'A','role':'leader','state':'running','lag':0},{'name':'B','role':'replica','state':'streaming','lag':0},{'name':'C','role':'replica','state':'streaming','lag':1024}]};calls=[]
 old=(pm.cluster_view,pm.configured_edge_on_any,pm.validate_agent,pm.post_switchover,pm.wait_for_primary)
 try:
  pm.cluster_view=lambda nodes,local:(view,'B');pm.configured_edge_on_any=lambda nodes:(False,{'A':False,'B':False,'C':False})
  pm.validate_agent=lambda nid,node,role,leader,hot:{'node':{'id':nid,'bundle_revision':'73'},'ha':{'role':role,'leader':leader,'reconciled':True,'control_plane_fenced':False,'hot_start_ready':True},'edge':{'configured':False}}
  pm.post_switchover=lambda leader_node,leader,target,env:(calls.append((leader,target)) or (200,'ok'))
  pm.wait_for_primary=lambda target,node,edge,timeout:{'node':{'bundle_revision':'73'},'ha':{'role':'primary','leader':target,'applications_active':True,'traffic_ready':False},'edge':{'configured':False}}
  assert pm.switch(SimpleNamespace(config=str(cp),env_file=str(ep),runtime=str(rt),target='C',timeout=1))==0 and calls==[('A','C')]
 finally: pm.cluster_view,pm.configured_edge_on_any,pm.validate_agent,pm.post_switchover,pm.wait_for_primary=old
print('PASS: manual Primary switch accepts C/lite')
PY

python3 tests/test_agent_efficiency.py
echo 'PASS: Node Agent batching/standby-degraded efficiency tests'
python3 tests/test_notification_noise.py
echo 'PASS: HA/TG notification-noise hotfix tests'
python3 tests/test_r66_operability.py
echo 'PASS: inherited r66 operability regressions'
python3 tests/test_r66_diagnostics_control.py
echo 'PASS: inherited r66 diagnostics control regressions'
python3 tests/test_r68_diagnostics_progress.py
echo 'PASS: r68 real diagnostics progress regressions'
python3 tests/test_r68_local_control_socket.py
echo 'PASS: r68 local Unix control socket regression'
python3 tests/test_r66_sql_reconcile.py
echo 'PASS: inherited r66 SQL reconcile regressions'
python3 tests/test_log_cleanup.py
echo 'PASS: v41.5.9 bounded authenticated log cleanup regressions'
python3 tests/test_container_state.py
echo 'PASS: v41.5.9 container inventory and local self-heal regressions'

echo 'V41.5.9 BASE REGRESSION TEST: PASS'

python3 tests/test_edge_lifecycle.py
python3 tests/test_edge_integration.py
python3 tests/test_sql_runtime.py
echo "V41.5.9 RELEASE TEST: PASS (offline; no production Docker/Cloudflare/ACME execution)"

python3 tests/test_managed_updates.py
python3 tests/test_update_extra.py
python3 tests/test_v4151_update_reliability.py
python3 tests/test_v4152_ownership.py
python3 tests/test_v4153_standby_efficiency.py
python3 tests/test_v4154_standby_consistency.py
python3 tests/test_v4155_docker_observe.py
python3 tests/test_v4156_standby_health_templates.py
echo "PASS: v41.5.9 registry fairness, retries, Watchtower retirement and migration safety"
echo "PASS: application updater isolation, retries, rollback, role fences and measured telemetry"

# v41.5.9 regression: transient Patroni /cluster misses must not drain Primary apps.
python3 tests/test_primary_cluster_desired_hold_hotfix.py
echo "PASS: v41.5.9 Patroni leader-lookup hold and confirmed-demotion fencing"

# v41.5.9 regression: an Exited container with stale unhealthy healthcheck
# must not perpetually extend its retry cooldown without attempting start.
python3 -B tests/test_v4158_stopped_unhealthy_retry.py
echo "PASS: v41.5.9 stopped/unhealthy restart and bounded re-crash retries"

python3 -B tests/test_v4159_ha_stability.py
python3 -B tests/test_v4159_policy.py
python3 -B tests/test_v4159_diagnostics.py
echo "PASS: v41.5.9 HA confirmation, mutation gates, timing policy and forensic logs"
