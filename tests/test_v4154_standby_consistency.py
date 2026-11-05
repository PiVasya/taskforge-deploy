#!/usr/bin/env python3
"""Regression tests for verified staging, bounded retry and HA-safe maintenance."""
import copy
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
from managed_updates import DockerApplications, ManagedUpdates


class StandbyBackend:
    def __init__(self):
        self.rows = {'front': {'state': 'created', 'health': 'none',
                               'image': 'sha256:old', 'id': 'front-old'}}
        self.image = 'sha256:new'
        self.calls = []
        self.mutate = True
    def retire_watchtower(self):
        self.calls.append('owner-check')
        return True
    def observe(self, names):
        return {n: copy.deepcopy(self.rows[n]) for n in names}
    def prepare(self, name, image):
        self.calls.append(('prepare', name, image))
        if self.mutate:
            self.rows[name] = {'state': 'created', 'health': 'none',
                               'image': image, 'id': 'front-new'}
    def references(self, services):
        return {s: 'ref-' + s for s in services}
    def pull(self, ref):
        self.calls.append(('pull', ref))
        return self.image
    def stop(self, name):
        self.calls.append(('stop', name))
        self.rows[name]['state'] = 'exited'
    def start(self, name):
        self.calls.append(('start', name))
        self.rows[name]['state'] = 'running'
        self.rows[name]['health'] = 'healthy'
    def preserve(self, name, image):
        return True
    def cleanup(self, image):
        return True


class StandbyCorrectnessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.backend = StandbyBackend()
        self.controller = ManagedUpdates(self.backend, self.runtime, ['front'],
                                         threading.Event(), lambda s: True,
                                         lambda: False, lambda *a, **kw: None,
                                         poll=300)
        self.controller.set_desired([], 'stable-routing')
        self.controller.step(100)

    def populate_target(self, moment=101):
        self.controller.poll_once(moment)
        self.assertEqual(self.controller.records['front']['target_image'], 'sha256:new')

    def count_prepares(self):
        return sum(isinstance(x, tuple) and x[0] == 'prepare' for x in self.backend.calls)

    def lease(self, duration=600, valid=True):
        item={'kind':'rabbitmq-standby', 'token':'deadbeefcafe1234567890',
              'expires_at':time.time()+duration}
        p=self.runtime/'updater-maintenance.json'
        p.write_text(json.dumps(item) if valid else '{not-json')
        p.chmod(0o600)
        return item

    def test_verified_prepare_is_staged_only_once(self):
        self.populate_target()
        self.controller.step(102)
        self.assertEqual(self.controller.records['front']['status'], 'staged')
        self.assertEqual(self.controller.records['front']['current_image'], 'sha256:new')
        self.assertEqual(self.controller.prepare_verified_count, 1)
        self.controller.step(107)
        self.controller.step(112)
        self.assertEqual(self.count_prepares(), 1)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='start' for x in self.backend.calls))

    def test_compose_success_without_new_image_does_not_forge_current(self):
        self.backend.mutate = False
        self.populate_target()
        self.controller.step(102)
        row=self.controller.records['front']
        self.assertEqual(row['current_image'], 'sha256:old')
        self.assertIn('container-prepare-not-applied', row['error'])
        self.assertEqual(row['status'], 'retrying')
        self.assertEqual(row['next_attempt'], 132)
        self.assertEqual(self.controller.prepare_mismatch_count, 1)
        self.assertEqual(self.controller.snapshot()['failed_count'], 1)
        self.assertEqual(self.count_prepares(), 1)
        for tick in range(103,132,5):self.controller.step(tick)
        self.assertEqual(self.count_prepares(), 1)

    def test_failure_backoff_grows_instead_of_cpu_spin(self):
        self.backend.mutate = False
        self.populate_target()
        for instant in (102,132,192):
            self.controller.step(instant)
        self.assertEqual(self.count_prepares(), 3)
        self.assertEqual(self.controller.records['front']['attempts'],3)
        self.assertEqual(self.controller.records['front']['next_attempt'],312)
        self.controller.step(193)
        self.assertEqual(self.count_prepares(), 3)

    def test_recovery_after_backoff_restores_clean_status(self):
        self.backend.mutate = False
        self.populate_target()
        self.controller.step(102)
        self.backend.mutate = True
        self.controller.step(131)
        self.assertEqual(self.count_prepares(),1)
        self.controller.step(132)
        self.assertEqual(self.controller.records['front']['status'],'staged')
        self.assertEqual(self.controller.records['front']['error'],'')
        self.assertEqual(self.controller.snapshot()['failed_count'],0)
        self.assertEqual(self.count_prepares(),2)

    def test_new_digest_overrides_old_failure_backoff(self):
        self.backend.mutate=False
        self.populate_target()
        self.controller.step(102)
        self.backend.image='sha256:next'
        self.controller.next_scan=0
        self.controller.records['front']['next_pull_at']=0
        self.controller.poll_once(105)
        self.assertEqual(self.controller.records['front']['next_attempt'],0)
        self.backend.mutate=True
        self.controller.step(106)
        self.assertEqual(self.controller.records['front']['current_image'],'sha256:next')
        self.assertEqual(self.controller.records['front']['error'],'')

    @unittest.skipUnless(os.geteuid()==0,'leases require root-owned files')
    def test_acknowledged_maintenance_pauses_compose_and_registry(self):
        lease=self.lease()
        self.controller.step(102)
        self.assertEqual(self.controller.phase,'maintenance')
        self.assertEqual(self.controller.snapshot()['maintenance_ack_token'],lease['token'])
        self.controller.poll_once(103)
        self.assertEqual(self.count_prepares(),0)
        self.assertFalse(any(isinstance(x,tuple) and x[0]=='pull' for x in self.backend.calls))

    @unittest.skipUnless(os.geteuid()==0,'leases require root-owned files')
    def test_promotion_preempts_maintenance_lease_immediately(self):
        self.lease()
        self.controller.step(102)
        self.controller.set_desired(['front'],'primary-routing')
        self.controller.step(103)
        self.assertNotEqual(self.controller.phase,'maintenance')
        self.assertEqual(self.controller.snapshot()['maintenance_ack_token'],'')
        self.assertTrue(any(isinstance(x,tuple) and x[0]=='start' for x in self.backend.calls))

    @unittest.skipUnless(os.geteuid()==0,'leases require root-owned files')
    def test_expired_lease_does_not_hold_back_standby(self):
        self.lease(duration=-1)
        self.controller.step(102)
        self.assertNotEqual(self.controller.phase,'maintenance')

    @unittest.skipUnless(os.geteuid()==0,'leases require root-owned files')
    def test_bad_or_world_readable_lease_is_ignored(self):
        self.lease(valid=False)
        self.controller.step(102)
        self.assertNotEqual(self.controller.phase,'maintenance')
        self.lease()
        (self.runtime/'updater-maintenance.json').chmod(0o644)
        self.controller.step(103)
        self.assertNotEqual(self.controller.phase,'maintenance')

    @unittest.skipUnless(os.geteuid()==0,'leases require root-owned files')
    def test_after_lease_release_checks_resume(self):
        self.lease()
        self.controller.step(102)
        (self.runtime/'updater-maintenance.json').unlink()
        self.populate_target(103)
        self.controller.step(104)
        self.assertEqual(self.controller.records['front']['current_image'],'sha256:new')
        self.assertNotEqual(self.controller.phase,'maintenance')

    def test_prepare_reason_persisted(self):
        self.populate_target()
        self.controller.step(102)
        record=self.controller.records['front']
        self.assertEqual(record['last_prepare_reason'],'image-mismatch')
        self.assertEqual(record['observed_before_prepare'],'sha256:old')
        restored=json.loads((self.runtime/'application-updates.json').read_text())
        self.assertEqual(restored['services']['front']['last_prepare_reason'],'image-mismatch')


class DockerPrepareVerificationTests(unittest.TestCase):
    def backend(self, td):
        path=Path(td)
        (path/'runtime').mkdir()
        (path/'cluster').mkdir()
        return DockerApplications(path,path/'runtime','taskforge-prod',{})

    def test_normal_prepare_does_not_force_recreate(self):
        with tempfile.TemporaryDirectory() as td:
            backend=self.backend(td)
            with patch.object(backend,'compose',return_value=CompletedProcess([],0,'','')) as compose:
                with patch.object(backend,'observe',return_value={'front':{'state':'created','image':'sha256:new'}}):
                    backend.prepare('front','sha256:new')
            self.assertEqual(compose.call_count,1)
            self.assertEqual(backend.compose_counters()['force_recreate'],0)

    def test_missing_actual_image_triggers_exactly_one_force_recreate(self):
        with tempfile.TemporaryDirectory() as td:
            backend=self.backend(td)
            with patch.object(backend,'compose',return_value=CompletedProcess([],0,'','')) as compose:
                with patch.object(backend,'observe',return_value={'front':{'state':'created','image':'sha256:old'}}):
                    backend.prepare('front','sha256:new')
            self.assertEqual(compose.call_count,2)
            self.assertIn('--force-recreate',compose.call_args.args)
            self.assertIn('--no-start',compose.call_args.args)
            self.assertIn('--no-deps',compose.call_args.args)
            self.assertEqual(backend.compose_counters()['force_recreate'],1)

    def test_force_recreate_failure_is_reported_not_hidden(self):
        with tempfile.TemporaryDirectory() as td:
            backend=self.backend(td)
            with patch.object(backend,'compose',side_effect=[CompletedProcess([],0,'',''),CompletedProcess([],1,'','')]):
                with patch.object(backend,'observe',return_value={'front':{'state':'created','image':'sha256:old'}}):
                    with self.assertRaisesRegex(RuntimeError,'container-force-prepare-failed:1'):
                        backend.prepare('front','sha256:new')


class ShellPreflightTests(unittest.TestCase):
    """Exercise the exact Python predicate embedded in the shipped optimizer."""
    @classmethod
    def setUpClass(cls):
        src=(ROOT/'cluster/ops/current/optimize-rabbitmq-standby.sh').read_text()
        blocks=re.findall(r"<<'PY'\n(.*?)\nPY",src,re.S)
        cls.validator=blocks[1].replace('import json,os','import json,os,sys').replace(
            'json.load(os.fdopen(3))','json.load(sys.stdin)')
    def telemetry(self, role='standby', mode='warm-standby', phase='applying', ack=''):
        return {'node':{'id':'A','bundle_revision':410509},
                'ha':{'role':role,'leader':'B','app_mode':mode,
                      'reconciled':True,'hot_start_ready':True,'traffic_ready':False,
                      'control_plane_fenced':False},
                'docker':{'running_app_count':0},
                'update':{'mode':'node-agent','running':True,'status':phase,
                          'failed_count':0,'maintenance_ack_token':ack}}
    def validate(self, payload, mode='before', token='auth-token'):
        env={**os.environ,'TF_EXPECT_NODE':'A','TF_OPT_MODE':mode,'TF_OPT_TOKEN':token}
        return subprocess.run([sys.executable,'-S','-c',self.validator],
                              input=json.dumps(payload),text=True,capture_output=True,env=env)
    def test_operator_can_request_pause_while_image_is_applying(self):
        self.assertEqual(self.validate(self.telemetry()).returncode,0)
    def test_no_ack_means_no_rabbitmq_recreation(self):
        self.assertNotEqual(self.validate(self.telemetry(phase='applying'),mode='paused').returncode,0)
        self.assertNotEqual(self.validate(self.telemetry(phase='maintenance'),mode='paused').returncode,0)
        self.assertEqual(self.validate(self.telemetry(phase='maintenance',ack='auth-token'),mode='paused').returncode,0)
    def test_primary_or_assist_mode_are_forbidden(self):
        for role,mode in (('primary','primary'),('standby','assist'),('unknown','warm-standby')):
            with self.subTest(role=role,mode=mode):
                self.assertIn('standby-preflight-failed', self.validate(self.telemetry(role=role,mode=mode)).stderr)
    def test_fence_or_hot_readiness_loss_prevents_mutation(self):
        for target,value in (('control_plane_fenced',True),('hot_start_ready',False),('traffic_ready',True)):
            with self.subTest(target=target):
                t=self.telemetry(); t['ha'][target]=value
                self.assertNotEqual(self.validate(t).returncode,0)
    def test_existing_running_apps_reject_standby_optimizer(self):
        t=self.telemetry();t['docker']['running_app_count']=1
        result=self.validate(t)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('no-running-apps',result.stderr)
    def test_wrong_control_revision_rejected(self):
        t=self.telemetry();t['node']['bundle_revision']=410503
        self.assertIn('control-revision',self.validate(t).stderr)
    def test_health_duration_go_string_and_nanoseconds_supported(self):
        src=(ROOT/'cluster/ops/current/optimize-rabbitmq-standby.sh').read_text()
        self.assertIn('[ "$1" = 30s ] || [ "$1" = 30000000000 ]',src)
        self.assertIn('[ "$new_retries" != 10 ]',src)
        for value,expected in [('30s',0),('30000000000',0),('5s',1)]:
            actual=subprocess.run(['bash','-c',
                'is_30s() { [ "$1" = 30s ] || [ "$1" = 30000000000 ]; }; is_30s "$1"',
                 'bash',value]).returncode
            self.assertEqual(actual,expected,value)


class ReleaseSafetyTests(unittest.TestCase):
    def test_rabbitmq_optimizer_preserves_volume_and_primary_gate(self):
        src=(ROOT/'cluster/ops/current/optimize-rabbitmq-standby.sh').read_text()
        for required in ("h.get('role')=='standby'", "h.get('app_mode')=='warm-standby'",
                         "h.get('hot_start_ready') is True", 'updater-maintenance.json',
                         "u.get('maintenance_ack_token')", '--no-deps --pull never rabbitmq',
                         'new_volume', 'old_volume', 'trap cleanup_lease EXIT',
                         'standby_hot_readiness=PASS', 'is_30s', 'old_retries', 'new_retries'):
            self.assertIn(required,src)
        for forbidden in ('docker volume rm', 'docker volume prune','docker rm -v', ' down -v'):
            self.assertNotIn(forbidden,src)
    def test_schema_and_data_services_unchanged(self):
        src=(ROOT/'cluster/managed_updates.py').read_text()
        self.assertIn("'postgres', 'etcd', 'redis', 'rabbitmq', 'minio'",src)
        self.assertIn('self.backend.observe([service])',src)
        self.assertIn('next_attempt=now + min(1800',src)
        self.assertIn('self._maintenance_token()',src)
        self.assertIn('self.wake.wait(5)',src)


if __name__ == '__main__':
    unittest.main(verbosity=2)
