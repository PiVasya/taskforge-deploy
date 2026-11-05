#!/usr/bin/env python3
from __future__ import annotations
import importlib.util, json, sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
spec=importlib.util.spec_from_file_location('taskforge_agent_v41.5.5',ROOT/'cluster'/'agent.py')
mod=importlib.util.module_from_spec(spec);sys.modules[spec.name]=mod;spec.loader.exec_module(mod)

agent=mod.Agent.__new__(mod.Agent)
agent.project='taskforge-prod';agent.assigned=['gateway','identity-api'];agent.assist=['gateway'];agent.node_id='A'
agent._docker_inventory_cache=None;agent._docker_inventory_cache_at=0.0;agent._docker_inventory_cache_mode=''
agent.cname=lambda svc:f'taskforge-prod-{svc}-1'
agent._container_group=mod.Agent._container_group.__get__(agent,mod.Agent)
agent._container_expected_state=mod.Agent._container_expected_state.__get__(agent,mod.Agent)
type(agent).quorum_enabled=property(lambda self: True)
inspect=[
 {'Name':'/taskforge-prod-gateway-1','Image':'sha256:gateway','Config':{'Labels':{'com.docker.compose.service':'gateway'}},'State':{'Status':'running','Health':{'Status':'healthy'},'OOMKilled':False,'StartedAt':'2026-09-24T10:00:00Z','FinishedAt':'0001-01-01T00:00:00Z','ExitCode':0,'Error':''},'RestartCount':2},
 {'Name':'/taskforge-prod-postgres-1','Image':'sha256:pg','Config':{'Labels':{'com.docker.compose.service':'postgres'}},'State':{'Status':'running','Health':{'Status':'healthy'},'OOMKilled':False,'StartedAt':'2026-09-24T10:00:00Z','FinishedAt':'0001-01-01T00:00:00Z','ExitCode':0,'Error':''},'RestartCount':0},
]
def fake_run(args,timeout=30,env=None):
    if args[:3]==['docker','ps','-aq']:
        return SimpleNamespace(returncode=0,stdout='one\ntwo\n',stderr='')
    if args[:2]==['docker','inspect']:
        return SimpleNamespace(returncode=0,stdout=json.dumps(inspect),stderr='')
    raise AssertionError(args)
with patch.object(mod,'run',fake_run):
    rows=mod.Agent.docker_inventory(agent,'primary')
by={r['service']:r for r in rows}
assert by['gateway']['state']=='running' and by['gateway']['expected_state']=='running'
assert by['gateway']['restart_count']==2 and by['gateway']['image_fingerprint']=='sha256:gateway'
assert by['postgres']['group']=='infrastructure' and by['postgres']['expected_state']=='running'
assert by['identity-api']['state']=='missing' and by['identity-api']['expected_state']=='running'
assert by['etcd']['state']=='missing' and 'watchtower' not in by

heal=mod.Agent.__new__(mod.Agent)
heal._sql_request=lambda *a,**k:None
calls=[]
heal.compose=lambda *a,**k:calls.append(a)
mod.Agent.start_service_set(heal,['gateway','identity-api'])
assert calls==[], 'HA tick must not block on Compose lifecycle work'
print('PASS: container inventory and non-blocking lifecycle delegation')
