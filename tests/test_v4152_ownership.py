"""v41.5.5 regression coverage for absent Watchtower and oversized telemetry."""
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
from managed_updates import DockerApplications, ManagedUpdates
sys.path.insert(0, str(ROOT / 'tests'))
from test_managed_updates import FakeDocker

class OwnershipTests(unittest.TestCase):
    def make(self):
        p = tempfile.TemporaryDirectory()
        self.addCleanup(p.cleanup)
        d = DockerApplications(p.name, p.name, 'taskforge-prod', {})
        return d

    def test_absent_legacy_is_success_even_if_docker_inspect_errors_differ(self):
        d = self.make(); commands = []
        def cli(*args, **kwargs):
            commands.append(args)
            if args[0] == 'inspect':
                raise AssertionError('do not infer existence from inspect stderr')
            return CompletedProcess(args, 0, '', '')
        d.cli = cli
        self.assertTrue(d.retire_watchtower())
        self.assertEqual(d.last_watchtower_state, 'absent')
        self.assertEqual(d.last_retirement_error, '')
        self.assertEqual([c[0] for c in commands], ['ps','ps'])
        self.assertIn('label=com.docker.compose.project=taskforge-prod', commands[0])
        self.assertIn('label=com.docker.compose.service=watchtower', commands[0])
        self.assertIn('name=^/taskforge-prod-watchtower-1$', commands[1])

    def test_docker_timeout_is_distinct_error_and_never_mutates_any_container(self):
        d = self.make(); calls=[]
        def cli(*args, **kwargs):
            calls.append(args)
            return CompletedProcess(args,124,'','Docker daemon is temporarily slow')
        d.cli=cli
        self.assertFalse(d.retire_watchtower())
        self.assertEqual(d.last_watchtower_state, 'unknown')
        self.assertIn('docker-ps-failed:124', d.last_retirement_error)
        self.assertEqual(len(calls),1)

    def test_legacy_label_catches_nonstandard_name_but_leaves_other_project(self):
        d=self.make();removed=[];calls=[]
        def cli(*args,**kwargs):
            calls.append(args)
            if args[0]=='ps':
                if removed:return CompletedProcess(args,0,'','')
                if 'label=com.docker.compose.service=watchtower' in args:
                    return CompletedProcess(args,0,'taskforge-prod-watchtower-2|running\n','')
                return CompletedProcess(args,0,'','')
            if args[0]=='rm':removed.append(args[-1])
            return CompletedProcess(args,0,'','')
        d.cli=cli
        self.assertTrue(d.retire_watchtower())
        self.assertEqual(removed,['taskforge-prod-watchtower-2'])
        self.assertNotIn('tgcapcha-prod-watchtower-1',str(calls))
        self.assertFalse(any('-v' in c for c in calls))

    def test_absent_legacy_unblocks_scanning_and_clears_incident(self):
        with tempfile.TemporaryDirectory() as td:
            d = DockerApplications(td,td,'taskforge-prod',{})
            d.cli = lambda *args,**kwargs: CompletedProcess(args,0,'','')
            backend=FakeDocker()
            backend.retire_watchtower=d.retire_watchtower
            events=[]
            ctl=ManagedUpdates(backend,Path(td),['broken','other'],threading.Event(),
                               lambda s:True,lambda:False,lambda *a,**kw:events.append(a),
                               poll=30,grace=30)
            ctl.set_desired(['broken','other'],'stable')
            ctl.step(100)
            self.assertTrue(ctl.retired)
            self.assertEqual(ctl.error,'')
            ctl.poll_once(101)
            self.assertIsNotNone(ctl.snapshot()['last_scan_at'])
            self.assertEqual(ctl.snapshot()['failed_count'],0)

    def test_failed_owner_probe_surfaces_actual_query_failure(self):
        with tempfile.TemporaryDirectory() as td:
            d=DockerApplications(td,td,'taskforge-prod',{})
            d.cli=lambda *args,**kwargs: CompletedProcess(args,124,'','timeout')
            class WithOwner(FakeDocker):
                @property
                def last_retirement_error(self):return d.last_retirement_error
            fake=WithOwner(); fake.retire_watchtower=d.retire_watchtower
            ctl=ManagedUpdates(fake,Path(td),['broken'],threading.Event(),
                               lambda s:True,lambda:False,lambda *a,**kw:None)
            ctl.set_desired(['broken'],'primary');ctl.step(100)
            self.assertFalse(ctl.retired)
            self.assertIn('legacy-owner-check-failed:docker-ps-failed:124',ctl.error)
            self.assertFalse(any(action[0] in {'prepare','start','stop'} for action in fake.calls))

class LargeTelemetryTests(unittest.TestCase):
    def test_200kb_telemetry_through_fd3_under_linux_arg_limit(self):
        with tempfile.TemporaryDirectory() as td:
            path=Path(td)/'telemetry.json'
            payload={'node':{'bundle_revision':'410505'},'host':{'history':[{'x':'y'*250} for _ in range(800)]}}
            path.write_text(json.dumps(payload))
            self.assertGreater(path.stat().st_size, 200000)
            snippet='''\
telemetry="$(cat "$1")"
python3 -S - 3<<<"$telemetry" <<'PYEND'
import json,os
payload=json.load(os.fdopen(3))
assert payload['node']['bundle_revision']=='410505'
assert len(payload['host']['history'])==800
print('LARGE_JSON_PASS')
PYEND
'''
            res=subprocess.run(['bash','-c',snippet,'_',str(path)],capture_output=True,text=True,timeout=10)
            self.assertEqual(res.returncode,0,res.stderr)
            self.assertIn('LARGE_JSON_PASS',res.stdout)

    def test_operational_scripts_never_exec_with_entire_telemetry_in_env(self):
        for name in ('migrate.sh','reconcile-sql.sh','rehome-replica.sh','verify.sh'):
            script=(ROOT/'cluster/ops/current'/name).read_text()
            self.assertNotIn('TELEMETRY="$payload"',script,name)
            self.assertNotIn('TELEMETRY="$last"',script,name)
            self.assertNotIn('TELEMETRY="$telemetry"',script,name)
            self.assertNotIn('REMOTE="$remote"',script,name)
            self.assertIn('os.fdopen(3)',script,name)
        migrate=(ROOT/'cluster/ops/current/migrate.sh').read_text()
        self.assertNotIn('last_telemetry=$last',migrate)
        self.assertIn('Node Agent updater ownership proof',migrate)
        sql=(ROOT/'cluster/ops/current/reconcile-sql.sh').read_text()
        self.assertIn('action=keep-running reason=already-healthy',sql)

if __name__ == '__main__':
    unittest.main()
