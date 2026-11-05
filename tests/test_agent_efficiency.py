#!/usr/bin/env python3
from __future__ import annotations
import subprocess
import sys
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
import agent


class AgentEfficiencyTests(unittest.TestCase):
    def bare(self):
        a=agent.Agent.__new__(agent.Agent)
        a.project='taskforge-prod'
        a.assigned=['gateway','tasks-api','image-analyzer']
        a.optional=['image-analyzer']
        a.critical=['gateway','tasks-api']
        a._health_batch_calls=0
        a._sql_lock=threading.Lock();a._sql_desired=False;a._sql_force=False
        return a

    def test_batch_inspect_parses_many_with_one_docker_invocation(self):
        a=self.bare()
        output='\n'.join([
            '/taskforge-prod-gateway-1|running|healthy|1|false',
            '/taskforge-prod-tasks-api-1|exited|none|0|false',
        ])+'\n'
        fake=subprocess.CompletedProcess([],0,output,'')
        with patch.object(agent,'run',return_value=fake) as run:
            states=a.inspect_health_many(['taskforge-prod-gateway-1','taskforge-prod-tasks-api-1','taskforge-prod-missing-1'])
        self.assertEqual(run.call_count,1)
        self.assertEqual(states['taskforge-prod-gateway-1']['health'],'healthy')
        self.assertEqual(states['taskforge-prod-tasks-api-1']['status'],'exited')
        self.assertEqual(states['taskforge-prod-missing-1']['status'],'missing')

    def test_app_status_uses_single_batch_for_all_apps(self):
        a=self.bare()
        a.inspect_health_many=Mock(return_value={
            'taskforge-prod-gateway-1':{'status':'running','health':'healthy','restart':0,'oom':False},
            'taskforge-prod-tasks-api-1':{'status':'running','health':'healthy','restart':0,'oom':False},
            'taskforge-prod-image-analyzer-1':{'status':'exited','health':'unhealthy','restart':0,'oom':False},
        })
        rows,prepared,running,ready=a.app_status()
        self.assertEqual(a.inspect_health_many.call_count,1)
        self.assertEqual((prepared,running,ready),(3,2,2))
        self.assertEqual(len(rows),3)

    def test_stopped_optional_is_not_degraded_when_not_expected(self):
        a=self.bare()
        rows=[
            {'service':'gateway','status':'running','health':'healthy'},
            {'service':'tasks-api','status':'running','health':'healthy'},
            {'service':'image-analyzer','status':'exited','health':'unhealthy'},
        ]
        self.assertEqual(a._degraded_services(rows,expected=[]),[])
        self.assertEqual(a._degraded_services(rows,expected=a.optional),['image-analyzer'])

    def test_tls_asset_probe_is_short_cached(self):
        a=self.bare();a.node={'web':{'tls_mode':'auto'}};a.compose_env={}
        a._tls_assets_cache=None;a._tls_assets_cache_at=0.0
        with patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
            self.assertTrue(a._tls_assets_ready())
            self.assertTrue(a._tls_assets_ready())
        self.assertEqual(run.call_count,1)

    def test_host_snapshot_is_short_cached(self):
        a=self.bare();a._host_snapshot_cache=None;a._host_snapshot_cache_at=0.0
        fake=subprocess.CompletedProcess([],0,'Status: active\n','')
        with patch.object(agent,'run',return_value=fake) as run:
            a.host_snapshot();a.host_snapshot()
        self.assertEqual(run.call_count,2)  # ufw + wg only on first sample

    def test_steady_warm_standby_does_not_stop_again_or_report_optional_degraded(self):
        a=self.bare()
        a.cfg={'cluster_name':'taskforge-prod','shared_state_interval_seconds':60,'nodes':[]}
        a.node_id='B';a.node={'priority':20,'dcs_voter':True,'app':{'profile':'full','can_be_primary':True},'wireguard':{'ip':'10.80.0.2'},'postgres':{}}
        a.bundle_version='v40-r68';a.bundle_revision='68';a.env={};a.events=[]
        a.last_seen_leader='A';a._baseline=True;a._leader_candidate='';a._leader_candidate_count=0;a._patroni_unknown_count=0
        a.shared_ok=False;a.last_shared_push=0.0;a.cloudflare_last_success=0.0;a.cloudflare_last_result={'configured':True,'success':False,'status':'standby-no-write'}
        a.assist_enabled=False
        a.patroni=Mock(return_value=(200,{'role':'replica','state':'running','timeline':42}))
        a.patroni_cluster=Mock(return_value=(200,{'members':[{'name':'A','role':'leader'}]}))
        stopped_rows=[
            {'service':'gateway','status':'exited','health':'unhealthy','restart':0,'oom':False},
            {'service':'tasks-api','status':'exited','health':'unhealthy','restart':0,'oom':False},
            {'service':'image-analyzer','status':'exited','health':'unhealthy','restart':0,'oom':False},
        ]
        a.app_status=Mock(return_value=(stopped_rows,3,0,0))
        a._infra_status=Mock(return_value={'minio':True,'redis':True,'rabbitmq':True})
        a._tls_assets_ready=Mock(return_value=True)
        a._edge_refresh=Mock(return_value=False)
        a._write_standby_routing=Mock(return_value=False)
        a.stop_apps=Mock()
        a._sql_request=Mock()
        a.inspect_health=Mock(return_value={'status':'running','health':'healthy','restart':0,'oom':False})
        a.containers=Mock(return_value={})
        a.host_snapshot=Mock(return_value=({'status':'ok'},{'status':'active'},{'interface':'wg-taskforge','peers':[]}))
        a._cloudflare_configured=Mock(return_value=True)
        a._write_state=Mock()
        class Edge:
            def snapshot(self): return {'success':False}
        a.edge_controller=Edge()
        with patch.object(agent.Agent,'quorum_enabled',new_callable=PropertyMock,return_value=True), patch.object(agent,'CONTROL_FENCE_FILE',Path('/definitely/not/a/taskforge/fence')):
            a.tick()
        a.stop_apps.assert_not_called()
        a._sql_request.assert_called_with(False)
        self.assertEqual(a.telemetry['ha']['app_mode'],'warm-standby')
        self.assertEqual(a.telemetry['ha']['degraded_services'],[])


if __name__=='__main__':
    unittest.main(verbosity=2)
