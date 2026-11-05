#!/usr/bin/env python3
from __future__ import annotations
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
import agent  # noqa: E402


class FakeEdge:
    def __init__(self, state):
        self.state = dict(state)
    def update(self, eligible):
        return dict(self.state)


class NotificationNoiseTests(unittest.TestCase):
    def bare_agent(self):
        a = agent.Agent.__new__(agent.Agent)
        a.node_id = 'B'
        a.cfg = {
            'nodes': [
                {'id': 'A', 'wireguard': {'ip': '10.80.0.1'}, 'health_port': 9187},
                {'id': 'B', 'wireguard': {'ip': '10.80.0.2'}, 'health_port': 9187},
                {'id': 'C', 'wireguard': {'ip': '10.80.0.3'}, 'health_port': 9187},
            ]
        }
        a.events = []
        a.lock = threading.RLock()
        a.cloudflare_last_result = {'success': False}
        a.cloudflare_last_success = 0.0
        a.cloudflare_last_error = ''
        a._primary_timeline = '42'
        a._notice_state = {}
        return a

    def test_agent_urls_include_local_liveness(self):
        a = self.bare_agent()
        urls = a._agent_urls().split(';')
        self.assertEqual(urls, [
            'http://10.80.0.1:9187',
            'http://10.80.0.2:9187',
            'http://10.80.0.3:9187',
        ])

    def test_edge_ready_is_once_per_patroni_term_across_restart(self):
        success = {'success': True, 'status': 'synced', 'phase': 'ready'}
        with tempfile.TemporaryDirectory() as td, patch.object(agent, 'RUNTIME', Path(td)):
            first = self.bare_agent()
            first.edge_controller = FakeEdge(success)
            self.assertTrue(first._edge_refresh(True))
            self.assertEqual([e['kind'] for e in first.events], ['edge.public_ready'])
            state = json.loads((Path(td) / 'notification-state.json').read_text())
            self.assertEqual(state['edge_public_ready_term'], 'B|42')

            restarted = self.bare_agent()
            restarted._notice_state = restarted._load_notice_state()
            restarted.edge_controller = FakeEdge(success)
            self.assertTrue(restarted._edge_refresh(True))
            self.assertEqual(restarted.events, [])

            restarted.cloudflare_last_result = {'success': False}
            restarted._primary_timeline = '43'
            self.assertTrue(restarted._edge_refresh(True))
            self.assertEqual([e['kind'] for e in restarted.events], ['edge.public_ready'])

    def test_previous_primary_unreachable_is_tagged_to_old_node_and_deduped(self):
        with tempfile.TemporaryDirectory() as td, patch.object(agent, 'RUNTIME', Path(td)):
            a = self.bare_agent()
            clock=iter(range(0,1000,10))
            a.stop=__import__('unittest.mock',fromlist=['Mock']).Mock()
            a.stop.is_set.return_value=False
            a.stop.wait.return_value=False
            with patch.object(agent, 'http_json', return_value=(0, {})), patch.object(agent.time, 'monotonic', side_effect=lambda:next(clock)):
                a._probe_previous_primary('A', 'A>B|42')
                a._probe_previous_primary('A', 'A>B|42')
            self.assertEqual(len(a.events), 1)
            self.assertEqual(a.events[0]['kind'], 'cluster.previous_primary_unreachable')
            self.assertEqual(a.events[0]['node'], 'A')
            self.assertEqual(a.events[0]['severity'], 'error')

            with patch.object(agent, 'http_json', return_value=(200, {'ok': True, 'node': 'A'})):
                a._probe_previous_primary('A', 'A>B|43')
            self.assertEqual(len(a.events), 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
