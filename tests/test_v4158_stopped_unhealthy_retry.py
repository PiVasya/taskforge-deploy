#!/usr/bin/env python3
"""Regression: stopped containers with stale Docker Health must be restarted.

v41.5.9 re-issued healthcheck-failed:exited/unhealthy at every retry deadline,
creating an endless backoff without calling docker start. All tests use a
fake Docker backend; no production containers are touched.
"""
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'cluster'))
from managed_updates import ManagedUpdates

class FakeDocker:
    def __init__(self):
        self.rows = {'ai-api': {'state': 'exited', 'health': 'unhealthy',
                                'image': 'sha256:live', 'id': 'ai-api',
                                'started_at': '2026-10-20T00:00:00Z', 'restarts': 0}}
        self.calls = []
    def retire_watchtower(self): return True
    def observe(self, services): return {s: dict(self.rows[s]) for s in services}
    def preserve(self, service, image): return True
    def start(self, service):
        self.calls.append(('start', service))
        self.rows[service].update(state='running', health='starting')
    def stop(self, service):
        self.calls.append(('stop', service))
        self.rows[service].update(state='exited', health='unhealthy')
    def cleanup(self, image): return True

class StoppedUnhealthyRetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.backend = FakeDocker()
        self.stop = threading.Event()
        self.allowed = True
        self.fenced = False
        self.controller = self.make()
        self.controller.set_desired(['ai-api'], 'routing-v1', reason='confirmed-primary')
        self.controller.records['ai-api'].update(
            target_image='sha256:live', current_image='sha256:live',
            applied_config='routing-v1')

    def make(self):
        return ManagedUpdates(self.backend, self.root, ['ai-api'], self.stop,
                              lambda s: self.allowed, lambda: self.fenced,
                              lambda *a, **kw: None, poll=300, grace=30)

    def test_cooldown_does_not_keep_increasing_when_container_is_stopped(self):
        r = self.controller.records['ai-api']
        r.update(status='retrying', error='healthcheck-failed:exited/unhealthy',
                 attempts=5, next_attempt=120, verify_deadline=0)
        self.controller.step(110)
        self.assertEqual(self.backend.calls, [])
        self.assertEqual(r['next_attempt'], 120)
        self.controller.step(120)
        self.assertEqual(self.backend.calls, [('start', 'ai-api')])
        self.assertEqual(r['attempts'], 5)
        self.assertEqual(r['last_start_reason'], 'desired-running-self-heal')

    def test_fast_crash_is_delayed_and_then_retried_once(self):
        self.controller.step(100)
        self.assertEqual(self.backend.calls, [('start', 'ai-api')])
        self.backend.rows['ai-api'].update(state='exited', health='unhealthy')
        self.controller.step(105)
        self.assertEqual(len(self.backend.calls), 1)
        self.controller.step(131)
        r = self.controller.records['ai-api']
        self.assertEqual(len(self.backend.calls), 1)
        self.assertTrue(r['error'].startswith('start-verification-failed:exited/'))
        self.assertGreater(r['next_attempt'], 131)
        due = r['next_attempt']
        self.controller.step(due - 1)
        self.assertEqual(len(self.backend.calls), 1)
        self.controller.step(due)
        self.assertEqual(len(self.backend.calls), 2)
        self.assertEqual(self.backend.calls[1], ('start', 'ai-api'))

    def test_standby_and_fence_never_revive_stopped_container(self):
        self.controller.set_desired([], 'routing-standby', reason='confirmed-replica')
        self.controller.step(100)
        self.assertEqual(self.backend.calls, [])
        self.controller.set_desired(['ai-api'], 'routing-v1', reason='confirmed-primary')
        self.fenced = True
        self.controller.step(200)
        self.assertEqual(self.backend.calls, [])
        self.fenced = False
        self.allowed = False
        self.controller.step(300)
        self.assertEqual(self.backend.calls, [])

    def test_old_backoff_cleared_once_on_version_upgrade(self):
        old = {'services': {'ai-api': {
            'error': 'healthcheck-failed:exited/unhealthy',
            'attempts': 7, 'next_attempt': 99999,
            'verify_deadline': 500,
            'target_image': 'sha256:live', 'current_image': 'sha256:live',
            'applied_config': 'routing-v1'}}, 'incident': {}}
        (self.root / 'application-updates.json').write_text(json.dumps(old))
        controller = self.make()
        r = controller.records['ai-api']
        self.assertEqual(r['next_attempt'], 0)
        self.assertEqual(r['verify_deadline'], 0)
        self.assertTrue(r['stopped_health_retry_migrated'])
        controller.set_desired(['ai-api'], 'routing-v1')
        controller.step(100)
        self.assertEqual(self.backend.calls, [('start', 'ai-api')])
        controller.persist(100)
        again = self.make()
        again_row = again.records['ai-api']
        again_row.update(next_attempt=777777, verify_deadline=0,
                         error='healthcheck-failed:exited/unhealthy')
        again.persist(102)
        preserved = self.make()
        self.assertEqual(preserved.records['ai-api']['next_attempt'], 777777)

if __name__ == '__main__':
    unittest.main(verbosity=2)
