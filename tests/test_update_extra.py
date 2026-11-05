import sys, tempfile, threading, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent))
from test_managed_updates import FakeDocker
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cluster'))
from managed_updates import ManagedUpdates, DockerApplications
from subprocess import CompletedProcess

class UpdateExtraTests(unittest.TestCase):
    def test_unhealthy_live_process_recovery_is_not_repeated_each_tick(self):
        with tempfile.TemporaryDirectory() as td:
            fake=FakeDocker();fake.rows['broken']['health']='unhealthy'
            ctl=ManagedUpdates(fake,Path(td),['broken','other'],threading.Event(),lambda s:True,lambda:False,lambda *a,**kw:None,grace=30)
            ctl.set_desired(['broken','other'],'a');ctl.step(100)
            self.assertEqual(fake.calls.count(('stop','broken')),0)
            ctl.step(130);ctl.step(161)
            self.assertEqual(fake.calls.count(('stop','broken')),1)
            self.assertEqual(fake.calls.count(('start','broken')),1)
            ctl.step(166);ctl.step(171)
            self.assertEqual(fake.calls.count(('start','broken')),1)
    def test_backend_prepare_never_upgrades_dependencies_or_restarts_database(self):
        with tempfile.TemporaryDirectory() as td:
            docker=DockerApplications(Path(td),Path(td),'taskforge-prod',{})
            seen=[]
            def compose(*args,**kwargs):
                import json
                value=json.loads(kwargs['override'].read_text())
                self.assertEqual(value,{'services':{'quiz-api':{'image':'sha256:abc'}}})
                seen.append(args)
                return CompletedProcess(args,0,'','')
            docker.compose=compose
            # Compose returned success; Docker inspect must independently confirm
            # that the expected immutable image was installed.
            docker.observe=lambda services:{'quiz-api':{'state':'created','image':'sha256:abc'}}
            docker.prepare('quiz-api','sha256:abc')
            self.assertEqual(seen,[('up','--no-start','--no-deps','--no-build','--pull','never','quiz-api')])
            self.assertFalse(list(Path(td).glob('update-image-*')))
    def test_slow_pull_does_not_hide_another_target(self):
        with tempfile.TemporaryDirectory() as td:
            fake=FakeDocker();blocked=threading.Event();release=threading.Event();other=threading.Event()
            original=fake.pull
            def pull(reference):
                if reference=='broken':
                    blocked.set();release.wait(2)
                value=original(reference)
                if reference=='other':other.set()
                return value
            fake.pull=pull
            ctl=ManagedUpdates(fake,Path(td),['broken','other'],threading.Event(),lambda s:True,lambda:False,lambda *a,**kw:None)
            ctl.set_desired(['broken','other'],'a');ctl.step(100)
            worker=threading.Thread(target=ctl.poll_once,args=(101,))
            worker.start()
            try:
                self.assertTrue(blocked.wait(1));self.assertTrue(other.wait(1))
            finally:
                release.set();worker.join(3)
            self.assertEqual(ctl.records['other']['target_image'],'sha256:new')

    def test_normal_compose_has_one_application_update_owner(self):
        import yaml
        root=Path(__file__).resolve().parents[1]
        for path in (root/'compose').glob('*.yaml'):
            for name,service in (yaml.safe_load(path.read_text()) or {}).get('services',{}).items():
                labels=service.get('labels',{})
                if isinstance(labels,list):labels=dict(x.split('=',1) for x in labels if '=' in x)
                self.assertNotEqual(str(labels.get('com.centurylinklabs.watchtower.enable','false')).lower(),'true',name)
        watch=yaml.safe_load((root/'compose/80-watchtower.yaml').read_text())['services']['watchtower']
        self.assertEqual(watch['profiles'],['legacy-watchtower'])
        self.assertEqual(watch['restart'],'no')

    def test_optional_services_do_not_block_core_but_analyzer_does(self):
        import agent
        for optional in ('telegram-quiz-bot','support-bot','ai-api','ai-worker','minecraft-api','rating-worker'):
            self.assertIn(optional,agent.OPTIONAL_SERVICES)
        for critical in ('identity-api','tasks-api','solutions-api','code-analyzer','execution-api'):
            self.assertNotIn(critical,agent.OPTIONAL_SERVICES)

if __name__=='__main__':unittest.main()
