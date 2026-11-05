#!/usr/bin/env python3
from __future__ import annotations
import argparse, base64, uuid, hashlib, hmac, json, os, shlex, socketserver, subprocess, threading, time, urllib.error, urllib.request
from contextlib import nullcontext
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from edge_lifecycle import Supervisor
from host_metrics import HostMetrics
from ha_health import HaHealth, Evidence
from managed_updates import ManagedUpdates, DockerApplications
from diagnostics_manager import DiagnosticsManager, DiagnosticsError, diagnostics_token
from log_cleanup import LogCleanupManager, LogCleanupError, maintenance_token

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path(os.environ.get('TASKFORGE_CLUSTER_RUNTIME','/var/lib/taskforge/cluster'))
CONFIG_ROOT=Path(os.environ.get('TASKFORGE_CONFIG_ROOT','/etc/taskforge'))
DOCKER_CONFIG_ROOT=CONFIG_ROOT/'docker'
try:
    DOCKER_CONFIG_ROOT.mkdir(parents=True,exist_ok=True)
except OSError:
    pass
os.environ['DOCKER_CONFIG']=str(DOCKER_CONFIG_ROOT)
ENABLED_FILE=RUNTIME/'enabled'
CONTROL_FENCE_FILE=RUNTIME/'join-rehome-fence'
APP_FULL=['telegram-quiz-bot','gateway','execution-worker','observability-api','execution-api','tasks-api','ai-worker','support-bot','javascript-runner','image-analyzer','solutions-api','identity-api','cpp-runner','rating-worker','minecraft-api','notifications-api','education-api','code-analyzer','support-api','pascal-runner','browser-api','ai-api','java-runner','quiz-api','files-api','csharp-runner','front-ct','content-api','front','python-runner','onec-runner']
SQL_SERVICES={'sql-worker','sql-postgres','sql-mysql'}
APP_FULL+=['sql-postgres','sql-mysql','sql-worker']
APP_LITE=[x for x in APP_FULL if x not in {'browser-api','image-analyzer'} | SQL_SERVICES]
DEFAULT_ASSIST_EXCLUDES={'support-bot','telegram-quiz-bot','rating-worker'} | SQL_SERVICES
OPTIONAL_SERVICES={'image-analyzer','onec-runner','telegram-quiz-bot','support-bot','rating-worker','ai-api','ai-worker','minecraft-api','browser-api'} | SQL_SERVICES
CRITICAL_INFRA=('minio','redis','rabbitmq')
MIGRATION_FENCE_FILE=RUNTIME/'migration-fence'

def utc_now(): return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
def run(args,timeout=30,env=None):
    try:return subprocess.run(args,text=True,capture_output=True,timeout=timeout,env=env)
    except Exception as e:return subprocess.CompletedProcess(args,125,'',str(e))
def read_env(path:Path):
    d={}
    if not path.is_file():return d
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        if not raw.strip() or raw.lstrip().startswith('#') or '=' not in raw:continue
        k,v=raw.split('=',1); v=v.strip()
        if len(v)>=2 and v[:1]==v[-1:] and v[0] in "\"'":v=v[1:-1]
        d[k.strip()]=v
    return d

def patroni_auth_header(env:dict[str,str])->str:
    key=env.get('TASKFORGE_INTERNAL_KEY','')
    if len(key)<40:return ''
    password=hmac.new(key.encode(),b'taskforge-patroni-rest-v2',hashlib.sha512).hexdigest()[:64]
    token=base64.b64encode(f'taskforge_patroni:{password}'.encode()).decode()
    return 'Basic '+token

def chown_to_deployment_owner(path:Path):
    if os.geteuid()!=0:
        return
    try:
        import pwd, grp
        user=(CONFIG_ROOT/'operator-user').read_text().strip()
        group=(CONFIG_ROOT/'operator-group').read_text().strip()
        os.chown(path,pwd.getpwnam(user).pw_uid,grp.getgrnam(group).gr_gid)
    except Exception:
        pass

_LOCAL_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))

def http_json(url,timeout=4):
    try:
        with _LOCAL_HTTP.open(url,timeout=timeout) as r:return r.status,json.load(r)
    except urllib.error.HTTPError as e:
        try:return e.code,json.load(e)
        except Exception:return e.code,{}
    except Exception:return 0,{}


def application_update_policy(enabled, role, observed_leader, confirmed_leader,
                              node_id, can_be_primary, app_mode, unknown_samples):
    """Return (action, reason) without treating a missing /cluster reply as demotion.

    Patroni /patroni can still confirm the local Primary when /cluster times out.
    A *positive* conflicting leader, a confirmed replica, or persistent local
    Patroni uncertainty still fence application writers. A transient missing
    DCS view must HOLD the last desired state, not publish an empty one: doing
    so used to make ManagedUpdates stop every Primary container via docker stop.
    Holding never authorizes new application starts: app_start_allowed performs
    a fresh positive local + cluster leader check for each start/prepare.
    """
    if not enabled:
        return 'stop', 'quorum-disabled'
    if role == 'primary':
        if not can_be_primary:
            return 'stop', 'primary-not-allowed'
        if observed_leader and observed_leader != node_id:
            return 'stop', 'positive-leader-conflict'
        if observed_leader == node_id and confirmed_leader == node_id:
            return 'primary', 'leader-confirmed'
        return 'hold', 'patroni-cluster-leader-unconfirmed'
    if role == 'standby':
        if app_mode == 'assist':
            return 'assist', 'standby-assist-authorized'
        return 'stop', 'patroni-confirmed-standby'
    if unknown_samples >= 3:
        return 'stop', 'patroni-local-role-unavailable'
    return 'hold', 'patroni-local-role-transient'


class Agent:
    def __init__(self,config:Path,env_file:Path):
        self.config_path=config; self.env_file=env_file
        self.cfg=json.loads(config.read_text(encoding='utf-8')); self.env=read_env(env_file)
        self.patroni_auth_header=patroni_auth_header(self.env)
        # Process identity is immutable for the lifetime of this Agent. The
        # stable control directory can be atomically replaced by a newer release
        # while the old process is still running; rereading REVISION from disk
        # would otherwise let old code impersonate the new revision.
        self.bundle_version=(ROOT/'VERSION').read_text().strip()
        self.bundle_revision=(ROOT/'REVISION').read_text().strip()
        self.project=self.env.get('COMPOSE_PROJECT_NAME','taskforge-prod')
        self.node_id=(RUNTIME/'node-id').read_text().strip()
        self.node=next(n for n in self.cfg['nodes'] if str(n['id'])==self.node_id)
        app=self.node.get('app',{}) if isinstance(self.node.get('app',{}),dict) else {}
        base=APP_FULL if app.get('profile','full')=='full' else APP_LITE
        excluded={str(x) for x in app.get('exclude_services',[]) if str(x)}
        if read_env(CONFIG_ROOT/'sql-runtime.env').get('SQL_ENABLED','false')!='true': excluded |= SQL_SERVICES
        if self.env.get('ONEC_ENABLED','false').lower()!='true': excluded.add('onec-runner')
        self.assigned=[x for x in base if x not in excluded]
        self.optional=[x for x in self.assigned if x in OPTIONAL_SERVICES]
        self.critical=[x for x in self.assigned if x not in OPTIONAL_SERVICES]
        assist_excluded={str(x) for x in app.get('assist_exclude_services',[]) if str(x)}|DEFAULT_ASSIST_EXCLUDES
        self.assist_enabled=bool(app.get('assist_on_failover',False))
        self.assist=[x for x in self.assigned if x not in assist_excluded]
        self.compose_env=os.environ.copy(); self.compose_env['TASKFORGE_ENV_FILE']=str(env_file); self.compose_env['TASKFORGE_CONTROL_ROOT']=str(ROOT); self.compose_env['TASKFORGE_CLUSTER_RUNTIME']=str(RUNTIME)
        self.lock=threading.RLock(); self.stop=threading.Event(); self.last_seen_leader=''; self._leader_candidate=''; self._leader_candidate_count=0; self._baseline=False
        self._patroni_unknown_count=0
        self._patroni_unknown_since=None
        self._primary_cluster_hold_samples=0
        self._primary_cluster_hold_total=0
        self.events=[]; self._event_lock=threading.RLock()
        try:
            self.events=json.loads((RUNTIME/'agent-events.json').read_text())[-80:]
        except (OSError,ValueError,TypeError):pass
        self.last_shared_push=0.0; self.shared_ok=False
        self._notice_state=self._load_notice_state(); self._primary_timeline=''
        self.cloudflare_last_attempt=0.0; self.cloudflare_last_success=0.0; self.cloudflare_last_error=''
        self.cloudflare_last_target=''; self.cloudflare_last_result={'configured':False,'status':'unconfigured'}
        self.edge_controller=Supervisor(ROOT,self.config_path,self.env_file,RUNTIME,self.critical)
        self.diagnostics_token=diagnostics_token(self.env)
        self.maintenance_token=maintenance_token(self.env)
        self.diagnostics=DiagnosticsManager(ROOT,RUNTIME,self.node_id,self.env)
        self.log_cleanup=LogCleanupManager(RUNTIME,self.project)
        self.maintenance_lock=threading.RLock()
        self.telemetry=self._skeleton()
        self._write_state()
        self._sql_lock=threading.Lock(); self._sql_desired=False; self._sql_force=False
        self._host_snapshot_cache=None; self._host_snapshot_cache_at=0.0
        self._tls_assets_cache=None; self._tls_assets_cache_at=0.0
        self._sql_thread=threading.Thread(target=self._sql_loop,name='sql-aux-runtime',daemon=True)
        self._sql_thread.start()
        self.metrics=HostMetrics(RUNTIME,self.stop)
        self.updater=ManagedUpdates(DockerApplications(ROOT,RUNTIME,self.project,self.compose_env),RUNTIME,
            self.assigned,self.stop,self.app_start_allowed,
            lambda: CONTROL_FENCE_FILE.is_file() or MIGRATION_FENCE_FILE.is_file() or (RUNTIME/'application-updates-paused').exists(),self._event,
            poll=int(self.env.get('TASKFORGE_UPDATE_POLL_SECONDS','300') or 300),
            can_stop=self.app_stop_required, can_recover=self.app_recovery_allowed,
            can_poll=self.background_updates_allowed)
        self.ha_health=HaHealth(self.cfg['nodes'], self.node_id, RUNTIME, self.stop, self._event)
        ha_worker=threading.Thread(target=self.ha_health.loop,name='ha-connectivity',daemon=True)
        self.ha_health.thread=ha_worker
        ha_worker.start()
        for target,name in ((self.metrics.loop,'host-metrics'),(self.updater.reconcile_loop,'app-lifecycle'),(self.updater.registry_loop,'image-poll')):
            worker = threading.Thread(target=target,name=name,daemon=True)
            if name == 'app-lifecycle': self.updater.lifecycle_thread = worker
            if name == 'image-poll': self.updater.registry_thread = worker
            worker.start()
    def _sql_loop(self):
        # Auxiliary engine startup/cleanup is deliberately outside the HA/edge thread.
        # No SQL health or cache operation is allowed to delay Primary/Cloudflare proof.
        while not self.stop.wait(5):
            try:
                if CONTROL_FENCE_FILE.is_file() or MIGRATION_FENCE_FILE.is_file() or (RUNTIME/'application-updates-paused').exists():continue
                with self._sql_lock:
                    desired=self._sql_desired; force=self._sql_force; self._sql_force=False
                services=[s for s in ('sql-postgres','sql-mysql') if s in self.assigned]
                if not services: continue
                states=self.inspect_health_many([self.cname(s) for s in services])
                if desired:
                    if force or any(states[self.cname(s)]['status']!='running' for s in services):
                        result=self.compose('up','-d','--no-deps','--pull','never',*services,timeout=90)
                        if result.returncode: self.stop.wait(10)
                else:
                    running=[self.cname(s) for s in services if states[self.cname(s)]['status']=='running']
                    if running: run(['docker','stop','-t','10',*running],45)
            except Exception:
                self.stop.wait(10)
    def _sql_request(self,desired,force=False):
        with self._sql_lock:
            self._sql_desired=desired; self._sql_force=self._sql_force or force
    @property
    def quorum_enabled(self):return ENABLED_FILE.is_file()
    def _skeleton(self):
        return {'schema_version':1,'generated_at':utc_now(),'cluster':self.cfg.get('cluster_name','taskforge-prod'),
          'node':{'id':self.node_id,'priority':self.node.get('priority',0),'dcs_voter':self.node.get('dcs_voter',True),'can_be_primary':self.node.get('app',{}).get('can_be_primary',True),'app_profile':self.node.get('app',{}).get('profile','full'),'deployment_mode':'quorum' if self.quorum_enabled else 'replica','bundle_version':self.bundle_version,'bundle_revision':self.bundle_revision},
          'topology':[], 'ha':{'reconciled':False,'role':'unknown','leader':'','app_mode':'initializing','applications_active':False,'traffic_ready':False,'hot_start_ready':False,'hot_start_blockers':['telemetry-initializing'],'tls_ready':False,'tls_assets_ready':False,'tls_live_ready':False,'failover_https_ready':False,'last_error':''},
          'host':{'status':'initializing'},'firewall':{'status':'unknown'},'wireguard':{'interface':'wg-taskforge','peers':[]},
          'postgres':{'healthy':None,'actual_role':'unknown','receiver':'','source':'','slot':'','replay_gap_bytes':None,'replicas':[],'role':'unknown','leader':'','member':{}},
          'minio':{'ready':None},'edge':{'provider':'cloudflare-dns','configured':self._cloudflare_configured(),'state':self.cloudflare_last_result,'last_success_epoch':0},'docker':{'containers':[],'services':[],'container_count':0,'assigned_app_count':len(self.assigned),'prepared_app_count':0,'running_app_count':0,'images_ready':0,'images_missing':len(self.assigned)},
          'update':{'mode':'node-agent','running':False,'watchtower_running':False,'include_stopped':True,'revive_stopped':False,'status':'initializing','started_at':'','completed_at':'','changed_services':[],'error':'','watchtower_error':'','poll_interval_seconds':int(self.env.get('WATCHTOWER_POLL_INTERVAL','300') or 300)},'events':[]}
    def _event(self,kind,title,detail,severity='info',node=None,event_id=None):
        # Events are transition records, not a transport for repeated Compose output.
        evt={'id':event_id or uuid.uuid4().hex,'at':utc_now(),'kind':kind,'title':title,'detail':str(detail)[:700],'severity':severity}
        if node: evt['node']=str(node)
        with getattr(self,'_event_lock',nullcontext()):
            if any(x.get('id')==evt['id'] for x in self.events):return
            self.events.append(evt);self.events=self.events[-80:]
            if hasattr(self,'_event_lock'):
                self._write_json(RUNTIME/'agent-events.json',self.events)

    def _load_notice_state(self):
        path=RUNTIME/'notification-state.json'
        try:
            value=json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value,dict) else {}
        except Exception:
            return {}
    def _save_notice_state(self):
        self._write_json(RUNTIME/'notification-state.json',self._notice_state)
    def _write_json(self,path:Path,obj):
        path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name(path.name+f'.tmp.{os.getpid()}.{uuid.uuid4().hex}'); tmp.write_text(json.dumps(obj,ensure_ascii=False,separators=(',',':'))+'\n',encoding='utf-8');os.chmod(tmp,0o600);chown_to_deployment_owner(tmp);os.replace(tmp,path)
    def _write_state(self):
        state={'node':self.node_id,'deployment_mode':'quorum' if self.quorum_enabled else 'replica','updated_at':utc_now()}
        self._write_json(RUNTIME/'agent-state.json',state); self._write_json(RUNTIME/'node-telemetry.json',self.telemetry)
    def ensure_storage(self):
        # r8 invariant: pre-quorum Agent must never start or reconcile data services.
        if not self.quorum_enabled:
            return
        # Patroni/etcd and all data services are managed by explicit cluster ops.
        # The Agent deliberately does not run compose-up for storage in standby mode.
        return
    def compose(self,*args,timeout=300):
        return run(['bash',str(ROOT/'compose.sh'),*args],timeout=timeout,env=self.compose_env)
    def _atomic_env(self,values):
        path=RUNTIME/'app-routing.env'; path.parent.mkdir(parents=True,exist_ok=True)
        body=''.join(f'{k}={shlex.quote(str(v))}\n' for k,v in values.items())
        try:
            if path.read_text(encoding='utf-8')==body:return False
        except FileNotFoundError:pass
        tmp=path.with_name(path.name+f'.tmp.{os.getpid()}.{uuid.uuid4().hex}')
        tmp.write_text(body,encoding='utf-8');os.chmod(tmp,0o600);chown_to_deployment_owner(tmp);os.replace(tmp,path);return True
    def _agent_urls(self):
        # Include the local Agent as an HTTP liveness source too. observability-api
        # still reads the full local telemetry file, but /ha/live now decides local
        # availability just like it does for peers. A slow/heavy telemetry refresh
        # therefore cannot make the current Primary look dead in Telegram.
        return ';'.join(f"http://{n['wireguard']['ip']}:{int(n.get('health_port',9187))}" for n in self.cfg['nodes'])
    def _write_local_routing(self):
        return self._atomic_env({'DB_HOST':'postgres','DB_PORT':'5432','RABBITMQ_HOST':'rabbitmq','REDIS_HOST':'redis','S3_INTERNAL_ENDPOINT':'http://minio:9000','MIGRATE_ON_STARTUP':'true','BROWSER_API_UPSTREAM':'browser-api:8080','IMAGE_ANALYZER_UPSTREAM':'image-analyzer:8080','TASKFORGE_APP_RUNTIME_MODE':'primary','CLUSTER_TELEMETRY_NOTIFICATIONS_ENABLED':'true','TASKFORGE_CLUSTER_AGENT_URLS':self._agent_urls()})
    def _write_standby_routing(self):
        return self._atomic_env({'DB_HOST':'postgres','DB_PORT':'5432','RABBITMQ_HOST':'rabbitmq','REDIS_HOST':'redis','S3_INTERNAL_ENDPOINT':'http://minio:9000','MIGRATE_ON_STARTUP':'false','BROWSER_API_UPSTREAM':'browser-api:8080','IMAGE_ANALYZER_UPSTREAM':'image-analyzer:8080','TASKFORGE_APP_RUNTIME_MODE':'standby','CLUSTER_TELEMETRY_NOTIFICATIONS_ENABLED':'false','TASKFORGE_CLUSTER_AGENT_URLS':self._agent_urls()})
    def _write_remote_routing(self,leader_id):
        n=next(n for n in self.cfg['nodes'] if str(n['id'])==leader_id); ip=n['wireguard']['ip']
        return self._atomic_env({'DB_HOST':ip,'DB_PORT':str(int(n.get('postgres',{}).get('host_port',5432))),'RABBITMQ_HOST':ip,'REDIS_HOST':ip,'S3_INTERNAL_ENDPOINT':'http://minio:9000','MIGRATE_ON_STARTUP':'false','BROWSER_API_UPSTREAM':f'{ip}:18080','IMAGE_ANALYZER_UPSTREAM':f'{ip}:18090','TASKFORGE_APP_RUNTIME_MODE':'assist','CLUSTER_TELEMETRY_NOTIFICATIONS_ENABLED':'false','TASKFORGE_CLUSTER_AGENT_URLS':self._agent_urls()})
    def _peer_traffic_ready(self,leader_id):
        try:n=next(n for n in self.cfg['nodes'] if str(n['id'])==leader_id)
        except StopIteration:return False
        code,_=http_json(f"http://{n['wireguard']['ip']}:{int(n.get('health_port',9187))}/ha/traffic-ready",3)
        return code==200
    def _tls_mode(self):
        return str((self.node.get('web') or {}).get('tls_mode','auto')).lower()
    def _tls_assets_ready(self):
        if self._tls_mode() in {'http','off','disabled','none'}: return True
        # Certificate verification is intentionally cached for a few seconds. The
        # certificate/key files do not flap on a five-second cadence, while the
        # full status probe performs Docker-volume and OpenSSL checks. Public
        # traffic liveness is still verified separately by _tls_live_ready().
        now=time.monotonic(); cached=getattr(self,'_tls_assets_cache',None); at=getattr(self,'_tls_assets_cache_at',0.0)
        if cached is not None and now-at<15.0:return bool(cached)
        p=run(['bash',str(ROOT/'cluster/ops/current/local-cert.sh'),'status'],8,env=self.compose_env)
        value=p.returncode==0
        self._tls_assets_cache=value; self._tls_assets_cache_at=now
        return value
    def _tls_live_ready(self):
        if self._tls_mode() in {'http','off','disabled','none'}: return True
        if not self._tls_assets_ready(): return False
        port=int(self.env.get('PUBLIC_HTTPS_PORT','443') or 443)
        domains=list(dict.fromkeys(self.env.get(k,'').strip() for k in ('DOMAIN','CT_DOMAIN') if self.env.get(k,'').strip()))
        if not domains:return False
        for domain in domains:
            p=run(['curl','-q','-fsS','--noproxy','*','--connect-timeout','2','--max-time','4','--resolve',f'{domain}:{port}:127.0.0.1',f'https://{domain}:{port}/health/gateway'],6)
            if p.returncode:return False
        return True
    def _cloudflare_configured(self):
        return (CONFIG_ROOT/'cloudflare-api-token').is_file() and (CONFIG_ROOT/'cloudflare-failover.json').is_file()
    def _edge_refresh(self,eligible):
        previous=self.cloudflare_last_result
        state=self.edge_controller.update(eligible)
        self.cloudflare_last_result=state
        if state.get('success'):
            self.cloudflare_last_success=time.time()
            if not previous.get('success'):
                # Public-ready is a transition notification, not a periodic proof
                # notification. The edge worker re-proves readiness and the Agent
                # itself can restart without a Primary change, so persist the last
                # notified Patroni term and suppress duplicates for that term.
                term=f"{self.node_id}|{self._primary_timeline or 'unknown'}"
                if self._notice_state.get('edge_public_ready_term')!=term:
                    self._event('edge.public_ready','Public route and certificate verified',f'node={self.node_id}; DNS -> settle -> HTTP-01 route -> local certificate -> HTTPS','success')
                    self._notice_state['edge_public_ready_term']=term
                    self._notice_state['edge_public_ready_at']=utc_now()
                    self._save_notice_state()
            self.cloudflare_last_error=''
        elif eligible:
            error=str(state.get('last_error',''))
            if error and error!=self.cloudflare_last_error:
                self._event('edge.bootstrap_pending','Public route/certificate pending',error,'warning')
                self.cloudflare_last_error=error
        return bool(state.get('success'))
    def _infra_status(self):
        states=self.inspect_health_many([self.cname(s) for s in CRITICAL_INFRA])
        return {svc:(states[self.cname(svc)]['status']=='running' and states[self.cname(svc)]['health'] in {'healthy','none'}) for svc in CRITICAL_INFRA}
    def _app_counts(self,rows,services):
        wanted=set(services); rr=[x for x in rows if x.get('service') in wanted]
        prepared=sum(1 for x in rr if x.get('status')!='missing')
        running=sum(1 for x in rr if x.get('status')=='running')
        ready=sum(1 for x in rr if x.get('status')=='running' and x.get('health') in {'healthy','none'})
        return prepared,running,ready
    def _degraded_services(self,rows,expected=None):
        # 'degraded' means an optional feature that is expected to be active in
        # the current role but is unhealthy. Optional services deliberately
        # stopped on a warm standby are not degraded.
        optional=set(self.optional)
        expected=optional if expected is None else optional.intersection(set(expected))
        return sorted(x.get('service') for x in rows if x.get('service') in expected and not (x.get('status')=='running' and x.get('health') in {'healthy','none'}))
    def cname(self,svc):return f'{self.project}-{svc}-1'
    def _container_group(self,svc):
        if svc in SQL_SERVICES:return 'sql'
        if svc in {'postgres','etcd','minio','redis','rabbitmq'}:return 'infrastructure'
        if svc=='watchtower':return 'updater'
        if svc=='certbot':return 'maintenance'
        if svc in APP_FULL:return 'application'
        return 'other'
    def _container_expected_state(self,svc,app_mode):
        if svc=='watchtower':return 'stopped'
        group=self._container_group(svc)
        if group=='infrastructure' or group=='updater':return 'running'
        if group=='maintenance':return 'on-demand'
        if svc not in self.assigned:return 'not-assigned'
        if app_mode=='primary':return 'running'
        if app_mode=='assist':return 'running' if svc in self.assist else 'prepared'
        if app_mode in {'uncertain-grace','migration-fenced'}:return 'hold'
        return 'prepared'
    def docker_inventory(self,app_mode):
        # One project-wide Docker inventory for the UI. This deliberately avoids
        # `docker stats`: liveness/health is a control-plane signal and should not
        # make the five-second Agent loop expensive.
        now=time.monotonic(); cached=getattr(self,'_docker_inventory_cache',None); at=getattr(self,'_docker_inventory_cache_at',0.0)
        cache_mode=getattr(self,'_docker_inventory_cache_mode','')
        if cached is not None and now-at<8.0 and cache_mode==app_mode:return cached
        ids=run(['docker','ps','-aq','--filter',f'label=com.docker.compose.project={self.project}'],12)
        rows=[]
        if ids.returncode==0:
            values=[x.strip() for x in ids.stdout.splitlines() if x.strip()]
            if values:
                inspected=run(['docker','inspect',*values],20)
                if inspected.returncode==0:
                    try: objects=json.loads(inspected.stdout)
                    except Exception: objects=[]
                    for obj in objects if isinstance(objects,list) else []:
                        labels=((obj.get('Config') or {}).get('Labels') or {})
                        svc=str(labels.get('com.docker.compose.service') or '')
                        if not svc:continue
                        state=obj.get('State') or {}; health=(state.get('Health') or {}).get('Status') or 'none'
                        rows.append({'service':svc,'name':str(obj.get('Name') or '').lstrip('/'),'state':str(state.get('Status') or 'unknown'),'health':str(health),'restart_count':int(obj.get('RestartCount') or 0),'oom':bool(state.get('OOMKilled',False)),'started_at':str(state.get('StartedAt') or ''),'finished_at':str(state.get('FinishedAt') or ''),'exit_code':state.get('ExitCode'),'error':str(state.get('Error') or ''),'image_fingerprint':str(obj.get('Image') or ''),'group':self._container_group(svc),'expected_state':self._container_expected_state(svc,app_mode),'assigned':svc in self.assigned})
        by_service={x['service']:x for x in rows}
        expected=set(self.assigned)|{'postgres','minio','redis','rabbitmq'}
        if self.quorum_enabled: expected.add('etcd')
        for svc in sorted(expected):
            if svc in by_service:continue
            rows.append({'service':svc,'name':self.cname(svc),'state':'missing','health':'missing','restart_count':None,'oom':None,'started_at':'','finished_at':'','exit_code':None,'error':'','image_fingerprint':'','group':self._container_group(svc),'expected_state':self._container_expected_state(svc,app_mode),'assigned':svc in self.assigned})
        rows.sort(key=lambda x:(x.get('group','other'),x.get('service',''),x.get('name','')))
        self._docker_inventory_cache=rows;self._docker_inventory_cache_at=now;self._docker_inventory_cache_mode=app_mode
        return rows
    def inspect_health_many(self,names):
        names=list(dict.fromkeys(str(x) for x in names if str(x)))
        out={name:{'status':'missing','health':'missing','restart':None,'oom':None} for name in names}
        if not names:return out
        # Docker CLI startup dominates the old Agent CPU profile. Ask Docker for
        # all container states in one invocation instead of spawning one
        # `docker inspect` process per service every five seconds.
        p=run(['docker','inspect','-f','{{.Name}}|{{.State.Status}}|{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}|{{.RestartCount}}|{{.State.OOMKilled}}',*names],15)
        for line in p.stdout.splitlines():
            x=(line.strip().split('|')+['','','','',''])[:5]
            if not x[0]:continue
            name=x[0].lstrip('/')
            if name not in out:continue
            try: restart=int(x[3] or 0)
            except Exception: restart=0
            out[name]={'status':x[1] or 'missing','health':x[2] or 'missing','restart':restart,'oom':x[4]=='true'}
        return out
    def inspect_health(self,name):
        return self.inspect_health_many([name]).get(name,{'status':'missing','health':'missing','restart':None,'oom':None})
    def _local_patroni(self,path):
        ip=self.node['wireguard']['ip']; port=int(self.node.get('postgres',{}).get('patroni_rest_port',8008))
        code,body=http_json(f'http://{ip}:{port}/{path}',4)
        if code!=0:return code,body
        # Failure of the host->WireGuard published port is not proof that the
        # local Patroni process is down. Read its loopback API inside the same
        # existing container; do not restart or execute PostgreSQL mutations.
        if path not in {'patroni','cluster','primary'}:return code,body
        script=("import json,urllib.request,urllib.error; "
                "op=urllib.request.build_opener(urllib.request.ProxyHandler({})); "
                f"r=op.open('http://127.0.0.1:8008/{path}',timeout=3); "
                "print(json.dumps({'http':r.status,'body':json.load(r)}))")
        result=run(['docker','exec',self.cname('postgres'),'/opt/patroni/bin/python3','-c',script],8)
        if result.returncode==0:
            try:
                data=json.loads(result.stdout)
                if data.get('http')==200 and isinstance(data.get('body'),dict):
                    self._patroni_loopback_recoveries=getattr(self,'_patroni_loopback_recoveries',0)+1
                    return 200,data['body']
            except (ValueError,TypeError):pass
        return code,body
    def patroni(self):
        return self._local_patroni('patroni')
    def patroni_cluster(self):
        return self._local_patroni('cluster')
    def pg_legacy_role(self):
        pg=self.cname('postgres'); p=run(['docker','exec',pg,'sh','-lc','psql -X -U "$POSTGRES_USER" -d postgres -Atc "select pg_is_in_recovery()"'],10)
        if p.returncode:return 'unknown',False
        v=p.stdout.strip();return ('standby' if v=='t' else 'primary'),v in {'t','f'}
    def _leader_hysteresis(self,leader):
        if not self._baseline:
            if leader:self.last_seen_leader=leader;self._baseline=True
            return
        if leader==self.last_seen_leader:self._leader_candidate='';self._leader_candidate_count=0;return
        if leader==self._leader_candidate:self._leader_candidate_count+=1
        else:self._leader_candidate=leader;self._leader_candidate_count=1
        required=2 if leader else 3
        if self._leader_candidate_count>=required:
            prev=self.last_seen_leader;self.last_seen_leader=leader;self._leader_candidate='';self._leader_candidate_count=0
            if prev and leader:
                title='Сменился Primary кластера';message=f'Primary: {prev} -> {leader}'
            elif leader:
                title='Primary кластера определён';message=f'Primary: {leader}.'
            else:
                title='Primary кластера не определён';message=f'Последний подтверждённый Primary: {prev or "не определён"}.'
            self._event('cluster.leader_changed',title,message,'warning' if not leader else 'success')
            if prev and leader==self.node_id and prev!=leader:
                self._probe_previous_primary_async(prev)
    def _probe_previous_primary_async(self,previous):
        # The previous Primary can take its observability-api down with it, so a
        # new Primary performs one independent Agent liveness probe. This closes
        # the most visible alerting gap without turning the Agent into a second
        # monitoring system. The probe is bounded and runs outside tick().
        term=f"{previous}>{self.node_id}|{self._primary_timeline or 'unknown'}"
        if self._notice_state.get('previous_primary_unreachable_term')==term:
            return
        threading.Thread(target=self._probe_previous_primary,args=(previous,term),name='previous-primary-probe',daemon=True).start()
    def _probe_previous_primary(self,previous,term):
        try:
            node=next(n for n in self.cfg['nodes'] if str(n['id'])==str(previous))
            url=f"http://{node['wireguard']['ip']}:{int(node.get('health_port',9187))}/ha/live"
        except Exception:
            return
        evidence=Evidence(minimum_samples=3, minimum_seconds=30)
        started=time.monotonic(); samples=[]
        stop=getattr(self, 'stop', threading.Event())
        patroni_url=f"http://{node['wireguard']['ip']}:{int(node.get('postgres',{}).get('patroni_rest_port',8008))}/patroni"
        while not stop.is_set():
            code,body=http_json(url,4)
            if code==200 and body.get('ok') is True and str(body.get('node'))==str(previous):
                return
            pcode,pbody=http_json(patroni_url,4)
            samples.append({'agent_http':code,'patroni_http':pcode,'at':utc_now()})
            if pcode==200:
                # Agent liveness alone cannot establish that the node is down.
                return
            confirmed,_=evidence.update(False,time.monotonic())
            if confirmed:
                break
            if time.monotonic()-started>70 or stop.wait(5):
                return
        if stop.is_set():return
        with self.lock:
            if self._notice_state.get('previous_primary_unreachable_term')==term:return
            if getattr(self, 'last_seen_leader', self.node_id)!=self.node_id:return
            self._event('cluster.previous_primary_unreachable','Связь с предыдущим Primary не подтверждена',
                        f'Observer={self.node_id}; previous={previous}; repeated Agent AND Patroni probes failed; '
                        f'samples={len(samples)}; elapsed={time.monotonic()-started:.1f}s. '
                        'Host failure is not proven; network isolation is also possible.', 'error', node=previous)
            self._notice_state['previous_primary_unreachable_term']=term
            self._notice_state['previous_primary_unreachable_at']=utc_now()
            self._notice_state['previous_primary_probe_evidence']=samples[-10:]
            self._save_notice_state()

    def shared_state(self,action,check=True):
        p=run(['bash',str(ROOT/'cluster/ops/quorum/shared-state.sh'),action],120)
        if p.returncode and check:raise RuntimeError((p.stderr or p.stdout or 'shared-state failed')[-1000:])
        return p.returncode==0
    def app_start_allowed(self,service):
        if CONTROL_FENCE_FILE.is_file() or MIGRATION_FENCE_FILE.is_file() or (RUNTIME/'application-updates-paused').exists() or not self.quorum_enabled:return False
        code,member=self.patroni()
        if code!=200 or member.get('state')!='running':return False
        role=str(member.get('role','')).lower()
        if role in {'primary','master','leader'}:
            code,cluster=self.patroni_cluster()
            leaders=[str(m.get('name')) for m in cluster.get('members',[]) if str(m.get('role','')).lower()=='leader']
            if code!=200 or leaders!=[self.node_id] or not self.node.get('app',{}).get('can_be_primary',True):return False
            lock_http,_=self._local_patroni('primary')
            return lock_http==200
        # Explicit assist profiles may use a healthy remote Primary; standby
        # workers/bots are never silently revived by an image download.
        return bool(self.assist_enabled and service in self.assist and self.last_seen_leader and self.last_seen_leader!=self.node_id and self._peer_traffic_ready(self.last_seen_leader))
    def app_stop_required(self,service):
        """Fresh stop evidence, distinct from lack of permission to START.

        A REST timeout must not turn a successful start into an immediate stop.
        Confirmed replica/conflict still fences writers without a grace delay.
        """
        if not self.quorum_enabled:return True
        if CONTROL_FENCE_FILE.is_file() or MIGRATION_FENCE_FILE.is_file():return False
        code,member=self.patroni()
        if code==200:
            role=str(member.get('role','')).lower()
            if role in {'primary','master','leader'}:
                if not self.node.get('app',{}).get('can_be_primary',True):return True
                ccode,cluster=self.patroni_cluster()
                leaders=[str(m.get('name')) for m in cluster.get('members',[]) if str(m.get('role','')).lower()=='leader']
                return ccode==200 and bool(leaders) and leaders!=[self.node_id]
            if role in {'replica','standby','slave'}:
                if self.assist_enabled and service in self.assist:
                    return not self.app_start_allowed(service)
                return True
        since=getattr(self,'_patroni_unknown_since',None)
        return (since is not None and time.monotonic()-since>=30 and
                getattr(self,'_patroni_unknown_count',0)>=3)

    def background_updates_allowed(self):
        if (getattr(self,'telemetry',{}).get('ha') or {}).get('role') not in {'primary','standby'}:return False
        health=getattr(self,'ha_health',None)
        return health is None or health.snapshot().get('quorum_observed') is not False

    def app_recovery_allowed(self,service):
        # Do not kill a running service for dependency failure or a slow HA probe.
        if not self.app_start_allowed(service):return False
        health=getattr(self,'ha_health',None)
        if health is not None:
            snapshot=health.snapshot()
            if snapshot.get('quorum_observed') is False:return False
        now=time.monotonic()
        if now-getattr(self,'_recovery_infra_at',0)>5:
            self._recovery_infra_ok=all(self._infra_status().values())
            self._recovery_infra_at=now
        return bool(getattr(self,'_recovery_infra_ok',False))

    def stop_service_set(self,services):
        if SQL_SERVICES.intersection(services):self._sql_request(False)
        # Desired state is published once at the end of tick(). Slow Docker work
        # belongs to the independent lifecycle worker, never to HA/edge sampling.
    def stop_apps(self):
        self.stop_service_set(self.assigned)
    def start_service_set(self,services,force_reconcile=False):
        if SQL_SERVICES.intersection(services):self._sql_request(True,force_reconcile)
    def start_apps(self):
        self.start_service_set(self.assigned)
    def app_status(self):
        rows=[];prepared=0;running=0;ready=0
        states=self.inspect_health_many([self.cname(s) for s in self.assigned])
        for s in self.assigned:
            st=dict(states[self.cname(s)]); st['service']=s;rows.append(st)
            if st['status']!='missing':prepared+=1
            if st['status']=='running':running+=1
            if st['status']=='running' and st['health'] in {'healthy','none'}:ready+=1
        return rows,prepared,running,ready
    def host_snapshot(self):
        now=time.monotonic(); cached=getattr(self,'_host_snapshot_cache',None); at=getattr(self,'_host_snapshot_cache_at',0.0)
        if cached is not None and now-at<15.0:return cached
        host={'status':'ok'}
        try:
            up=Path('/proc/uptime').read_text().split()[0];host['uptime_seconds']=int(float(up))
        except Exception: host['uptime_seconds']=None
        try:
            st=os.statvfs('/');host['disk_root_total_bytes']=st.f_blocks*st.f_frsize;host['disk_root_free_bytes']=st.f_bavail*st.f_frsize
        except Exception: pass
        fw=run(['ufw','status'],5);firewall={'status':'active' if fw.returncode==0 and 'Status: active' in fw.stdout else 'unknown'}
        wg=run(['wg','show','wg-taskforge','dump'],5);peers=[]
        if wg.returncode==0:
            for line in wg.stdout.splitlines()[1:]:
                x=line.split('\t')
                if len(x)>=6: peers.append({'public_key':x[0][:12]+'...', 'endpoint':x[2], 'allowed_ips':x[3], 'latest_handshake_epoch':int(x[4] or 0)})
        value=(host,firewall,{'interface':'wg-taskforge','peers':peers})
        self._host_snapshot_cache=value; self._host_snapshot_cache_at=now
        return value

    def _record_ha_decision(self, evidence):
        identity=(evidence.get('role'),evidence.get('postgres_state'),evidence.get('timeline'),
                  evidence.get('observed_leader'),evidence.get('policy'),evidence.get('reason'))
        if identity==getattr(self,'_last_ha_decision',None):return
        try:
            RUNTIME.mkdir(parents=True,exist_ok=True)
            path=RUNTIME/'ha-decisions.jsonl'
            if path.exists() and path.stat().st_size>2*1024*1024:
                os.replace(path,RUNTIME/'ha-decisions.jsonl.1')
            with path.open('a') as handle:
                os.chmod(path,0o600)
                handle.write(json.dumps({'at':utc_now(),'node':self.node_id,**evidence},separators=(',',':'))+'\n')
            self._last_ha_decision=identity
        except OSError:
            # Evidence collection must never become a new election/fencing input.
            pass

    def tick(self):
        enabled=self.quorum_enabled
        role='unknown'; observed_leader=''; pghealthy=False; member={}
        patroni_http=0; cluster_http=0
        if enabled:
            patroni_http,p=self.patroni(); cluster_http,c=self.patroni_cluster()
            if patroni_http==200:
                self._primary_timeline=str(p.get('timeline') or '')
                pr=str(p.get('role','')).lower()
                if pr in {'primary','master','leader'}:
                    role='primary'
                elif pr in {'replica','standby','slave'}:
                    role='standby'
                pghealthy=str(p.get('state','')).lower()=='running' and role in {'primary','standby'}
                member=p
            if cluster_http==200:
                for m in c.get('members',[]):
                    if str(m.get('role','')).lower()=='leader':
                        observed_leader=str(m.get('name',''))
                        break
        else:
            role,pghealthy=self.pg_legacy_role(); observed_leader=self.node_id if role=='primary' else ''

        # Debounce only absence/change notifications. Positive conflicting leader
        # evidence is still fenced immediately below.
        if enabled and role=='primary' and pghealthy and not observed_leader and self.last_seen_leader==self.node_id:
            # Missing cluster summary does not erase a still-confirmed local role.
            self._leader_candidate='';self._leader_candidate_count=0
        else:
            self._leader_hysteresis(observed_leader)
        leader=self.last_seen_leader if self._baseline else observed_leader
        if enabled and role=='unknown':
            self._patroni_unknown_count+=1
            if getattr(self,'_patroni_unknown_since',None) is None:self._patroni_unknown_since=time.monotonic()
        else:
            self._patroni_unknown_count=0
            self._patroni_unknown_since=None
        unknown_confirmed=(self._patroni_unknown_count>=3 and
                           self._patroni_unknown_since is not None and
                           time.monotonic()-self._patroni_unknown_since>=30)

        rows,prepared,running,ready=self.app_status()
        cprepared,crunning,cready=self._app_counts(rows,self.critical)
        oprepared,orunning,oready=self._app_counts(rows,self.optional)
        degraded=[]
        infra=self._infra_status(); infra_ready=all(infra.values())
        tls_assets=self._tls_assets_ready(); tls_live=False
        blockers=[]; traffic=False; active=False; app_mode='warm-standby'
        control_fenced=CONTROL_FENCE_FILE.is_file()
        edge_allowed=False
        # Missing /cluster data is not a positive leader conflict. Preserve
        # an already-proven public edge while local Patroni still reports
        # Primary; a real demotion or conflicting DCS leader revokes it now.
        if (not enabled or role!='primary' or control_fenced or
                (observed_leader and observed_leader!=self.node_id) or
                (not observed_leader and leader!=self.node_id)):
            self._edge_refresh(False)

        # During a controlled release re-home the migration shell is the only
        # Docker-state writer. The r55 Agent may be started for liveness and
        # telemetry, but while this durable fence exists it must not call any
        # application/Watchtower/storage mutation path.
        if control_fenced:
            self.shared_ok=False
            blockers.append('control-plane-rehome-fenced')
            app_mode='migration-fenced'
            active=False
            traffic=False

        elif enabled and role=='primary':
            leader_conflict=bool(observed_leader and observed_leader!=self.node_id)
            leader_confirmed=(not leader_conflict and leader==self.node_id)
            forbidden_primary=not self.node.get('app',{}).get('can_be_primary',True)
            if forbidden_primary:
                blockers.append('node-nofailover')
            if not pghealthy:
                blockers.append('postgres-not-ready')
            if not infra_ready:
                blockers.append('infra-not-ready:'+','.join(k for k,v in infra.items() if not v))
            if leader_conflict:
                blockers.append(f'leader-conflict:{observed_leader}')
            elif not leader_confirmed:
                blockers.append('leader-not-confirmed')

            if forbidden_primary or leader_conflict:
                # A forbidden primary or positive DCS conflict is stronger
                # than a transient REST miss: immediately fence applications.
                self.shared_ok=False
                self.stop_apps()
                rows,prepared,running,ready=self.app_status(); cprepared,crunning,cready=self._app_counts(rows,self.critical); degraded=[]
                app_mode='fenced-forbidden-primary' if forbidden_primary else 'fenced-leader-conflict'
            elif not blockers:
                try:
                    if not self.shared_ok:
                        self.shared_ok=self.shared_state('pull',True)
                    routing_changed=self._write_local_routing()
                    # Traffic-critical apps and optional features have separate activation
                    # contracts. An optional analyzer failure must never poison the site.
                    app_state_changed=False
                    if routing_changed or crunning!=len(self.critical):
                        self.start_service_set(self.critical,force_reconcile=(routing_changed and crunning!=len(self.critical)))
                        app_state_changed=True
                    if self.optional and (routing_changed or orunning!=len(self.optional)):
                        try:
                            self.start_service_set(self.optional,force_reconcile=routing_changed)
                            app_state_changed=True
                        except Exception as e:
                            self._event('ha.optional_activation_failed','Optional service activation failed',str(e),'warning')
                    if app_state_changed:
                        rows,prepared,running,ready=self.app_status(); cprepared,crunning,cready=self._app_counts(rows,self.critical); oprepared,orunning,oready=self._app_counts(rows,self.optional)
                    degraded=self._degraded_services(rows,self.optional)
                    # The edge worker never blocks this reconciliation loop.
                    # A successful DNS API write is not a public-route proof.
                    edge_allowed=(cready==len(self.critical) and leader_confirmed)
                    if not self._edge_refresh(edge_allowed):
                        blockers.append('edge-pending:'+str(self.cloudflare_last_result.get('phase','starting')))
                    tls_assets=self._tls_assets_ready()
                except Exception as e:
                    blockers.append('shared-state-or-activation-failed')
                    self._event('ha.activation_failed','HA activation failed',str(e),'error')
            # Optional services may degrade without taking the public site down.
            # Only traffic-critical services participate in edge readiness.
            if cready!=len(self.critical):
                blockers.append(f'core-apps-not-ready:{cready}/{len(self.critical)}')
            tls_live=self._tls_live_ready()
            if not tls_live:
                blockers.append('tls-live-not-ready')
            active=crunning==len(self.critical)
            if forbidden_primary or leader_conflict:
                active=False
            app_mode='primary' if active and not blockers else ('primary-unconfirmed' if active else app_mode)
            traffic=(not blockers and cready==len(self.critical) and leader_confirmed and tls_live and self.edge_controller.snapshot().get('success',False))
            if traffic and time.time()-self.last_shared_push>=int(self.cfg.get('shared_state_interval_seconds',60)):
                if self.shared_state('push',False):
                    self.last_shared_push=time.time()
        elif enabled and role=='standby':
            self.shared_ok=False
            # Cancel an old term immediately; its proof is never reusable.
            self._edge_refresh(False)
            # An optional lite assist profile may use the already-ready remote
            # primary for DB/RabbitMQ/Redis while keeping local replicated MinIO.
            if self.assist_enabled and leader and leader!=self.node_id and self._peer_traffic_ready(leader):
                try:
                    routing_changed=self._write_remote_routing(leader)
                    nonassist=[x for x in self.assigned if x not in set(self.assist)]
                    self.stop_service_set(nonassist)
                    self.start_service_set(self.assist,force_reconcile=routing_changed)
                    rows,prepared,running,ready=self.app_status(); cprepared,crunning,cready=self._app_counts(rows,self.critical); degraded=self._degraded_services(rows,self.assist)
                    assist_states=self.inspect_health_many([self.cname(x) for x in self.assist])
                    active=all(assist_states[self.cname(x)]['status']=='running' for x in self.assist)
                    app_mode='assist'
                except Exception as e:
                    blockers.append('assist-activation-failed')
                    self._event('ha.assist_failed','Assist activation failed',str(e),'error')
            else:
                # An explicit Patroni standby role is authoritative. Once all
                # application containers are already stopped, do not keep asking
                # Docker to stop/inspect the same set every five seconds.
                self._write_standby_routing()
                if running:
                    self.stop_apps()
                    rows,prepared,running,ready=self.app_status(); cprepared,crunning,cready=self._app_counts(rows,self.critical); oprepared,orunning,oready=self._app_counts(rows,self.optional)
                else:
                    self._sql_request(False)
                degraded=[]; app_mode='warm-standby'
            if not pghealthy:
                blockers.append('postgres-not-ready')

        elif enabled:
            # Patroni REST uncertainty is not the same thing as an explicit
            # demotion. Remove public readiness immediately, but tolerate two
            # missed 5-second samples before stopping already-running apps.
            self.shared_ok=False
            blockers.append('patroni-role-unknown')
            if patroni_http!=200:
                blockers.append(f'patroni-rest-http:{patroni_http or 0}')
            if unknown_confirmed:
                self._write_standby_routing()
                if running:
                    self.stop_apps()
                    rows,prepared,running,ready=self.app_status(); cprepared,crunning,cready=self._app_counts(rows,self.critical); oprepared,orunning,oready=self._app_counts(rows,self.optional)
                else:
                    self._sql_request(False)
                degraded=[]; app_mode='fenced-patroni-unknown'
            else:
                active=running>0
                app_mode='uncertain-grace' if active else 'warm-standby'

        if not edge_allowed:
            self._edge_refresh(False)

        # Hot standby readiness is a failover contract for the traffic-critical
        # site, not a requirement that every optional feature is healthy/running.
        hot_blockers=[]
        if cprepared!=len(self.critical): hot_blockers.append(f'core-apps-unprepared:{cprepared}/{len(self.critical)}')
        if not pghealthy: hot_blockers.append('postgres-not-ready')
        if not infra_ready: hot_blockers.append('infra-not-ready:'+','.join(k for k,v in infra.items() if not v))
        hot=not hot_blockers
        failover_https_ready=hot and tls_assets
        tls_ready=tls_live if role=='primary' else tls_assets
        rd=RUNTIME/'readiness';rd.mkdir(parents=True,exist_ok=True)
        marker=rd/'traffic-ready'
        if traffic:
            marker.write_text(f'node={self.node_id} at={utc_now()}\n');os.chmod(marker,0o644);chown_to_deployment_owner(marker)
        else:
            try:marker.unlink()
            except FileNotFoundError:pass
        update_policy, update_reason = application_update_policy(
            enabled, role, observed_leader, leader, self.node_id,
            self.node.get('app',{}).get('can_be_primary', True),
            app_mode, self._patroni_unknown_count if unknown_confirmed else min(self._patroni_unknown_count,2))
        self._record_ha_decision({'role':role,'postgres_state':member.get('state'),
                                  'timeline':member.get('timeline'),'observed_leader':observed_leader,
                                  'dcs_last_seen':member.get('dcs_last_seen'),
                                  'postmaster_start_time':member.get('postmaster_start_time'),
                                  'patroni_http':patroni_http,'cluster_http':cluster_http,
                                  'policy':update_policy,'reason':update_reason})
        # Diagnostic evidence for intermittent /cluster REST failures, without
        # generating a Telegram warning on every five-second HA sample.
        if update_policy == 'hold' and role == 'primary':
            self._primary_cluster_hold_samples = getattr(self, '_primary_cluster_hold_samples', 0) + 1
            self._primary_cluster_hold_total = getattr(self, '_primary_cluster_hold_total', 0) + 1
        else:
            self._primary_cluster_hold_samples = 0
        if hasattr(self, 'updater') and not control_fenced:
            # A timeout/empty leader from /cluster is NOT a replica transition.
            # In the HOLD state, preserve the previous desired service set and
            # routing generation. Do not let the updater stage/stop Primary apps.
            if update_policy != 'hold':
                desired = (self.assigned if update_policy == 'primary' else
                           self.assist if update_policy == 'assist' else [])
                routing = ((RUNTIME/'app-routing.env').read_bytes()
                           if (RUNTIME/'app-routing.env').is_file() else b'')
                self.updater.set_desired(desired, hashlib.sha256(routing).hexdigest(), reason=update_reason)
                self._sql_request(bool(SQL_SERVICES.intersection(desired)))
        host,firewall,wireguard=self.host_snapshot()
        if hasattr(self,'metrics'):host={**host,**self.metrics.snapshot()}
        container_rows=self.docker_inventory(app_mode)
        container_by_service={x.get('service'):x for x in container_rows}
        service_rows=[]
        for item in rows:
            svc=item['service']; inv=container_by_service.get(svc,{})
            service_rows.append(dict(item,assigned=True,desired_state=self._container_expected_state(svc,app_mode),container_state=item['status'],image_fingerprint=inv.get('image_fingerprint',''),image_available=item['status']!='missing'))
        self.telemetry={'schema_version':1,'generated_at':utc_now(),'cluster':self.cfg.get('cluster_name','taskforge-prod'),
          'node':{**self._skeleton()['node'],'deployment_mode':'quorum' if enabled else 'replica'},'topology':self.cfg.get('nodes',[]),
          'ha':{'reconciled':True,'role':role,'leader':leader,'app_mode':app_mode,'applications_active':active,'traffic_ready':traffic,'hot_start_ready':hot,'hot_start_blockers':hot_blockers,'traffic_blockers':blockers,'tls_ready':tls_ready,'tls_assets_ready':tls_assets,'tls_live_ready':tls_live,'failover_https_ready':failover_https_ready,'degraded_services':degraded,'last_error':'','patroni_unknown_samples':self._patroni_unknown_count,'control_plane_fenced':control_fenced,
                  'patroni_local_http':patroni_http,'patroni_cluster_http':cluster_http,
                  'patroni_unknown_seconds':round(time.monotonic()-self._patroni_unknown_since,1) if self._patroni_unknown_since is not None else 0,
                  'unknown_confirmation_seconds':30,
                  'patroni_loopback_recoveries':getattr(self,'_patroni_loopback_recoveries',0),
                  'app_desired_policy':update_policy,'app_desired_reason':update_reason,
                  'primary_cluster_hold_samples':self._primary_cluster_hold_samples,
                  'primary_cluster_hold_total':getattr(self, '_primary_cluster_hold_total', 0)},
          'host':host,'firewall':firewall,'wireguard':wireguard,
          'ha_health':self.ha_health.snapshot() if hasattr(self,'ha_health') else {},
          'postgres':{'healthy':pghealthy,'actual_role':role,'role':role,'leader':leader,'member':member,'receiver':'','source':'','slot':'','replay_gap_bytes':None,'replicas':[]},
          'minio':{'ready':infra.get('minio',False)},
          'edge':{'provider':'cloudflare-dns','configured':self._cloudflare_configured(),'state':self.cloudflare_last_result,'last_success_epoch':int(self.cloudflare_last_success or 0)},
          'docker':{'containers':container_rows,'services':service_rows,'container_count':len(container_rows),'assigned_app_count':len(self.assigned),'critical_app_count':len(self.critical),'optional_app_count':len(self.optional),'prepared_app_count':prepared,'critical_prepared_app_count':cprepared,'running_app_count':running,'critical_running_app_count':crunning,'images_ready':prepared,'images_missing':len(self.assigned)-prepared},
          'update':self.updater.snapshot() if hasattr(self,'updater') else {'mode':'node-agent','running':False},'events':self.events[-80:]}
        self._write_state()
    def loop(self):
        while not self.stop.is_set():
            try:
                with self.lock:self.tick()
            except Exception as e:
                self._event('agent.tick_error','Node Agent tick failed',repr(e),'error')
                try:self._write_state()
                except Exception:pass
            self.stop.wait(5)


class ThreadingUnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads=True
    allow_reuse_address=True

class Handler(BaseHTTPRequestHandler):
    agent:Agent=None
    def log_message(self,fmt,*args):return
    def _send(self,code,obj):
        b=(json.dumps(obj,ensure_ascii=False,separators=(',',':'))+'\n').encode();self.send_response(code);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(b)));self.send_header('Cache-Control','no-store');self.end_headers()
        try:self.wfile.write(b)
        except BrokenPipeError:pass
    def _control_auth(self,expected):
        supplied=self.headers.get('X-TaskForge-Cluster-Token','')
        return bool(expected) and hmac.compare_digest(expected,supplied)
    def _diag_auth(self):
        return self._control_auth(self.agent.diagnostics_token)
    def _maintenance_auth(self):
        return self._control_auth(self.agent.maintenance_token)
    def _diag_error(self,e):
        return self._send(e.status,{'status':e.status,'code':e.code,'message':e.message})
    def _diag_parts(self):
        path=self.path.split('?',1)[0].rstrip('/')
        return [x for x in path.split('/') if x]
    def _send_archive(self,path):
        size=path.stat().st_size
        self.send_response(200);self.send_header('Content-Type','application/gzip');self.send_header('Content-Length',str(size));self.send_header('Content-Disposition',f'attachment; filename="{path.name}"');self.send_header('Cache-Control','no-store');self.end_headers()
        try:
            with path.open('rb') as f:
                while True:
                    chunk=f.read(1024*1024)
                    if not chunk:break
                    self.wfile.write(chunk)
        except (BrokenPipeError,ConnectionResetError):pass
    def do_POST(self):
        a=self.agent;parts=self._diag_parts()
        if parts==['ha','diagnostics']:
            if not self._diag_auth():return self._send(401,{'status':401,'code':'DIAGNOSTICS_UNAUTHORIZED','message':'unauthorized'})
            try:
                length=int(self.headers.get('Content-Length','0') or 0)
                if length<0 or length>16384:return self._send(413,{'status':413,'code':'DIAGNOSTICS_REQUEST_TOO_LARGE','message':'request body too large'})
                raw=self.rfile.read(length) if length else b'{}'; body=json.loads(raw.decode('utf-8')) if raw else {}
                with getattr(a,'maintenance_lock',nullcontext()):
                    job=a.diagnostics.start(body.get('mode','standard'),body.get('since','6h'),body.get('max_log_mb',32))
                return self._send(202,job)
            except DiagnosticsError as e:return self._diag_error(e)
            except Exception as e:return self._send(400,{'status':400,'code':'DIAGNOSTICS_REQUEST_INVALID','message':str(e)[:400]})
        if parts==['ha','logs','cleanup']:
            if not self._maintenance_auth():return self._send(401,{'status':401,'code':'LOG_CLEANUP_UNAUTHORIZED','message':'unauthorized'})
            try:
                length=int(self.headers.get('Content-Length','0') or 0)
                if length<0 or length>4096:return self._send(413,{'status':413,'code':'LOG_CLEANUP_REQUEST_TOO_LARGE','message':'request body too large'})
                if length:self.rfile.read(length)
                with getattr(a,'maintenance_lock',nullcontext()):
                    if a.diagnostics.is_busy():
                        raise LogCleanupError(409,'LOG_CLEANUP_DIAGNOSTICS_BUSY','diagnostics collection is running on this node')
                    result=a.log_cleanup.cleanup()
                result['node']=a.node_id
                print(f"[TFLOG CLEANUP] node={a.node_id} reclaimed={result.get('reclaimed_bytes',0)} free_delta={result.get('filesystem_free_delta_bytes',0)}",flush=True)
                return self._send(200,result)
            except LogCleanupError as e:return self._send(e.status,{'status':e.status,'code':e.code,'message':e.message})
            except Exception as e:return self._send(500,{'status':500,'code':'LOG_CLEANUP_FAILED','message':str(e)[:400]})
        return self._send(404,{'error':'not-found'})
    def do_GET(self):
        a=self.agent;parts=self._diag_parts()
        if len(parts)>=2 and parts[:2]==['ha','diagnostics']:
            if not self._diag_auth():return self._send(401,{'status':401,'code':'DIAGNOSTICS_UNAUTHORIZED','message':'unauthorized'})
            try:
                if len(parts)==3:return self._send(200,a.diagnostics.status(parts[2]))
                if len(parts)==4 and parts[3]=='archive':return self._send_archive(a.diagnostics.archive(parts[2]))
                return self._send(404,{'error':'not-found'})
            except DiagnosticsError as e:return self._diag_error(e)
        if self.path=='/ha/live':return self._send(200,{'ok':True,'node':a.node_id,'revision':int(a.bundle_revision),'reconciled':bool((a.telemetry.get('ha') or {}).get('reconciled',False))})
        # Read the last complete snapshot without waiting for the reconciliation lock.
        # tick() replaces self.telemetry as one object at completion, so HTTP readers
        # never need to wait behind Docker/PostgreSQL collection.
        t=json.loads(json.dumps(a.telemetry))
        if hasattr(a,'metrics'):t['host']={**t.get('host',{}),**a.metrics.snapshot()}
        if hasattr(a,'updater'):t['update']=a.updater.snapshot()
        # Publish worker progress even between reconciliation snapshots. A worker
        # failure immediately revokes HTTP readiness rather than waiting on tick().
        edge_state=a.edge_controller.snapshot()
        t.setdefault('edge',{})['state']=edge_state
        t['edge']['configured']=a._cloudflare_configured()
        if not edge_state.get('success'):
            t.setdefault('ha',{})['traffic_ready']=False
        if self.path=='/ha/telemetry':return self._send(200,t)
        if self.path=='/ha/status':return self._send(200,t.get('ha',{}))
        if self.path=='/ha/traffic-ready':return self._send(200 if t.get('ha',{}).get('traffic_ready') else 503,{'ready':bool(t.get('ha',{}).get('traffic_ready')),'node':a.node_id})
        return self._send(404,{'error':'not-found'})

def main():
    ap=argparse.ArgumentParser();ap.add_argument('--config',default=str(CONFIG_ROOT/'cluster.json'));ap.add_argument('--env-file',default=str(CONFIG_ROOT/'taskforge.env'));a=ap.parse_args()
    ag=Agent(Path(a.config),Path(a.env_file));Handler.agent=ag
    ip=ag.node['wireguard']['ip'];port=int(ag.node.get('health_port',9187))
    unix_path=RUNTIME/'node-agent.sock'
    try: unix_path.unlink(missing_ok=True)
    except OSError: pass
    th=threading.Thread(target=ag.loop,daemon=True);th.start()
    srv=ThreadingHTTPServer((ip,port),Handler)
    local_srv=None; local_th=None
    try:
        local_srv=ThreadingUnixHTTPServer(str(unix_path),Handler)
        os.chmod(unix_path,0o600)
        local_th=threading.Thread(target=local_srv.serve_forever,name='node-agent-unix-http',daemon=True)
        local_th.start()
        print(f'[TFCLUSTER LOCAL CONTROL] socket={unix_path} mode=0600',flush=True)
        srv.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        ag.stop.set();ag.edge_controller.close()
        if local_srv is not None:
            try: local_srv.shutdown()
            except Exception: pass
            local_srv.server_close()
        srv.server_close()
        try: unix_path.unlink(missing_ok=True)
        except OSError: pass
if __name__=='__main__':main()
