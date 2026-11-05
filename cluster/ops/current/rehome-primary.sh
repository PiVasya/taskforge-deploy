#!/usr/bin/env bash
set -Eeuo pipefail
source /opt/taskforge/control/cluster/lib/common.sh
cluster_need_env; cluster_need_config; cluster_runtime_prepare
[ "$(id -u)" -eq 0 ] || cluster_die 'rehome-primary requires root'
node="$(cluster_node_id)"
tx="$TASKFORGE_CLUSTER_RUNTIME/primary-rehome-r55.json"
lock="$TASKFORGE_CLUSTER_RUNTIME/primary-rehome-r55.lock"
command -v flock >/dev/null 2>&1 || cluster_die 'flock is required'
exec 8>"$lock"; flock -n 8 || cluster_die 'another r55 primary re-home is active'

node_field(){ python3 -S - "$TASKFORGE_CLUSTER_CONFIG" "$1" <<'PY'
import json,sys
n=next(x for x in json.load(open(sys.argv[1]))['nodes'] if str(x['id'])==sys.argv[2])
print('|'.join(map(str,[n['wireguard']['ip'],(n.get('postgres') or {}).get('patroni_rest_port',8008),n.get('health_port',9187),n.get('priority',0),(n.get('app') or {}).get('profile','full')])))
PY
}
IFS='|' read -r local_ip local_rest local_health _ _ <<<"$(node_field "$node")"
current_leader(){ python3 -S - "$TASKFORGE_CLUSTER_CONFIG" <<'PY'
import json,sys,urllib.request
cfg=json.load(open(sys.argv[1])); found=[]
for n in cfg['nodes']:
    try:
        with urllib.request.urlopen(f"http://{n['wireguard']['ip']}:{(n.get('postgres') or {}).get('patroni_rest_port',8008)}/primary",timeout=2) as r:
            if r.status==200: found.append(str(n['id']))
    except Exception: pass
if len(found)!=1: raise SystemExit('expected exactly one Patroni primary, got '+repr(found))
print(found[0])
PY
}
member_lag_ok(){
  local leader="$1" candidate="$2" limit="$3" lip lrest
  IFS='|' read -r lip lrest _ _ _ <<<"$(node_field "$leader")"
  curl -fsS --connect-timeout 2 --max-time 5 "http://$lip:$lrest/cluster" 2>/dev/null | python3 -S -c 'import json,sys;c=sys.argv[1];limit=int(sys.argv[2]);d=json.load(sys.stdin);m=[x for x in d.get("members",[]) if str(x.get("name",""))==c];assert len(m)==1,m;x=m[0];assert str(x.get("state","")).lower() in {"running","streaming"},x;assert int(x.get("lag",0) or 0)<=limit,(x.get("lag"),limit)' "$candidate" "$limit" >/dev/null 2>&1
}
agent_hot(){
  local id="$1" ip rest hp
  IFS='|' read -r ip rest hp _ _ <<<"$(node_field "$id")"
  [ "$(cluster_http_code "http://$ip:$rest/replica")" = 200 ] || return 1
  curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$hp/ha/telemetry" 2>/dev/null | python3 -S -c 'import json,sys;t=json.load(sys.stdin);h=t.get("ha") or {};assert h.get("reconciled") is True;assert h.get("role")=="standby";assert h.get("hot_start_ready") is True;assert h.get("control_plane_fenced") is False;assert str(h.get("leader","")).strip()' >/dev/null 2>&1
}
edge_configured(){ [ -s /etc/taskforge/cloudflare-api-token ] && [ -s /etc/taskforge/cloudflare-failover.json ]; }
primary_ready(){
  local id="$1" ip rest hp
  IFS='|' read -r ip rest hp _ _ <<<"$(node_field "$id")"
  [ "$(cluster_http_code "http://$ip:$rest/primary")" = 200 ] || return 1
  if edge_configured; then
    curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$hp/ha/telemetry" 2>/dev/null | python3 -S -c 'import json,sys;t=json.load(sys.stdin);h=t.get("ha") or {};assert h.get("reconciled") is True;assert h.get("role")=="primary";assert h.get("applications_active") is True;assert h.get("hot_start_ready") is True;assert h.get("control_plane_fenced") is False;assert h.get("traffic_ready") is True' >/dev/null 2>&1
  else
    curl -fsS --connect-timeout 2 --max-time 5 "http://$ip:$hp/ha/telemetry" 2>/dev/null | python3 -S -c 'import json,sys;t=json.load(sys.stdin);h=t.get("ha") or {};assert h.get("reconciled") is True;assert h.get("role")=="primary";assert h.get("applications_active") is True;assert h.get("hot_start_ready") is True;assert h.get("control_plane_fenced") is False' >/dev/null 2>&1
  fi
}
wait_primary_ready(){ local id="$1" i; for i in $(seq 1 60); do primary_ready "$id" && return 0; [ $((i%5)) -ne 0 ] || echo "[taskforge] waiting for temporary primary $id: $((i*2))s/120s" >&2; sleep 2; done; return 1; }
select_candidate(){
  local leader limit id attempt
  leader="$(current_leader)" || return 1
  limit="$(python3 -S -c 'import json,sys;print(int(json.load(open(sys.argv[1])).get("maximum_lag_on_switchover_bytes",16777216)))' "$TASKFORGE_CLUSTER_CONFIG")"
  for attempt in $(seq 1 12); do
    while IFS= read -r id; do
      [ -n "$id" ] || continue
      if agent_hot "$id" && member_lag_ok "$leader" "$id" "$limit"; then echo "$id"; return 0; fi
    done < <(python3 -S - "$TASKFORGE_CLUSTER_CONFIG" "$node" <<'PY'
import json,sys
cfg=json.load(open(sys.argv[1])); local=sys.argv[2]
xs=[x for x in cfg['nodes'] if str(x['id'])!=local and (x.get('app') or {}).get('can_be_primary',True)]
xs.sort(key=lambda x:((x.get('app') or {}).get('profile','full')!='full',-int(x.get('priority',0))))
for x in xs: print(x['id'])
PY
)
    [ "$attempt" -eq 12 ] || sleep 5
  done
  return 1
}
patroni_switch(){
  local leader="$1" candidate="$2" ip rest out code
  IFS='|' read -r ip rest _ _ _ <<<"$(node_field "$leader")"; out="$(mktemp)"
  code="$(cluster_patroni_switchover "$leader" "$candidate" "$ip" "$rest" "$out")"
  if [ "$code" != 200 ] && [ "$code" != 202 ]; then echo "error: Patroni switchover $leader->$candidate failed HTTP=$code body=$(tail -c 500 "$out" 2>/dev/null)" >&2; rm -f "$out"; return 1; fi
  rm -f "$out"; echo "[taskforge] Patroni accepted controlled switchover $leader -> $candidate (HTTP $code)"
}
wait_roles(){
  local leader="$1" replica="$2" lip lrest rip rrest i
  IFS='|' read -r lip lrest _ _ _ <<<"$(node_field "$leader")"; IFS='|' read -r rip rrest _ _ _ <<<"$(node_field "$replica")"
  for i in $(seq 1 120); do [ "$(cluster_http_code "http://$lip:$lrest/primary")" = 200 ] && [ "$(cluster_http_code "http://$rip:$rrest/replica")" = 200 ] && return 0; sleep 1; done
  return 1
}
tx_get(){ [ -s "$tx" ] || return 0; python3 -S - "$tx" "$1" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]));v=d.get(sys.argv[2],'');print(v if v is not None else '')
PY
}
tx_set(){ python3 -S - "$tx" "$node" "$@" <<'PY'
import datetime,json,os,sys,tempfile
p,node=sys.argv[1:3]; a=sys.argv[3:]
try:d=json.load(open(p)) if os.path.isfile(p) else {}
except Exception as e: raise SystemExit('invalid r55 primary transaction: '+str(e))
if len(a)%2: raise SystemExit('odd tx args')
for i in range(0,len(a),2): d[a[i]]=a[i+1]
d['revision']=55;d['node']=node;d['updated_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
fd,tmp=tempfile.mkstemp(prefix='.primary-rehome-r55.',dir=os.path.dirname(p),text=True)
with os.fdopen(fd,'w') as f: json.dump(d,f,indent=2);f.write('\n');f.flush();os.fsync(f.fileno())
os.chmod(tmp,0o600);os.replace(tmp,p)
PY
cluster_chown_owner "$tx" 2>/dev/null || true; }

phase="$(tx_get phase)"; candidate="$(tx_get candidate)"
if [ -z "$phase" ]; then
  [ "$(current_leader)" = "$node" ] || cluster_die "local node $node is not current Primary"
  candidate="$(select_candidate)" || cluster_die 'no healthy hot standby is available for controlled local re-home'
  tx_set phase switch-requested candidate "$candidate" original_primary "$node"
  phase=switch-requested
  echo "[taskforge] selected temporary primary=$candidate for state-based r55 re-home"
fi
[ "$(tx_get node)" = "$node" ] || cluster_die 'r55 primary transaction belongs to another node'
candidate="$(tx_get candidate)"; [ -n "$candidate" ] || cluster_die 'r55 primary transaction candidate missing'

if [ "$phase" = switch-requested ]; then
  leader="$(current_leader)"
  if [ "$leader" = "$node" ]; then
    agent_hot "$candidate" || cluster_die "temporary candidate $candidate is no longer hot-ready"
    patroni_switch "$node" "$candidate" || cluster_die 'temporary switchover failed'
    wait_roles "$candidate" "$node" || cluster_die 'temporary switchover did not converge'
  elif [ "$leader" != "$candidate" ]; then cluster_die "unexpected leader during resume: $leader"; fi
  wait_primary_ready "$candidate" || {
    echo "[taskforge] temporary primary $candidate did not become application-ready; rolling back before local container mutation" >&2
    [ "$(cluster_http_code "http://$local_ip:$local_rest/replica")" = 200 ] || cluster_die 'cannot roll back: local node is not a replica'
    patroni_switch "$candidate" "$node" || cluster_die 'rollback switchover failed'
    wait_roles "$node" "$candidate" || cluster_die 'rollback did not converge'
    rm -f "$tx"
    cluster_die 'temporary primary readiness failed; local containers were not re-homed'
  }
  tx_set phase temporary-primary-proven
  phase=temporary-primary-proven
fi

if [ "$phase" = temporary-primary-proven ]; then
  [ "$(current_leader)" = "$candidate" ] || cluster_die 'temporary primary changed before local re-home'
  bash "$CLUSTER_DIR/ops/current/rehome-replica.sh" --keep-migration-fence
  tx_set phase local-rehome-complete
  phase=local-rehome-complete
fi

if [ "$phase" = local-rehome-complete ]; then
  leader="$(current_leader)"
  if [ "$leader" = "$candidate" ]; then
    agent_hot "$node" || cluster_die 're-homed local node is not hot-ready for restoring original Primary'
    tx_set phase restore-requested
    patroni_switch "$candidate" "$node" || cluster_die 'restore switchover failed'
    wait_roles "$node" "$candidate" || cluster_die 'restore switchover did not converge'
  elif [ "$leader" != "$node" ]; then cluster_die "unexpected leader before restore: $leader"; fi
  tx_set phase restore-requested
  phase=restore-requested
fi

if [ "$phase" = restore-requested ]; then
  leader="$(current_leader)"
  if [ "$leader" = "$candidate" ]; then patroni_switch "$candidate" "$node" || cluster_die 'resume restore switchover failed'; wait_roles "$node" "$candidate" || cluster_die 'resume restore did not converge';
  elif [ "$leader" != "$node" ]; then cluster_die "unexpected leader while restoring original Primary: $leader"; fi
  wait_primary_ready "$node" || {
    echo '[taskforge] original Primary did not become application-ready; trying safe rollback to temporary Primary' >&2
    if agent_hot "$candidate" && patroni_switch "$node" "$candidate" && wait_roles "$candidate" "$node" && wait_primary_ready "$candidate"; then tx_set phase local-rehome-complete; cluster_die 'local runtime is re-homed, but application activation failed after restore; cluster was safely returned to temporary Primary'; fi
    cluster_die 'original Primary activation failed and rollback could not be proven'
  }
  tx_set phase complete
  phase=complete
fi

[ "$phase" = complete ] || cluster_die "unexpected r55 primary re-home phase=$phase"
rm -f "$TASKFORGE_CLUSTER_RUNTIME/migration-fence" "$TASKFORGE_CLUSTER_RUNTIME/join-rehome-fence"
systemctl restart taskforge-cluster.service >/dev/null || cluster_die 'cannot restart v41.5.9 Agent after primary re-home'
bash "$CLUSTER_DIR/ops/current/mount-audit.sh" >/dev/null || cluster_die 'final stable-path audit failed after primary re-home'
mkdir -p "$TASKFORGE_CLUSTER_RUNTIME/easy"
mv -f "$tx" "$TASKFORGE_CLUSTER_RUNTIME/easy/primary-rehome-r55-complete.json"
cluster_chown_owner "$TASKFORGE_CLUSTER_RUNTIME/easy/primary-rehome-r55-complete.json" 2>/dev/null || true
echo "PASS: node $node re-homed from actual live state; temporary_primary=$candidate; original Primary restored"
