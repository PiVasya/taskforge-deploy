#!/usr/bin/env python3
"""v41.5.5: Docker stopped-container fallback and HA-safe RabbitMQ operations."""
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
from managed_updates import DockerApplications, ManagedUpdates

PROJECT='taskforge-prod'
FORMAT_PREFIX=('inspect','-f')

def cp(rc=0,out='',err=''):
    return CompletedProcess([],rc,out,err)

def container_obj(name, image='sha256:new', cid='deadbeef0000000', state='created'):
    return {'Name':'/'+name, 'Id':cid, 'Image':image, 'RestartCount':0,
            'State':{'Status':state,'ExitCode':0,'StartedAt':'2026-10-19T15:00:00Z'}}

class FakeCLI:
    def __init__(self, project=PROJECT):
        self.project=project
        self.objects={}
        self.formatted_error=''
        self.ps_failure=''
        self.json_failure=''
        self.calls=[]
        self.last_composer=None
        self.json_incomplete=False
    def __call__(self,*args,timeout=60):
        self.calls.append(tuple(args))
        if args[:2]==FORMAT_PREFIX:
            if self.formatted_error:
                return cp(1,err=self.formatted_error)
            names=args[3:]
            if any(name not in self.objects for name in names):
                return cp(1,err='Error response from daemon: No such object: '+','.join(names))
            lines=[]
            for name in names:
                obj=self.objects[name]
                state=obj['State']
                lines.append('|'.join(('/'+name,obj['Id'],obj['Image'],state['Status'],
                                       (state.get('Health') or {}).get('Status','none'),
                                       str(obj.get('RestartCount') or 0),state['StartedAt'],
                                       str(state['ExitCode']))))
            return cp(0,'\n'.join(lines)+'\n')
        if args[:3]==('ps','-a','--format'):
            if self.ps_failure:return cp(1,err=self.ps_failure)
            return cp(0,'\n'.join('%s|%s'%(name,obj['Id'][:12]) for name,obj in self.objects.items())+'\n')
        if args[:2]==('container','inspect'):
            if self.json_failure:return cp(1,err=self.json_failure)
            ids=set(args[2:])
            obs=[obj for obj in self.objects.values() if obj['Id'][:12] in ids]
            if self.json_incomplete:obs=[]
            return cp(0,json.dumps(obs))
        return cp(0)

class DockerObserveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name)
        (root/'runtime').mkdir()
        self.backend=DockerApplications(root,root/'runtime',PROJECT,{})
        self.fake=FakeCLI()
        self.backend.cli=self.fake
    def test_no_overhead_for_healthy_or_staged_docker(self):
        self.fake.objects[f'{PROJECT}-front-1']=container_obj(f'{PROJECT}-front-1')
        self.assertEqual(self.backend.observe(['front'])['front']['image'],'sha256:new')
        self.assertEqual(self.backend.observe_counters()['fallback_calls'],0)
        self.assertEqual(len(self.fake.calls),1)
    def test_real_absent_service_not_mistaken_for_daemon_failure(self):
        # Exact failure observed in production: nonzero inspect with no rows.
        self.assertEqual(self.backend.observe(['front'])['front']['state'],'missing')
        self.assertEqual(self.backend.observe_counters()['fallback_successes'],1)
        self.assertEqual(self.backend.observe_counters()['missing_confirmed'],1)
    def test_localized_error_does_not_require_english_phrases(self):
        self.fake.formatted_error='Erreur: aucun objet de ce nom'
        self.assertEqual(self.backend.observe(['front'])['front']['state'],'missing')
    def test_template_error_recovers_with_raw_docker_json(self):
        name=f'{PROJECT}-front-1'
        self.fake.objects[name]=container_obj(name, image='sha256:correct')
        self.fake.formatted_error='template: :1:14: executing failed: invalid field'
        actual=self.backend.observe(['front'])['front']
        self.assertEqual(actual['image'],'sha256:correct')
        self.assertEqual(actual['state'],'created')
        self.assertEqual(actual['health'],'none')
        self.assertEqual(self.backend.observe_counters()['fallback_successes'],1)
    def test_json_works_for_two_existing_and_one_missing(self):
        self.fake.formatted_error='Docker inspect has a broken Go template'
        for svc in ['front','solutions-api']:
            n=f'{PROJECT}-{svc}-1'
            self.fake.objects[n]=container_obj(n,image='sha256:'+svc,cid='deadbeef'+svc.ljust(12,'0'))
        actual=self.backend.observe(['front','solutions-api','support-bot'])
        self.assertEqual(actual['front']['image'],'sha256:front')
        self.assertEqual(actual['solutions-api']['image'],'sha256:solutions-api')
        self.assertEqual(actual['support-bot']['state'],'missing')
        self.assertEqual(self.backend.observe_counters()['missing_confirmed'],1)
    def test_other_compose_project_never_adopted(self):
        self.fake.objects['tgcapcha-prod-watchtower-1']=container_obj('tgcapcha-prod-watchtower-1')
        self.assertEqual(self.backend.observe(['front'])['front']['state'],'missing')
        self.assertFalse(any(arg=='rm' or arg=='stop' for call in self.fake.calls for arg in call))
    def test_fail_closed_when_docker_list_unavailable(self):
        self.fake.ps_failure='Cannot connect to docker daemon'
        with self.assertRaisesRegex(RuntimeError,'docker-list-failed:1:Cannot connect'):
            self.backend.observe(['front'])
        self.assertEqual(self.backend.observe_counters()['fallback_successes'],0)
    def test_fail_closed_when_raw_inspect_unavailable(self):
        n=f'{PROJECT}-front-1'
        self.fake.objects[n]=container_obj(n)
        self.fake.formatted_error='format error'
        self.fake.json_failure='Cannot inspect container'
        with self.assertRaisesRegex(RuntimeError,'docker-json-inspect-failed:1:Cannot inspect'):
            self.backend.observe(['front'])
    def test_incomplete_docker_json_cannot_be_accepted(self):
        n=f'{PROJECT}-front-1'
        self.fake.objects[n]=container_obj(n)
        self.fake.formatted_error='format error'
        self.fake.json_incomplete=True
        with self.assertRaisesRegex(RuntimeError,'docker-json-inspect-invalid'):
            self.backend.observe(['front'])
    def test_silent_partial_formatted_output_requires_confirmation(self):
        self.fake.objects[f'{PROJECT}-front-1']=container_obj(f'{PROJECT}-front-1')
        self.fake.formatted_error='template error'
        actual=self.backend.observe(['front','solutions-api'])
        self.assertEqual(actual['front']['state'],'created')
        self.assertEqual(actual['solutions-api']['state'],'missing')
    def test_prepare_one_compose_when_template_fails_but_raw_json_proves_image(self):
        name=f'{PROJECT}-front-1'
        self.fake.objects[name]=container_obj(name)
        self.fake.formatted_error='template error'
        with patch.object(self.backend,'compose',return_value=cp()) as compose:
            self.backend.prepare('front','sha256:new')
        self.assertEqual(compose.call_count,1)
        self.assertEqual(self.backend.compose_counters()['force_recreate'],0)
    def test_prepare_forces_recreate_once_on_confirmed_wrong_image(self):
        name=f'{PROJECT}-front-1'
        self.fake.objects[name]=container_obj(name,image='sha256:old')
        self.fake.formatted_error='template error'
        with patch.object(self.backend,'compose',return_value=cp()) as compose:
            self.backend.prepare('front','sha256:new')
        self.assertEqual(compose.call_count,2)
        self.assertIn('--force-recreate',compose.call_args.args)
        self.assertNotIn('start',compose.call_args.args)
    def test_backend_snapshot_exposes_fallback_diagnostic(self):
        self.fake.ps_failure='Docker temporarily down'
        with self.assertRaises(RuntimeError):self.backend.observe(['front'])
        state=self.backend.observe_counters()
        self.assertIn('docker-list-failed',state['last_error'])

class SavedStateUpgradeTests(unittest.TestCase):
    def test_clear_only_obsolete_inspect_error_and_keep_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'application-updates.json'
            path.write_text(json.dumps({'services':{
                'front':{'error':'docker-inspect-unavailable','status':'retrying',
                         'next_attempt':9999999999,'attempts':10,
                         'last_good_image':'sha256:last-good','target_image':'sha256:next'},
                'solutions-api':{'error':'registry-pull-failed:1','status':'retrying',
                                 'next_attempt':9999999999,'attempts':4},
            },'incident':{}}))
            ctl=ManagedUpdates(object(),Path(tmp),['front','solutions-api'],threading.Event(),
                               lambda s:False,lambda:False,lambda *args,**kw:None)
            self.assertEqual(ctl.records['front']['error'],'')
            self.assertEqual(ctl.records['front']['next_attempt'],0)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:last-good')
            self.assertEqual(ctl.records['solutions-api']['error'],'registry-pull-failed:1')
            self.assertEqual(ctl.records['solutions-api']['next_attempt'],9999999999)
    def test_target_and_sha_are_not_forged_if_actual_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            root.joinpath('runtime').mkdir()
            b=DockerApplications(root,root/'runtime',PROJECT,{})
            f=FakeCLI()
            b.cli=f
            with patch.object(b,'compose',return_value=cp()):
                ctl=ManagedUpdates(b,root/'runtime',['front'],threading.Event(),lambda s:False,
                                   lambda:False,lambda *args,**kw:None)
                ctl.set_desired([],'config')
                ctl.retired=True
                ctl.next_owner_check=float('inf')
                ctl.records['front']['target_image']='sha256:new'
                ctl.step(100)
                self.assertIn('prepare-not-applied',ctl.records['front']['error'])
                self.assertEqual(ctl.records['front']['current_image'],'')
                self.assertEqual(ctl.records['front']['next_attempt'],130)
                before=b.compose_counters()['prepare']
                ctl.step(105)
                self.assertEqual(b.compose_counters()['prepare'],before)

class OptimizerSafetyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src=(ROOT/'cluster/ops/current/optimize-rabbitmq-standby.sh').read_text()
        blocks=re.findall(r"<<'PY'\n(.*?)\nPY",src,re.S)
        cls.code=blocks[1].replace('import json,os','import json,os,sys').replace('json.load(os.fdopen(3))','json.load(sys.stdin)')
    def payload(self,role='standby',phase='applying',failures=10):
        return {'node':{'id':'A','bundle_revision':410509},
                'ha':{'role':role,'leader':'B','app_mode':'warm-standby','reconciled':True,
                      'hot_start_ready':True,'traffic_ready':False,'control_plane_fenced':False},
                'docker':{'running_app_count':0},
                'update':{'mode':'node-agent','running':True,'status':phase,
                          'failed_count':failures,'maintenance_ack_token':''}}
    def validate(self,payload,mode='before',token='secret-token'):
        env={**os.environ,'TF_EXPECT_NODE':'A','TF_OPT_MODE':mode,'TF_OPT_TOKEN':token}
        return subprocess.run([sys.executable,'-S','-c',self.code],input=json.dumps(payload),
                              text=True,capture_output=True,env=env)
    def test_image_errors_do_not_block_infra_maintenance_when_hot_standby(self):
        result=self.validate(self.payload())
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('app_update_failures=10',result.stdout)
    def test_global_updater_fault_still_blocks(self):
        d=self.payload();d['update']['error']='registry-loop-failed:OSError'
        self.assertIn('no-updater-global-error',self.validate(d).stderr)
    def test_primary_and_nonidle_modes_are_forbidden(self):
        d=self.payload(role='primary')
        self.assertIn('standby-role',self.validate(d).stderr)
        d=self.payload();d['ha']['app_mode']='assist'
        self.assertIn('warm-standby-mode',self.validate(d).stderr)
        d=self.payload();d['docker']['running_app_count']=1
        self.assertIn('no-running-apps',self.validate(d).stderr)
    def test_hot_readiness_is_required(self):
        d=self.payload();d['ha']['hot_start_ready']=False
        self.assertIn('hot-standby-ready',self.validate(d).stderr)
    def test_acknowledged_maintenance_still_required(self):
        d=self.payload(phase='maintenance')
        self.assertNotEqual(self.validate(d,'paused').returncode,0)
        d['update']['maintenance_ack_token']='secret-token'
        self.assertEqual(self.validate(d,'paused').returncode,0)

if __name__=='__main__':unittest.main(verbosity=2)
