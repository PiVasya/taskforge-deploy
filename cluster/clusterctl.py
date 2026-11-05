#!/usr/bin/env python3
from __future__ import annotations
import argparse, hashlib, hmac, json, os, shlex, sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
RUNTIME=Path(os.environ.get('TASKFORGE_CLUSTER_RUNTIME','/var/lib/taskforge/cluster'))
CONFIG_ROOT=Path(os.environ.get('TASKFORGE_CONFIG_ROOT','/etc/taskforge'))

def die(msg:str)->None: raise SystemExit(f'error: {msg}')

def load_env(path:Path)->dict[str,str]:
    out={}
    if not path.is_file(): return out
    for raw in path.read_text(encoding='utf-8-sig').splitlines():
        s=raw.strip()
        if not s or s.startswith('#') or '=' not in raw: continue
        k,v=raw.split('=',1); k=k.strip(); v=v.strip()
        if len(v)>=2 and v[:1]==v[-1:] and v[0] in "\"'": v=v[1:-1]
        out[k]=v
    return out

def q(v:Any)->str: return shlex.quote(str(v))

def chown_to_deployment_owner(path:Path)->None:
    # Mutating cluster operations run under sudo/root, but the bundle is operated
    # by its owning account. Keep 0600 generated state readable by that account.
    if os.geteuid()!=0:
        return
    try:
        import pwd, grp
        user=(CONFIG_ROOT/'operator-user').read_text().strip()
        group=(CONFIG_ROOT/'operator-group').read_text().strip()
        os.chown(path,pwd.getpwnam(user).pw_uid,grp.getgrnam(group).gr_gid)
    except Exception:
        pass

def write_atomic(path:Path,text:str,mode:int=0o600)->None:
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_name(path.name+f'.tmp.{os.getpid()}')
    tmp.write_text(text,encoding='utf-8')
    os.chmod(tmp,mode)
    chown_to_deployment_owner(tmp)
    os.replace(tmp,path)

@dataclass
class Node:
    raw:dict[str,Any]
    @property
    def id(self): return str(self.raw['id'])
    @property
    def priority(self): return int(self.raw.get('priority',0))
    @property
    def wg_ip(self): return str(self.raw.get('wireguard',{}).get('ip',''))
    @property
    def can_be_primary(self): return bool(self.raw.get('app',{}).get('can_be_primary',True))
    @property
    def profile(self): return str(self.raw.get('app',{}).get('profile','full'))
    @property
    def dcs_voter(self): return bool(self.raw.get('dcs_voter',True))
    @property
    def etcd_client_port(self): return int(self.raw.get('etcd',{}).get('client_port',2379))
    @property
    def etcd_peer_port(self): return int(self.raw.get('etcd',{}).get('peer_port',2380))
    @property
    def pg_port(self): return int(self.raw.get('postgres',{}).get('host_port',5432))
    @property
    def rest_port(self): return int(self.raw.get('postgres',{}).get('patroni_rest_port',8008))
    @property
    def health_port(self): return int(self.raw.get('health_port',9187))

class Cluster:
    def __init__(self,raw): self.raw=raw; self.nodes=[Node(x) for x in raw.get('nodes',[])]
    def node(self,nid):
        for n in self.nodes:
            if n.id==nid:return n
        die(f'unknown node {nid}')
    @property
    def voters(self): return [n for n in self.nodes if n.dcs_voter]

def load_cluster(path:Path)->Cluster:
    try: raw=json.loads(path.read_text(encoding='utf-8'))
    except Exception as e: die(f'cannot parse {path}: {e}')
    if raw.get('schema_version')!=1: die('cluster schema_version must be 1')
    ids=[str(n.get('id','')) for n in raw.get('nodes',[])]
    if not ids or len(ids)!=len(set(ids)): die('cluster node ids missing/duplicate')
    c=Cluster(raw)
    if len(c.voters)<3: die('requires at least 3 DCS voters')
    if not any(n.can_be_primary for n in c.nodes): die('no primary candidates')
    for n in c.nodes:
        if not n.wg_ip: die(f'{n.id}: wireguard ip missing')
    return c

def patroni_password(env:dict[str,str])->str:
    key=env.get('TASKFORGE_INTERNAL_KEY','')
    if len(key)<40: die('TASKFORGE_INTERNAL_KEY missing/short; cannot derive Patroni REST password')
    return hmac.new(key.encode(),b'taskforge-patroni-rest-v2',hashlib.sha512).hexdigest()[:64]

def render_patroni(c:Cluster,node:Node,env:dict[str,str])->dict[str,Any]:
    pguser=env.get('POSTGRES_USER','taskforge'); pgpass=env.get('POSTGRES_PASSWORD','')
    if not pgpass: die('POSTGRES_PASSWORD missing')
    internal=env.get('TASKFORGE_INTERNAL_KEY','')
    if len(internal)<40: die('TASKFORGE_INTERNAL_KEY missing/short')
    # Preserve the existing credentials exactly: these purpose strings are the live
    # cluster's compatibility contract and must not change between releases.
    repl='taskforge_repl'; rewind='taskforge_rewind'
    replpass=hmac.new(internal.encode(),b'taskforge-postgres-replication-v2',hashlib.sha512).hexdigest()[:64]
    rewindpass=hmac.new(internal.encode(),b'taskforge-postgres-rewind-v2',hashlib.sha512).hexdigest()[:64]
    wg=str(c.raw.get('wireguard_cidr','10.80.0.0/24'))
    hosts=[f'etcd:{node.etcd_client_port}']+[f'{v.wg_ip}:{v.etcd_client_port}' for v in c.voters if v.id!=node.id]
    hba=[
      'local all all trust',
      'host all all 127.0.0.1/32 scram-sha-256',
      'host all all ::1/128 scram-sha-256',
      'host all all 10.0.0.0/8 scram-sha-256',
      'host all all 172.16.0.0/12 scram-sha-256',
      'host all all 192.168.0.0/16 scram-sha-256',
      'host all all fd00::/8 scram-sha-256',
      f'host replication {repl} {wg} scram-sha-256',
      f'host all {rewind} {wg} scram-sha-256',
    ]
    return {
      'scope':str(c.raw.get('cluster_name','taskforge-prod')),
      'namespace':'/taskforge/patroni/', 'name':node.id,
      'tags':{'noloadbalance':not node.can_be_primary,'clonefrom':False,'nosync':True,'failover_priority':node.priority if node.can_be_primary else 0},
      'restapi':{
        'listen':'0.0.0.0:8008', 'connect_address':f'{node.wg_ip}:{node.rest_port}',
        'authentication':{'username':'taskforge_patroni','password':patroni_password(env)}
      },
      # Stable-runtime cluster contract: local etcd is Docker-local, remotes stay on WireGuard.
      # use_proxies prevents discovery from replacing this with advertised URLs
      # and reintroducing the self-WG published-port hairpin.
      'etcd3':{'hosts':hosts,'use_proxies':True},
      'bootstrap':{
        'dcs':{
          'ttl':120,'loop_wait':10,'retry_timeout':45,
          'maximum_lag_on_failover':int(c.raw.get('maximum_lag_on_failover_bytes',67108864)),
          'check_timeline':True,'failsafe_mode':True,
          'postgresql':{
            'use_pg_rewind':True,'use_slots':True,
            'remove_data_directory_on_rewind_failure':False,
            'remove_data_directory_on_diverged_timelines':False,
            'parameters':{
              'wal_level':'replica','hot_standby':'on',
              'max_wal_senders':max(20,len(c.nodes)*4),'max_replication_slots':max(20,len(c.nodes)*4),
              'wal_keep_size':'4096MB','synchronous_commit':'on','synchronous_standby_names':'',
              'password_encryption':'scram-sha-256','max_connections':300,
            }
          }
        },
        'initdb':[{'encoding':'UTF8'},'data-checksums'],
        'pg_hba':hba,
      },
      'postgresql':{
        'listen':'0.0.0.0:5432','connect_address':f'{node.wg_ip}:{node.pg_port}',
        'data_dir':'/var/lib/postgresql/18/docker','bin_dir':'/usr/lib/postgresql/18/bin','pgpass':'/var/lib/postgresql/.pgpass',
        'use_pg_rewind':True,
        'remove_data_directory_on_rewind_failure':False,
        'remove_data_directory_on_diverged_timelines':False,
        'authentication':{
          'superuser':{'username':pguser,'password':pgpass},
          'replication':{'username':repl,'password':replpass},
          'rewind':{'username':rewind,'password':rewindpass},
        },
        'create_replica_methods':['basebackup'],
        'basebackup':{'max-rate':'200M','checkpoint':'fast'},
        'parameters':{'unix_socket_directories':'/var/run/postgresql'},
      },
      'watchdog':{'mode':'off'},
    }

def render(c:Cluster,node:Node,env:dict[str,str])->None:
    RUNTIME.mkdir(parents=True,exist_ok=True)
    write_atomic(RUNTIME/'node-id',node.id+'\n')
    # Merge the previously generated environment if this is an in-place release upgrade.
    # The stable runtime owns the resulting files, but unknown application-routing keys survive
    # so a production-only environment override cannot disappear during migration.
    node_env=load_env(RUNTIME/'node.env')
    etcd_initial=','.join(f'{v.id}=http://{v.wg_ip}:{v.etcd_peer_port}' for v in c.voters)
    etcd_endpoints=','.join(f'http://{v.wg_ip}:{v.etcd_client_port}' for v in c.voters)
    node_env.update({
      'TASKFORGE_NODE_ID':node.id,'TASKFORGE_NODE_IP':node.wg_ip,
      'CLUSTER_NODE_ID':node.id,'CLUSTER_WG_IP':node.wg_ip,
      'CLUSTER_POSTGRES_BIND':node.wg_ip,'CLUSTER_POSTGRES_PORT':node.pg_port,
      'CLUSTER_PATRONI_BIND':node.wg_ip,'CLUSTER_PATRONI_REST_BIND':node.wg_ip,'CLUSTER_PATRONI_REST_PORT':node.rest_port,
      'CLUSTER_ETCD_BIND':node.wg_ip,'CLUSTER_ETCD_CLIENT_PORT':node.etcd_client_port,'CLUSTER_ETCD_PEER_PORT':node.etcd_peer_port,
      'CLUSTER_ETCD_INITIAL_CLUSTER':etcd_initial,'CLUSTER_ETCD_ENDPOINTS':etcd_endpoints,
      'CLUSTER_ETCD_TOKEN':str(c.raw.get('cluster_name','taskforge-prod'))+'-etcd-v1',
      'ETCD_IMAGE':str(c.raw.get('etcd_image','gcr.io/etcd-development/etcd:v3.6.14')),
      'MINIO_BIND':node.wg_ip,'MINIO_PORT':int(node.raw.get('minio',{}).get('api_port',9000)),
      'MINIO_CONSOLE_BIND':node.wg_ip,'MINIO_CONSOLE_PORT':int(node.raw.get('minio',{}).get('console_port',9001)),
      'RABBITMQ_BIND':node.wg_ip,'RABBITMQ_PORT':5672,'REDIS_BIND':node.wg_ip,'REDIS_PORT':6379,
      'CLUSTER_HEALTH_BIND':node.wg_ip,'CLUSTER_HEALTH_PORT':node.health_port,
      'TASKFORGE_CLUSTER_AGENT_BIND':node.wg_ip,'TASKFORGE_CLUSTER_AGENT_PORT':node.health_port,
      'TASKFORGE_CLUSTER_AGENT_URLS':';'.join(f'http://{n.wg_ip}:{n.health_port}' for n in c.nodes),
      'TASKFORGE_BROWSER_CLUSTER_PORT':18080,'TASKFORGE_IMAGE_ANALYZER_CLUSTER_PORT':18090,
      'TASKFORGE_APP_PROFILE':node.profile,
    })
    write_atomic(RUNTIME/'node.env',''.join(f'{k}={q(v)}\n' for k,v in node_env.items()))
    routing=load_env(RUNTIME/'app-routing.env')
    routing.update({
      'DB_HOST':'postgres','DB_PORT':'5432','RABBITMQ_HOST':'rabbitmq','REDIS_HOST':'redis','S3_INTERNAL_ENDPOINT':'http://minio:9000',
      'TASKFORGE_CLUSTER_AGENT_URLS':';'.join(f'http://{n.wg_ip}:{n.health_port}' for n in c.nodes),
    })
    routing.pop('TASKFORGE_ACTIVE_PRIMARY_HINT',None)
    write_atomic(RUNTIME/'app-routing.env',''.join(f'{k}={q(v)}\n' for k,v in routing.items()))
    write_atomic(RUNTIME/'patroni.yml',json.dumps(render_patroni(c,node,env),indent=2,sort_keys=False)+'\n')

def cloudflare(c:Cluster,env:dict[str,str])->dict[str,Any]:
    # r55 uses ordinary Cloudflare DNS records plus the API, not the paid
    # Cloudflare Load Balancing product. Application profile (full/lite) does
    # not decide leadership authority: any can_be_primary node with a public
    # origin may become the active edge target.
    origins=[n for n in sorted(c.nodes,key=lambda x:x.priority,reverse=True)
             if n.can_be_primary and str(n.raw.get('public_host','')).strip()]
    if len(origins)<2: die('need at least two public primary candidates')
    domain=str(env.get('DOMAIN','taskforge.by') or 'taskforge.by').strip()
    explicit=str(env.get('CLOUDFLARE_FAILOVER_HOSTS','') or '').strip()
    raw=explicit.replace(';',',').split(',') if explicit else [domain,str(env.get('CT_DOMAIN','') or '')]
    hosts=[]
    for x in raw:
        x=x.strip().rstrip('.').lower()
        if x and x not in hosts: hosts.append(x)
    return {
      'mode':'cloudflare-dns-api-failover',
      'paid_load_balancing_required':False,
      'public_candidates':[{'node':n.id,'origin':n.raw.get('public_host','')} for n in origins],
      'managed_host_candidates':hosts,
      'zone_name':str(env.get('CLOUDFLARE_ZONE',domain) or domain),
      'zone_id_configured':bool(str(env.get('CLOUDFLARE_ZONE_ID','')).strip()),
      'minimum_token_permission':'Zone DNS Write; add Zone Read only when zone id is not configured',
      'authority':'only the locally confirmed unique Patroni primary may reconcile DNS',
      'switch_gate':'primary + DCS leader agreement + critical applications healthy',
      'token_storage':'/etc/taskforge/cloudflare-api-token (0600, on every public primary candidate)',
      'config_storage':'/etc/taskforge/cloudflare-failover.json (0600, on every public primary candidate)',
      'setup_command':'sudo bash ./cluster.sh cloudflare setup',
      'status_command':'sudo bash ./cluster.sh cloudflare status',
    }

def main()->int:
    ap=argparse.ArgumentParser(); ap.add_argument('--config',default=str(CONFIG_ROOT/'cluster.json')); ap.add_argument('--env-file',default=str(CONFIG_ROOT/'taskforge.env'))
    sp=ap.add_subparsers(dest='cmd',required=True)
    sp.add_parser('validate')
    p=sp.add_parser('render'); p.add_argument('--node',required=True)
    p=sp.add_parser('node-info'); p.add_argument('--node')
    sp.add_parser('cloudflare')
    args=ap.parse_args(); c=load_cluster(Path(args.config)); env=load_env(Path(args.env_file))
    if args.cmd=='validate': print(f"cluster {c.raw.get('cluster_name')} ok nodes={','.join(n.id for n in c.nodes)} voters={len(c.voters)}"); return 0
    if args.cmd=='render': render(c,c.node(args.node),env); print(f'rendered node={args.node} runtime={RUNTIME}'); return 0
    if args.cmd=='node-info':
        nid=args.node or ((RUNTIME/'node-id').read_text().strip() if (RUNTIME/'node-id').is_file() else '')
        n=c.node(nid); print(json.dumps(n.raw,indent=2)); return 0
    if args.cmd=='cloudflare': print(json.dumps(cloudflare(c,env),indent=2)); return 0
    return 2
if __name__=='__main__': raise SystemExit(main())
