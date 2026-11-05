#!/usr/bin/env python3
"""v41.5.5 low-load standby regression and non-destructive rollout policy."""
import json
import os
import sys
import tempfile
import threading
import unittest
import subprocess
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
from managed_updates import DockerApplications, ManagedUpdates


class EfficientStandbyTests(unittest.TestCase):
    def controller(self, tmp):
        backend=Mock()
        return ManagedUpdates(backend, Path(tmp), ['front', 'solutions-api'], threading.Event(),
                              lambda _: False, lambda: False, lambda *a, **kw: None, poll=300)

    def test_unchanged_standby_does_not_trigger_extra_full_lifecycle_wake(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller=self.controller(tmp)
            controller.set_desired([], 'routes-stable')
            self.assertTrue(controller.wake.is_set())
            controller.wake.clear()
            for _ in range(200):
                controller.set_desired([], 'routes-stable')
            self.assertFalse(controller.wake.is_set())

    def test_promotion_or_demote_wakes_agent_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller=self.controller(tmp)
            controller.set_desired([], 'routes-stable')
            controller.wake.clear()
            controller.set_desired(['front'], 'routes-primary')
            self.assertTrue(controller.wake.is_set())
            controller.wake.clear()
            controller.set_desired([], 'routes-stable')
            self.assertTrue(controller.wake.is_set())

    def test_routing_change_wakes_even_when_desired_services_same(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller=self.controller(tmp)
            controller.set_desired([], 'routes-A')
            controller.wake.clear()
            controller.set_desired([], 'routes-B')
            self.assertTrue(controller.wake.is_set())

    def backend(self, tmp):
        root=Path(tmp)/'control'
        for d in ('compose', 'cluster'):
            (root/d).mkdir(parents=True)
        (root/'compose.sh').write_text('#!/bin/sh\n')
        (root/'compose'/'00-storage.yaml').write_text('services: {}\n')
        return DockerApplications(root, Path(tmp)/'runtime', 'taskforge-prod',
                                  {'TASKFORGE_CONFIG_ROOT':str(Path(tmp)/'config'),
                                   'TASKFORGE_ENV_FILE':str(Path(tmp)/'config'/'taskforge.env')})

    def test_registry_batches_reuse_one_compose_config_within_sweep(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=self.backend(tmp)
            result=subprocess.CompletedProcess([], 0, json.dumps({'services':{
                'front':{'image':'ghcr.io/taskforge/front:develop'},
                'solutions-api':{'image':'ghcr.io/taskforge/solutions-api:develop'},
            }}), '')
            with patch.object(backend, 'compose', return_value=result) as compose:
                self.assertEqual(backend.references(['front']),{'front':'ghcr.io/taskforge/front:develop'})
                self.assertEqual(backend.references(['solutions-api']),{'solutions-api':'ghcr.io/taskforge/solutions-api:develop'})
            self.assertEqual(compose.call_count, 1)
            self.assertEqual(backend.compose_counters()['config'], 1)
            self.assertEqual(backend.compose_counters()['reference_cache_hits'], 1)

    def test_file_change_invalidates_config_cache_immediately(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=self.backend(tmp)
            result=subprocess.CompletedProcess([], 0, json.dumps({'services':{'front':{'image':'v1'}}}), '')
            with patch.object(backend, 'compose', return_value=result) as compose:
                self.assertEqual(backend.references(['front'])['front'],'v1')
                config=backend.root/'compose'/'00-storage.yaml'
                config.write_text('services: {hello: {image: v2}}\n')
                result.stdout=json.dumps({'services':{'front':{'image':'v2'}}})
                self.assertEqual(backend.references(['front'])['front'],'v2')
            self.assertEqual(compose.call_count, 2)

    def test_expired_config_cache_rerenders(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=self.backend(tmp)
            result=subprocess.CompletedProcess([],0,'{"services":{"front":{"image":"v1"}}}','')
            with patch.object(backend,'compose',return_value=result) as compose:
                with patch('managed_updates.time.monotonic', side_effect=[100,191]):
                    backend.references(['front']); backend.references(['front'])
            self.assertEqual(compose.call_count, 2)

    def test_failed_compose_config_is_never_cached(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend=self.backend(tmp)
            result=subprocess.CompletedProcess([],1,'','failed')
            with patch.object(backend,'compose',return_value=result) as compose:
                for _ in range(2):
                    with self.assertRaisesRegex(RuntimeError,'compose-config-failed'):
                        backend.references(['front'])
            self.assertEqual(compose.call_count, 2)

    def test_rabbitmq_real_probe_frequency_drops_sixfold(self):
        text=(ROOT/'compose'/'00-storage.yaml').read_text()
        fragment=text.split('  rabbitmq:\n',1)[1].split('\n  redis:',1)[0]
        for value in ('rabbitmq-diagnostics', 'interval: 30s', 'timeout: 10s',
                      'retries: 10', 'start_period: 20s', 'rabbitmq-data:/var/lib/rabbitmq'):
            self.assertIn(value,fragment)
        self.assertNotIn('interval: 5s', fragment)
        self.assertEqual(3600//5, 6*(3600//30))

    def test_opt_in_rabbitmq_change_only_when_warm_standby(self):
        source=(ROOT/'cluster/ops/current/optimize-rabbitmq-standby.sh').read_text()
        for item in ("h.get('role')=='standby'", "h.get('app_mode')=='warm-standby'",
                     "h.get('hot_start_ready') is True", "int(d.get('running_app_count', -1))==0", "u.get('status')=='maintenance'",
                     "u.get('maintenance_ack_token')", 'reconciled',
                     'flock -n 9', 'old_volume', 'new_volume', 'updater-maintenance.json',
                     'up -d --no-deps --pull never rabbitmq', '30000000000'):
            self.assertIn(item,source)
        self.assertNotIn('docker volume rm',source)
        self.assertNotIn(' down -v',source)
        self.assertNotIn('docker rm -v',source)

    def test_agent_retains_fast_failover_tick(self):
        source=(ROOT/'cluster/agent.py').read_text()
        self.assertIn('self.stop.wait(5)', source)
        source=(ROOT/'cluster/managed_updates.py').read_text()
        self.assertIn('self.wake.wait(5)',source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
