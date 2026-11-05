"""Regression tests for v41.5.5 multi-image update and legacy-owner retirement."""
import copy
import tempfile
import threading
import unittest
import sys
from pathlib import Path
from subprocess import CompletedProcess

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'cluster'))
from managed_updates import DockerApplications, ManagedUpdates


class SimulatedDocker:
    def __init__(self, names):
        self.rows={name:{'state':'running','health':'healthy','image':'sha256:old','id':name} for name in names}
        self.registry={name:'sha256:current-'+name for name in names}
        self.pulled=[];self.started=[];self.prepared=[];self.preserved=[]
    def retire_watchtower(self):return True
    def references(self, names):return {name:name for name in names}
    def pull(self, name):
        self.pulled.append(name)
        value=self.registry[name]
        if isinstance(value, Exception): raise value
        return value
    def observe(self, names):return {s:copy.deepcopy(self.rows[s]) for s in names}
    def preserve(self, name, image):self.preserved.append((name,image));return True
    def prepare(self, name, image):
        self.prepared.append((name,image))
        self.rows[name]={'state':'created','health':'starting','image':image,'id':name+'-new'}
    def start(self, name):self.started.append(name);self.rows[name].update(state='running',health='healthy')
    def stop(self, name):self.rows[name]['state']='exited'
    def cleanup(self, image):return True

class FairRegistryTests(unittest.TestCase):
    def mk(self, backend, directory, poll=300):
        ctl=ManagedUpdates(backend, Path(directory), list(backend.rows), threading.Event(),
                           lambda name:True, lambda:False, lambda *args,**kwargs:None,poll=poll)
        ctl.set_desired(list(backend.rows),'primary')
        ctl.step(100)
        return ctl

    def test_all_services_reached_in_small_independent_batches(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker([f'service-{i:02d}' for i in range(19)])
            ctl=self.mk(backend,td)
            # 19 apps are not all queued behind one failed registry request.
            for t in (101, 102, 103, 104):
                ctl.poll_once(t)
                ctl.next_scan=0  # emulate next independent 5-second registry tick
            self.assertEqual(len(set(backend.pulled)),19)
            self.assertEqual(len(backend.pulled),19)
            self.assertGreater(ctl.scan_completed,0)
            ctl.step(105)
            self.assertEqual(len(backend.prepared),19)
            self.assertEqual(len(backend.started),19)
            self.assertEqual(ctl.snapshot()['failed_count'],0)

    def test_two_timeout_images_do_not_starve_other_apps(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['00-python','01-pascal']+[f'next-{i:02d}' for i in range(12)])
            backend.registry['00-python']=RuntimeError('registry-pull-failed:124')
            backend.registry['01-pascal']=RuntimeError('registry-pull-failed:124')
            ctl=self.mk(backend,td)
            for t in (101,102,103):
                ctl.poll_once(t);ctl.next_scan=0
            ctl.step(104)
            self.assertEqual(len(set(backend.pulled)),14)
            self.assertEqual(backend.rows['00-python']['image'],'sha256:old')
            self.assertEqual(backend.rows['01-pascal']['image'],'sha256:old')
            self.assertEqual(ctl.snapshot()['failed_count'],2)
            self.assertEqual(len(backend.prepared),12)
            self.assertGreater(ctl.records['00-python']['next_pull_at'],101)

    def test_last_good_is_not_replaced_by_one_healthy_tick(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['front']);ctl=self.mk(backend,td)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:old')
            ctl.poll_once(101);ctl.step(102)
            backend.rows['front'].update(health='healthy',started_at='image-start-1',restarts=0)
            ctl.step(105)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:old')
            self.assertEqual(ctl.records['front']['status'],'verifying')
            self.assertNotIn(('front','sha256:old'),backend.prepared)
            ctl.step(159)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:old')
            ctl.step(166)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:current-front')
            self.assertEqual(ctl.records['front']['status'],'healthy')

    def test_flapping_candidate_does_not_overwrite_old_rollback(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['front']);ctl=self.mk(backend,td)
            ctl.poll_once(101);ctl.step(102)
            backend.rows['front'].update(health='healthy',started_at='first')
            ctl.step(103)
            backend.rows['front'].update(health='starting')
            ctl.step(150)
            backend.rows['front'].update(health='healthy')
            ctl.step(151)
            self.assertEqual(ctl.records['front']['stable_since'],151)
            ctl.step(200)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:old')
            # Candidate fails after a few successful probes. At the health
            # deadline the earlier image must still be available to restore.
            backend.rows['front'].update(state='restarting',health='unhealthy')
            ctl.step(285)
            self.assertIn(('front','sha256:old'),backend.prepared)
            self.assertEqual(backend.rows['front']['image'],'sha256:old')

    def test_candidate_restart_resets_stable_health_timer(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['front']);ctl=self.mk(backend,td)
            ctl.poll_once(101);ctl.step(102)
            backend.rows['front'].update(health='healthy',started_at='first',restarts=0)
            ctl.step(104)
            backend.rows['front'].update(health='healthy',started_at='second',restarts=1)
            ctl.step(155)
            self.assertEqual(ctl.records['front']['stable_since'],155)
            self.assertEqual(ctl.records['front']['last_good_image'],'sha256:old')

    def test_legacy_updater_is_rechecked_periodically_not_every_tick(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['front']);count=[]
            backend.retire_watchtower=lambda:count.append('retire') or True
            ctl=self.mk(backend,td)
            ctl.step(105)
            self.assertEqual(len(count),1)
            ctl.step(160)
            self.assertEqual(len(count),2)

    def test_registry_status_reports_worker_failure_separately_from_site(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['front']);ctl=self.mk(backend,td)
            class DeadWorker:
                def is_alive(self):return False
            ctl.registry_thread=DeadWorker()
            snapshot=ctl.snapshot()
            self.assertFalse(snapshot['registry_thread_alive'])
            self.assertEqual(snapshot['mode'],'node-agent')
            self.assertEqual(snapshot['failed_count'],0)
            # status.py reports the worker failure separately from traffic_ready.
            status=(ROOT/'cluster/ops/current/status.py').read_text()
            self.assertIn('updater:registry-thread-exited',status)

    def test_failed_pull_retries_without_new_image_and_clears_failure(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['python-runner'])
            backend.registry['python-runner']=RuntimeError('registry-pull-failed:124')
            ctl=self.mk(backend,td,poll=30)
            ctl.poll_once(101)
            row=ctl.records['python-runner']
            self.assertEqual(row['next_pull_at'],131)
            self.assertEqual(row['pull_failures'],1)
            ctl.poll_once(110)
            self.assertEqual(len(backend.pulled),1)
            backend.registry['python-runner']='sha256:recovered'
            ctl.poll_once(132)
            self.assertEqual(ctl.records['python-runner']['pull_failures'],0)
            self.assertEqual(ctl.records['python-runner']['pull_error'],'')
            ctl.step(133)
            self.assertIn(('python-runner','sha256:recovered'),backend.prepared)

    def test_manual_check_ignores_previous_pull_backoff(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['pascal-runner']);ctl=self.mk(backend,td)
            backend.registry['pascal-runner']=RuntimeError('registry-pull-failed:124')
            ctl.poll_once(101)
            backend.registry['pascal-runner']='sha256:fixed'
            (Path(td)/'updates-check-request').write_text('check\n')
            ctl.poll_once(102)
            self.assertEqual(ctl.records['pascal-runner']['target_image'],'sha256:fixed')
            self.assertFalse((Path(td)/'updates-check-request').exists())

    def test_pull_state_persists_and_is_rechecked_after_restart(self):
        with tempfile.TemporaryDirectory() as td:
            backend=SimulatedDocker(['python-runner']);ctl=self.mk(backend,td)
            backend.registry['python-runner']=RuntimeError('registry-pull-failed:124')
            ctl.poll_once(101)
            restored=ManagedUpdates(backend,Path(td),['python-runner'],threading.Event(),
                                    lambda service:True,lambda:False,lambda *a,**kw:None)
            self.assertEqual(restored.records['python-runner']['pull_error'],'registry-pull-failed:124')
            self.assertEqual(restored.records['python-runner']['next_pull_at'],0)

class LegacyWatchtowerTests(unittest.TestCase):
    def test_retires_only_project_watchtower_without_force_or_volumes(self):
        with tempfile.TemporaryDirectory() as td:
            d=DockerApplications(td,td,'taskforge-prod',{})
            calls=[];removed=[]
            def cli(*args,**kwargs):
                calls.append(args)
                if args[0]=='ps':
                    return CompletedProcess(args,0,'' if removed else 'taskforge-prod-watchtower-1|exited\n','')
                if args[0]=='rm':removed.append(args[-1])
                return CompletedProcess(args,0,'','')
            d.cli=cli
            self.assertTrue(d.retire_watchtower())
            self.assertEqual(d.last_watchtower_state,'absent')
            self.assertEqual([x[0] for x in calls],['ps','ps','update','rm','ps','ps'])
            self.assertFalse(any('tgcapcha' in str(call) or '-v' in call for call in calls))

    def test_running_legacy_watchtower_disabled_before_stopping(self):
        with tempfile.TemporaryDirectory() as td:
            d=DockerApplications(td,td,'taskforge-prod',{})
            calls=[];removed=[]
            def cli(*args,**kwargs):
                calls.append(args)
                if args[0]=='ps':
                    return CompletedProcess(args,0,'' if removed else 'taskforge-prod-watchtower-1|running\n','')
                if args[0]=='rm':removed.append(args[-1])
                return CompletedProcess(args,0,'','')
            d.cli=cli
            self.assertTrue(d.retire_watchtower())
            self.assertEqual([x[0] for x in calls],['ps','ps','update','stop','rm','ps','ps'])

    def test_no_old_script_restarts_legacy_owner_or_requires_running(self):
        rehome=(ROOT/'cluster/ops/current/rehome-replica.sh').read_text()
        verify=(ROOT/'cluster/ops/current/verify.sh').read_text()
        self.assertNotIn('docker start "$wt"',rehome)
        self.assertNotIn('Watchtower is not running',verify)
        self.assertIn('legacy Watchtower is running alongside Node Agent',verify)
        migration=(ROOT/'cluster/ops/current/migrate.sh').read_text()
        self.assertIn('docker rm "$project-watchtower-1"',migration)
        self.assertNotIn('docker rm -v',migration)

    def test_disabled_onec_does_not_restart_healthy_apps_on_hotfix(self):
        data=(ROOT/'cluster/ops/current/apply-onec-runtime.sh').read_text()
        inactive=data.index('if [ "$onec_enabled" != true ]; then')
        pulls=data.index('cluster_compose_force pull "${services[@]}"')
        self.assertLess(inactive,pulls)
        self.assertIn("exit 0",data[inactive:pulls])

if __name__=='__main__': unittest.main()
