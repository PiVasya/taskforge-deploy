"""Regression for Primary B outages caused by a transient Patroni /cluster miss.

A missing /cluster response is not a confirmed replica or a positive conflicting
leader. The Node Agent must not publish desired=[] and the updater must not
stop the running application stack as if it were a warm standby.
"""
from __future__ import annotations

import tempfile
import threading
import time
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, PropertyMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
import agent
from managed_updates import ManagedUpdates


class Backend:
    def __init__(self):
        self.calls = []
        self.rows = {
            s: {'state': 'running', 'health': 'healthy', 'image': 'sha256:known',
                'id': s, 'restarts': 0, 'started_at': '2026-10-21T01:00:00Z'}
            for s in ('gateway', 'tasks-api')
        }

    def retire_watchtower(self):
        return True

    def observe(self, services):
        return {s: dict(self.rows[s]) for s in services}

    def preserve(self, service, image):
        return True

    def stop(self, service):
        self.calls.append(('stop', service))
        self.rows[service]['state'] = 'exited'

    def start(self, service):
        self.calls.append(('start', service))
        self.rows[service]['state'] = 'running'

    def prepare(self, service, image):
        self.calls.append(('prepare', service))
        self.rows[service].update(image=image, state='created')


class PrimaryClusterHoldTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.runtime = Path(self.tmp.name)
        self.backend = Backend()
        self.controller = ManagedUpdates(
            self.backend, self.runtime, ['gateway', 'tasks-api'],
            threading.Event(), lambda service: True, lambda: False,
            lambda *args, **kwargs: None,
        )
        self.controller.set_desired(['gateway', 'tasks-api'], 'unchanged-primary-routing')
        self.controller.step(100)

    def test_policy_distinguishes_absent_leader_from_positive_conflict(self):
        policy = agent.application_update_policy
        self.assertEqual(policy(True, 'primary', '', 'B', 'B', True, 'primary', 0)[0], 'hold')
        self.assertEqual(policy(True, 'primary', 'B', 'B', 'B', True, 'primary', 0)[0], 'primary')
        self.assertEqual(policy(True, 'primary', 'A', 'B', 'B', True, 'primary', 0)[0], 'stop')
        self.assertEqual(policy(True, 'standby', 'A', 'A', 'B', True, 'warm-standby', 0)[0], 'stop')
        self.assertEqual(policy(True, 'standby', 'A', 'A', 'B', True, 'assist', 0)[0], 'assist')
        self.assertEqual(policy(True, 'unknown', '', 'B', 'B', True, 'uncertain-grace', 2)[0], 'hold')
        self.assertEqual(policy(True, 'unknown', '', 'B', 'B', True, 'fenced-patroni-unknown', 3)[0], 'stop')
        self.assertEqual(policy(True, 'primary', '', 'B', 'B', False, 'primary', 0)[0], 'stop')

    def bare_agent(self):
        a = agent.Agent.__new__(agent.Agent)
        a.project = 'taskforge-prod'
        a.node_id = 'B'
        a.node = {'priority': 10, 'dcs_voter': True, 'app': {'profile': 'full', 'can_be_primary': True},
                  'wireguard': {'ip': '10.80.0.2'}, 'postgres': {}}
        a.cfg = {'cluster_name': 'taskforge-prod', 'nodes': [], 'shared_state_interval_seconds': 60}
        a.assigned = ['gateway', 'tasks-api']
        a.critical = ['gateway', 'tasks-api']
        a.optional = []
        a.assist = []
        a.assist_enabled = False
        a.last_seen_leader = 'B'
        a._leader_candidate = ''
        a._leader_candidate_count = 0
        a._baseline = True
        a._patroni_unknown_count = 0
        a._primary_cluster_hold_samples = 0
        a._primary_cluster_hold_total = 0
        a.events = []
        a.env = {}
        a.bundle_version = 'v41.5.9'
        a.bundle_revision = '410509'
        a.shared_ok = True
        a.last_shared_push = time.time()
        a.cloudflare_last_success = 0
        a.cloudflare_last_result = {'configured': False, 'success': True}
        a.edge_controller = Mock()
        a.edge_controller.snapshot.return_value = {'success': True}
        a.updater = self.controller
        a.patroni = Mock(return_value=(200, {'role': 'primary', 'state': 'running', 'timeline': 15}))
        a.patroni_cluster = Mock(return_value=(0, {}))
        rows = [{'service': s, 'status': 'running', 'health': 'healthy',
                 'restart': 0, 'oom': False} for s in a.assigned]
        a.app_status = Mock(return_value=(rows, 2, 2, 2))
        a._infra_status = Mock(return_value={'minio': True, 'redis': True, 'rabbitmq': True})
        a._tls_assets_ready = Mock(return_value=True)
        a._tls_live_ready = Mock(return_value=True)
        a._edge_refresh = Mock(return_value=True)
        a._write_local_routing = Mock(return_value=False)
        a._write_standby_routing = Mock(return_value=False)
        a.stop_apps = Mock()
        a.start_service_set = Mock()
        a._sql_request = Mock()
        a.host_snapshot = Mock(return_value=({}, {}, {}))
        a.docker_inventory = Mock(return_value=[])
        a._write_state = Mock()
        a._event = Mock()
        return a

    def test_real_agent_tick_holds_desired_on_one_cluster_timeout(self):
        a = self.bare_agent()
        with (patch.object(agent.Agent, 'quorum_enabled', new_callable=PropertyMock, return_value=True),
              patch.object(agent, 'RUNTIME', self.runtime),
              patch.object(agent, 'CONTROL_FENCE_FILE', self.runtime / 'control-fence')):
            a.tick()
        self.assertEqual(a.telemetry['ha']['role'], 'primary')
        self.assertEqual(a.telemetry['ha']['patroni_cluster_http'], 0)
        self.assertEqual(a.telemetry['ha']['app_desired_policy'], 'hold')
        self.assertEqual(a.telemetry['ha']['primary_cluster_hold_samples'], 1)
        self.assertEqual(self.controller.desired, {'gateway', 'tasks-api'})
        self.assertEqual(self.controller.config_version, 'unchanged-primary-routing')
        # No needless public-edge revocation or Cloudflare re-provisioning.
        self.assertNotIn(False, [call.args[0] for call in a._edge_refresh.call_args_list])
        self.controller.step(105)
        self.assertEqual(self.backend.calls, [])

    def test_repeated_cluster_timeouts_do_not_drain_primary_apps(self):
        a = self.bare_agent()
        with (patch.object(agent.Agent, 'quorum_enabled', new_callable=PropertyMock, return_value=True),
              patch.object(agent, 'RUNTIME', self.runtime),
              patch.object(agent, 'CONTROL_FENCE_FILE', self.runtime / 'control-fence')):
            for index in range(5):
                a.tick()
                self.controller.step(105 + index * 5)
            self.assertEqual(self.controller.desired, {'gateway', 'tasks-api'})
            self.assertEqual(a.last_seen_leader, 'B')
            self.assertFalse(any(call[0] == 'stop' for call in self.backend.calls))
            self.assertEqual(a.telemetry['ha']['primary_cluster_hold_samples'], 5)
            self.assertEqual(a.telemetry['ha']['primary_cluster_hold_total'], 5)
            a.patroni_cluster.return_value = (200, {'members': [{'name': 'B', 'role': 'leader'}]})
            for _ in range(2):
                a.tick()
        self.assertEqual(a.telemetry['ha']['app_desired_policy'], 'primary')
        self.assertEqual(a.telemetry['ha']['primary_cluster_hold_samples'], 0)
        self.assertEqual(self.controller.desired, {'gateway', 'tasks-api'})
        self.assertFalse(any(call[0] == 'stop' for call in self.backend.calls))

    def test_real_agent_tick_stops_on_positive_conflicting_leader(self):
        a = self.bare_agent()
        a.patroni_cluster.return_value = (200, {'members': [{'name': 'A', 'role': 'leader'}]})
        with (patch.object(agent.Agent, 'quorum_enabled', new_callable=PropertyMock, return_value=True),
              patch.object(agent, 'RUNTIME', self.runtime),
              patch.object(agent, 'CONTROL_FENCE_FILE', self.runtime / 'control-fence')):
            a.tick()
        self.assertEqual(a.telemetry['ha']['app_desired_policy'], 'stop')
        self.assertEqual(self.controller.desired, set())
        self.assertIn(False, [call.args[0] for call in a._edge_refresh.call_args_list])
        self.assertEqual(self.controller.desired_reason, 'positive-leader-conflict')
        self.controller.step(105)
        self.assertIn('positive-leader-conflict', self.controller.records['gateway']['last_stop_reason'])
        self.assertIn(('stop', 'gateway'), self.backend.calls)
        self.assertIn(('stop', 'tasks-api'), self.backend.calls)
        self.assertFalse(any(call[0] == 'start' for call in self.backend.calls))

    def test_real_agent_tick_stops_on_confirmed_replica(self):
        a = self.bare_agent()
        a.patroni.return_value = (200, {'role': 'replica', 'state': 'running'})
        a.patroni_cluster.return_value = (200, {'members': [{'name': 'A', 'role': 'leader'}]})
        with (patch.object(agent.Agent, 'quorum_enabled', new_callable=PropertyMock, return_value=True),
              patch.object(agent, 'RUNTIME', self.runtime),
              patch.object(agent, 'CONTROL_FENCE_FILE', self.runtime / 'control-fence')):
            a.tick()
        self.assertEqual(a.telemetry['ha']['app_desired_policy'], 'stop')
        self.assertEqual(self.controller.desired, set())
        self.controller.step(105)
        self.assertTrue(any(action[0] == 'stop' for action in self.backend.calls))


if __name__ == '__main__':
    unittest.main()
