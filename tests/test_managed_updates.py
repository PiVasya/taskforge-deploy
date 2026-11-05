import copy
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'cluster'))
from managed_updates import ManagedUpdates, bounded_run
from host_metrics import HostMetrics, cpu_delta, cpu_ticks, memory_values


class FakeDocker:
    def __init__(self):
        self.rows={s:{'state':'running','health':'healthy','image':'sha256:old','id':s} for s in ('broken','other')}
        self.registry={'broken':'sha256:new','other':'sha256:new'}
        self.calls=[]
        self.prepare_hook=None
    def retire_watchtower(self):self.calls.append(('retire',));return True
    def references(self,assigned):return {s:s for s in assigned}
    def pull(self,ref):
        self.calls.append(('pull',ref))
        value=self.registry[ref]
        if isinstance(value,Exception):raise value
        return value
    def observe(self,services):return {s:copy.deepcopy(self.rows[s]) for s in services}
    def preserve(self,s,image):self.calls.append(('preserve',s,image));return True
    def prepare(self,s,image):
        self.calls.append(('prepare',s,image))
        self.rows[s]={'state':'created','health':'starting','image':image,'id':s+'new'}
        if self.prepare_hook:self.prepare_hook()
    def start(self,s):self.calls.append(('start',s));self.rows[s].update(state='running',health='starting')
    def stop(self,s):self.calls.append(('stop',s));self.rows[s].update(state='exited')


class UpdatesTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)
        self.docker=FakeDocker();self.stop=threading.Event();self.events=[];self.fence=False;self.allowed=True
        self.controller=self.make()
        self.controller.set_desired(['broken','other'],'primary')
    def tearDown(self):self.tmp.cleanup()
    def make(self):
        return ManagedUpdates(self.docker,self.path,['broken','other','postgres'],self.stop,
                              lambda s:self.allowed,lambda:self.fence,lambda *a,**kw:self.events.append((a,kw)),poll=30,grace=30)
    def test_healthy_adoption_does_not_restart_or_stop_watchtower_every_tick(self):
        self.controller.step(100);self.controller.step(105)
        self.assertEqual([c for c in self.docker.calls if c[0]=='retire'],[('retire',)])
        self.assertFalse(any(c[0] in {'prepare','start','stop'} for c in self.docker.calls))
    def test_crash_does_not_starve_registry_or_new_image(self):
        self.docker.rows['broken'].update(state='restarting',health='unhealthy')
        self.controller.step(100)
        self.controller.poll_once(101)
        self.controller.step(102)
        self.assertIn(('prepare','broken','sha256:new'),self.docker.calls)
        self.assertIn(('prepare','other','sha256:new'),self.docker.calls)
        self.assertEqual(self.controller.records['broken']['target_image'],'sha256:new')
    def test_pull_failure_does_not_stop_other_service(self):
        self.docker.registry['broken']=RuntimeError('registry-down')
        self.controller.step(100);self.controller.poll_once(101);self.controller.step(102)
        self.assertIn(('prepare','other','sha256:new'),self.docker.calls)
        self.assertEqual(self.docker.rows['broken']['image'],'sha256:old')
        self.assertEqual(self.controller.records['broken']['pull_error'],'registry-down')
    def test_standby_stages_without_starting(self):
        self.controller.set_desired([],'standby')
        self.controller.step(100);self.controller.poll_once(101);self.controller.step(102)
        self.assertFalse(any(c[0]=='start' for c in self.docker.calls))
        self.assertTrue(all(row['state']=='created' for row in self.docker.rows.values()))
        self.assertEqual(self.controller.records['other']['status'],'staged')
    def test_fence_blocks_all_mutations_and_downloads(self):
        self.fence=True;self.controller.step(100);self.controller.poll_once(101)
        self.assertEqual(self.docker.calls,[])
    def test_demotion_during_prepare_never_starts_app(self):
        self.controller.step(100);self.controller.poll_once(101)
        self.docker.prepare_hook=lambda:self.controller.set_desired([],'standby')
        self.controller.step(102)
        self.assertFalse(any(c[0]=='start' for c in self.docker.calls))
    def test_authoritative_primary_gate_not_only_cached_role(self):
        self.controller.step(100);self.controller.poll_once(101);self.allowed=False
        self.controller.step(102)
        self.assertFalse(any(c[0]=='start' for c in self.docker.calls))
    def test_failed_candidate_rolls_back_and_next_digest_bypasses_backoff(self):
        self.controller.step(100);self.controller.poll_once(101);self.controller.step(102)
        self.docker.rows['broken'].update(state='restarting',health='unhealthy')
        self.docker.rows['other'].update(health='healthy')
        self.controller.step(135)
        self.assertEqual(self.docker.rows['broken']['image'],'sha256:old')
        self.assertEqual(self.controller.records['broken']['failed_image'],'sha256:new')
        self.docker.rows['broken'].update(health='healthy')
        self.controller.step(136)
        self.assertEqual(self.controller.records['broken']['status'],'rolled-back')
        self.docker.registry['broken']='sha256:fixed';self.controller.next_scan=0
        self.controller.poll_once(137);self.controller.step(138)
        self.assertEqual(self.docker.rows['broken']['image'],'sha256:fixed')
    def test_incident_once_across_restart_and_recovery_once(self):
        self.controller.records['broken']['error']='bad-health'
        self.controller._incidents(100);self.controller._incidents(161);self.controller._incidents(400)
        self.controller.persist(400)
        self.assertEqual(len(self.events),1)
        restored=self.make();restored._incidents(600)
        self.assertEqual(len(self.events),1)
        restored.records['broken']['error']='';restored._incidents(601);restored._incidents(632);restored._incidents(700)
        self.assertEqual(len(self.events),2)
        self.assertEqual(self.events[1][0][0],'update.recovered')
    def test_data_services_are_never_floating_apps(self):
        self.assertNotIn('postgres',self.controller.services)
    def test_command_timeout_is_bounded(self):
        result=bounded_run([sys.executable,'-c','import time;time.sleep(30)'],timeout=.1)
        self.assertEqual(result.returncode,124)
    def test_command_output_is_bounded(self):
        result=bounded_run([sys.executable,'-c','print("x"*100000)'],limit=1024)
        self.assertEqual(result.returncode,0);self.assertLessEqual(len(result.stdout),1024)


class MetricTests(unittest.TestCase):
    def test_cpu_separates_guest_and_wait(self):
        before=cpu_ticks('cpu 100 0 20 400 20 2 3 10 50 0')
        after=cpu_ticks('cpu 120 0 30 450 30 2 3 20 60 0')
        self.assertEqual(cpu_delta(before,after),(30.,10.,10.))
        self.assertEqual(cpu_delta(None,after),(None,None,None))
        self.assertEqual(cpu_delta(after,before),(None,None,None))
    def test_memory_uses_available_not_free(self):
        mem,swap=memory_values('MemTotal: 1000 kB\nMemFree: 20 kB\nMemAvailable: 600 kB\nSwapTotal: 100 kB\nSwapFree: 80 kB')
        self.assertEqual(mem['percent'],40);self.assertEqual(mem['used'],400*1024);self.assertEqual(swap['used'],20*1024)
        self.assertIsNone(memory_values('MemTotal: 1000 kB')[0]['used'])
    def test_missing_sensors_stay_null_history_bounded(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);proc=root/'proc';proc.mkdir()
            (proc/'stat').write_text('cpu 10 0 0 40 0 0 0 0')
            (proc/'meminfo').write_text('MemTotal: 1000 kB\nMemAvailable: 500 kB')
            sensor=HostMetrics(root,threading.Event(),proc=proc,sys=root/'sys',limit=3)
            for tick in range(5):sensor.sample(100+tick)
            snapshot=sensor.snapshot()
            self.assertEqual(len(snapshot['history']),3)
            self.assertIsNone(snapshot['temperature_c'])
    def test_only_cpu_temperature_selected(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);cpu=root/'sys/class/hwmon/hwmon0';gpu=root/'sys/class/hwmon/hwmon1'
            cpu.mkdir(parents=True);gpu.mkdir(parents=True)
            (cpu/'name').write_text('coretemp');(cpu/'temp1_input').write_text('67000');(cpu/'fan1_input').write_text('3000')
            (gpu/'name').write_text('amdgpu');(gpu/'temp1_input').write_text('90000')
            metrics=HostMetrics(root,threading.Event(),proc=root/'proc',sys=root/'sys')
            temperatures,fans=metrics.temperatures()
            self.assertEqual([r['c'] for r in temperatures],[67.]);self.assertEqual(fans[0]['rpm'],3000)

if __name__=='__main__':unittest.main()
