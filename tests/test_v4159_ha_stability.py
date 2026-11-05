"""Adversarial HA/mutation tests: uncertainty must not become a destructive command."""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
sys.path.insert(0,str(ROOT/'tests'))
import agent
import ha_health
import ha_policy
from managed_updates import ManagedUpdates, DockerApplications, MutationDeferred
from test_managed_updates import FakeDocker


class MutationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name);self.docker=FakeDocker()
        self.allow_start=True;self.must_stop=False;self.allow_recovery=True
        self.control=ManagedUpdates(self.docker,self.path,['broken','other'],threading.Event(),
            lambda s:self.allow_start,lambda:False,lambda *a,**k:None,grace=30,
            can_stop=lambda s:self.must_stop,can_recover=lambda s:self.allow_recovery)
        self.control.set_desired(['broken','other'],'primary')
        self.control.step(100)
        self.docker.calls.clear()

    def test_successful_start_survives_transient_followup_timeout(self):
        self.control.records['broken']['target_image']='sha256:new'
        start=self.docker.start
        def start_and_timeout(service):
            start(service);self.allow_start=False
        self.docker.start=start_and_timeout
        self.control.step(105)
        self.assertIn(('start','broken'),self.docker.calls)
        self.assertNotIn(('stop','broken'),self.docker.calls)

    def test_actual_demotion_after_start_still_stops(self):
        self.control.records['broken']['target_image']='sha256:new'
        start=self.docker.start
        def start_and_demote(service):
            start(service);self.must_stop=True;self.allow_start=False
        self.docker.start=start_and_demote
        self.control.step(105)
        self.assertIn(('stop','broken'),self.docker.calls)

    def test_unconfirmed_standby_does_not_stop_running_apps(self):
        self.control.set_desired([],'standby')
        self.control.step(105)
        self.assertFalse(any(c[0] in ('stop','prepare','start') for c in self.docker.calls))

    def test_confirmed_standby_does_stop(self):
        self.must_stop=True;self.control.set_desired([],'standby')
        self.control.step(105)
        self.assertIn(('stop','broken'),self.docker.calls)

    def test_promotion_during_stop_probe_cancels_stale_stop(self):
        self.control.set_desired([],'standby')
        def recovered(service):
            self.control.set_desired(['broken','other'],'primary');return True
        self.control.can_stop=recovered
        self.control.step(105)
        self.assertNotIn(('stop','broken'),self.docker.calls)

    def test_transient_unhealthy_is_not_restarted(self):
        self.docker.rows['broken']['health']='unhealthy'
        for now in (105,110,115,120):self.control.step(now)
        self.docker.rows['broken']['health']='healthy';self.control.step(125)
        self.assertFalse(any(c[0]=='stop' for c in self.docker.calls))

    def test_elapsed_time_and_sample_count_both_required(self):
        self.docker.rows['broken']['health']='unhealthy'
        for now in (105,106,107,108):self.control.step(now)
        self.assertNotIn(('stop','broken'),self.docker.calls)
        self.control.step(166)
        self.assertIn(('stop','broken'),self.docker.calls)
        self.assertIn(('start','broken'),self.docker.calls)

    def test_only_two_widely_spaced_samples_do_not_confirm_unhealthy(self):
        self.docker.rows['broken']['health']='unhealthy'
        self.control.step(105);self.control.step(166)
        self.assertNotIn(('stop','broken'),self.docker.calls)

    def test_dependency_outage_blocks_health_restart(self):
        self.allow_recovery=False;self.docker.rows['broken']['health']='unhealthy'
        for now in (105,135,166,180):self.control.step(now)
        self.assertNotIn(('stop','broken'),self.docker.calls)
        self.allow_recovery=True;self.control.step(200)
        self.assertIn(('start','broken'),self.docker.calls)

    def test_dependency_outage_blocks_running_image_replacement(self):
        self.allow_recovery=False
        self.control.records['broken']['target_image']='sha256:new'
        self.control.step(105)
        self.assertFalse(any(c[0] in ('stop','prepare') for c in self.docker.calls))
        self.assertEqual(self.control.records['broken']['status'],'waiting-role')
        self.assertFalse(self.control.records['broken'].get('error'))

    def test_stopped_container_still_recovers_without_unhealthy_delay(self):
        self.docker.rows['broken'].update(state='exited',health='unhealthy')
        self.control.step(105)
        self.assertIn(('start','broken'),self.docker.calls)

    def test_running_identity_change_resets_confirmation(self):
        self.docker.rows['broken']['health']='unhealthy'
        self.control.step(105);self.control.step(135)
        self.docker.rows['broken']['id']='replacement'
        self.control.step(166)
        self.assertNotIn(('stop','broken'),self.docker.calls)

    def test_real_backend_rechecks_guard_before_force_recreation(self):
        backend=DockerApplications(ROOT,self.path,'test',{})
        backend.compose=Mock(return_value=subprocess.CompletedProcess([],0,'',''))
        backend.observe=Mock(return_value={'broken':{'state':'created','image':'sha256:wrong'}})
        values=iter([True,False])
        with self.assertRaises(MutationDeferred):backend.prepare_guarded('broken','sha256:new',lambda:next(values))
        self.assertEqual(backend.compose.call_count,1)


class EvidenceTests(unittest.TestCase):
    def test_single_timeout_does_not_confirm_failure(self):
        e=ha_health.Evidence()
        self.assertEqual(e.update(False,0),(False,False))
        self.assertEqual(e.update(True,30),(False,False))
        self.assertEqual(e.count,0)

    def test_three_failures_without_elapsed_window_do_not_confirm(self):
        e=ha_health.Evidence()
        for t in (0,1,2):self.assertFalse(e.update(False,t)[0])
        self.assertTrue(e.update(False,30)[0])
        self.assertEqual(e.update(True,35),(False,True))

    def test_probe_validates_identity(self):
        node={'id':'A','wireguard':{'ip':'127.0.0.1'},'dcs_voter':False}
        def response(url,timeout):
            return {'http':200,'elapsed_ms':1,'error':'','data':{'ok':True,'node':'B'}}
        value=ha_health.probe_node(node,transport=response)
        self.assertFalse(value['endpoints']['agent']['healthy'])
        self.assertFalse(value['endpoints']['patroni']['healthy'])

    def test_monitor_persists_bounded_evidence_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as raw:
            events=[]
            monitor=ha_health.HaHealth([{'id':'B','dcs_voter':True}],'A',raw,threading.Event(),lambda *a,**k:events.append(a))
            row={'node':'B','endpoints':{'agent':{'healthy':False},'patroni':{'healthy':False},'etcd':{'healthy':False}}}
            for now in (0,15,30,45):monitor.record([copy.deepcopy(row)],now)
            self.assertEqual(len(events),1)
            self.assertFalse(monitor.snapshot()['quorum_observed'])
            for value in row['endpoints'].values():value['healthy']=True
            monitor.record([row],60)
            self.assertEqual(len(events),2)
            self.assertTrue(Path(raw,'ha-health-history.jsonl').is_file())

    def test_previous_primary_agent_timeout_with_live_patroni_is_not_node_down(self):
        a=agent.Agent.__new__(agent.Agent);a.node_id='A'
        a.cfg={'nodes':[{'id':'B','wireguard':{'ip':'10.80.0.2'}}]};a._event=Mock()
        with patch.object(agent,'http_json',side_effect=[(0,{}),(200,{'role':'replica','state':'running'})]):
            a._probe_previous_primary('B','B>A|50')
        a._event.assert_not_called()


class AgentGuardTests(unittest.TestCase):
    def bare(self):
        a=agent.Agent.__new__(agent.Agent)
        a.project='taskforge-prod'
        a.node_id='B';a.node={'app':{'can_be_primary':True},'wireguard':{'ip':'10.80.0.2'}}
        a.assist_enabled=False;a.assist=[]
        a.patroni=Mock(return_value=(200,{'role':'primary','state':'running'}))
        a.patroni_cluster=Mock(return_value=(0,{}))
        a._patroni_unknown_count=0;a._patroni_unknown_since=None
        return a

    def test_missing_cluster_reply_is_not_stop_evidence(self):
        a=self.bare()
        with patch.object(agent.Agent,'quorum_enabled',new_callable=PropertyMock,return_value=True):
            self.assertFalse(a.app_stop_required('gateway'))

    def test_replica_is_immediate_positive_stop_evidence(self):
        a=self.bare();a.patroni.return_value=(200,{'role':'replica','state':'running'})
        with patch.object(agent.Agent,'quorum_enabled',new_callable=PropertyMock,return_value=True):
            self.assertTrue(a.app_stop_required('tasks-api'))

    def test_unknown_role_requires_both_time_and_samples(self):
        a=self.bare();a.patroni.return_value=(0,{})
        a._patroni_unknown_since=100;a._patroni_unknown_count=3
        with patch.object(agent.Agent,'quorum_enabled',new_callable=PropertyMock,return_value=True):
            with patch.object(agent.time,'monotonic',return_value=125):self.assertFalse(a.app_stop_required('tasks-api'))
            with patch.object(agent.time,'monotonic',return_value=131):self.assertTrue(a.app_stop_required('tasks-api'))

    def test_start_requires_patroni_lock_proof_not_just_cached_cluster(self):
        a=self.bare();a.patroni_cluster.return_value=(200,{'members':[{'name':'B','role':'leader'}]})
        with patch.object(agent.Agent,'quorum_enabled',new_callable=PropertyMock,return_value=True):
            with patch.object(agent,'http_json',return_value=(503,{})):self.assertFalse(a.app_start_allowed('tasks-api'))
            with patch.object(agent,'http_json',return_value=(200,{})):self.assertTrue(a.app_start_allowed('tasks-api'))

    def test_local_loopback_fallback_does_not_turn_network_failure_into_stop(self):
        a=self.bare()
        answer=json.dumps({'http':200,'body':{'role':'primary','state':'running'}})
        with patch.object(agent,'http_json',return_value=(0,{})), patch.object(agent,'run',return_value=subprocess.CompletedProcess([],0,answer,'')) as runner:
            self.assertEqual(a._local_patroni('patroni'),(200,{'role':'primary','state':'running'}))
            cmd=runner.call_args.args[0]
            self.assertEqual(cmd[:3],['docker','exec','taskforge-prod-postgres-1'])
            self.assertNotIn('stop',cmd)
            self.assertEqual(a._patroni_loopback_recoveries,1)


if __name__=='__main__':unittest.main(verbosity=2)
