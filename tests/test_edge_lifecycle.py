#!/usr/bin/env python3
"""Offline contract tests. No Cloudflare, ACME CA or production Docker calls."""
from __future__ import annotations

import contextlib
import copy
import fcntl
import http.server
import importlib.util
import json
import os
from pathlib import Path
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
import cloudflare_dns as cf
import edge_lifecycle as el
from edge_route import PublicRoute, RouteError, normalize_hosts
import agent


class FakeOps:
    def __init__(self, *args):
        self.policy = el.Policy(settle=15, route_timeout=25, interval=5,
                                confirmations=3, tls_timeout=10)
        self.now = 0
        self.events = []
        self.configured = True
        self.node = 'B'
        self.http_only = False
        self.token = ''
        self.http = [True, True, True]
        self.https = [True]
        self.before = None
        self.cert = None
        self.fail_dns = False
        self.cert_failure = False
        self.guard_hook = None
        self.cert_calls = 0
        self.restart_calls = 0
        self.route_calls = []
        self.force_not_due = False
        self.tls = True
    def guard(self):
        if self.guard_hook:
            self.guard_hook(self)
    @contextlib.contextmanager
    def exclusive(self):
        self.events.append('lock')
        yield
    def core_ready(self):
        self.guard(); self.events.append('core')
    def reconcile(self):
        self.guard(); self.events.append('dns')
        if self.fail_dns: raise el.Pending('Cloudflare HTTP 429')
        return {'success': True, 'changed': ['taskforge.by'], 'target_ip': '2.2.2.2'}
    def pause(self, seconds):
        self.events.append(('sleep', seconds)); self.now += seconds; self.guard()
    def route_probe(self, scheme='http'):
        self.guard(); self.events.append(scheme); self.route_calls.append((scheme, self.now))
        values = self.http if scheme == 'http' else self.https
        value = values.pop(0) if len(values) > 1 else values[0]
        return {'ok': value, 'hosts': [{'host': 'taskforge.by', 'ok': value, 'reason': '' if value else 'old origin'},
                                      {'host': 'ct.taskforge.by', 'ok': value, 'reason': '' if value else 'old origin'}]}
    def certificate(self):
        self.guard(); self.events.append('certificate'); return self.cert
    def cert_due(self, cert):
        return not self.force_not_due
    def cert_wait(self): return 300
    def certbot(self):
        self.guard(); self.events.append('certbot'); self.cert_calls += 1
        if self.cert_failure: raise el.Pending('CA unavailable')
        self.cert = {'fingerprint': 'new', 'not_after_epoch': time.time() + 86400 * 90}
        self.guard(); return self.cert
    def local_tls(self): self.guard(); self.events.append('local_tls'); return self.tls
    def restart_gateway(self):
        self.guard(); self.events.append('restart'); self.restart_calls += 1; self.tls = True


class PipelineTests(unittest.TestCase):
    def run_ops(self, ops):
        states = []
        result = el.Runner(ops, states.append, clock=lambda: ops.now).run()
        return result, states
    def test_exact_order_new_certificate(self):
        o = FakeOps(); result, states = self.run_ops(o)
        self.assertTrue(result['success'])
        self.assertEqual(o.cert_calls, 1)
        self.assertEqual(o.restart_calls, 1)
        self.assertEqual(o.route_calls[:3], [('http', 15), ('http', 20), ('http', 25)])
        self.assertLess(o.events.index('dns'), o.events.index('http'))
        self.assertLess(o.events.index('http'), o.events.index('certbot'))
        self.assertLess(o.events.index('certbot'), o.events.index('restart'))
        self.assertLess(o.events.index('restart'), o.events.index('https'))
        self.assertEqual(states[-1]['phase'], 'ready')
    def test_dns_failure_cannot_reach_certbot(self):
        o = FakeOps(); o.fail_dns = True
        with self.assertRaises(el.Pending): self.run_ops(o)
        self.assertEqual(o.cert_calls, 0); self.assertEqual(o.route_calls, [])
    def test_http_wrong_node_never_reaches_certbot(self):
        o = FakeOps(); o.http = [False]
        with self.assertRaises(el.Pending): self.run_ops(o)
        self.assertEqual(o.cert_calls, 0); self.assertLessEqual(o.now, 40)
    def test_http_confirmations_reset_after_any_failed_round(self):
        o = FakeOps(); o.policy = el.Policy(route_timeout=60); o.http = [True, False, True, True, True]
        result, states = self.run_ops(o)
        self.assertTrue(result['success'])
        counts = [x['route_confirmations'] for x in states if x.get('phase') == 'route-check']
        self.assertEqual(counts, [1, 0, 1, 2, 3])
    def test_demotion_during_settle_stops_certbot(self):
        o = FakeOps()
        def hook(o):
            if o.now >= 15: raise el.Cancelled('lost Primary')
        o.guard_hook = hook
        with self.assertRaises(el.Cancelled): self.run_ops(o)
        self.assertEqual(o.cert_calls, 0); self.assertFalse(o.route_calls)
    def test_demotion_during_route_proof_stops_certbot(self):
        o = FakeOps()
        def hook(o):
            if len(o.route_calls) >= 2: raise el.Cancelled('lost Primary')
        o.guard_hook = hook
        with self.assertRaises(el.Cancelled): self.run_ops(o)
        self.assertEqual(o.cert_calls, 0)
    def test_demotion_during_certbot_prevents_gateway_restart(self):
        o = FakeOps()
        def hook(o):
            if o.cert_calls: raise el.Cancelled('lost Primary while issuing')
        o.guard_hook = hook
        with self.assertRaises(el.Cancelled): self.run_ops(o)
        self.assertEqual(o.restart_calls, 0)
    def test_existing_cert_reused_without_restart_or_ca_request(self):
        o = FakeOps(); o.cert = {'fingerprint': 'old', 'not_after_epoch': time.time() + 86400*80}; o.force_not_due = True
        result, _ = self.run_ops(o)
        self.assertTrue(result['success']); self.assertEqual(o.cert_calls, 0); self.assertEqual(o.restart_calls, 0)
        self.assertEqual(len(o.route_calls), 4)
    def test_existing_cert_http_only_gateway_is_restarted(self):
        o = FakeOps(); o.cert = {'fingerprint': 'old', 'not_after_epoch': time.time()+86400}; o.force_not_due = True; o.tls = False
        self.run_ops(o)
        self.assertEqual(o.cert_calls, 0); self.assertEqual(o.restart_calls, 1)
    def test_ca_failure_without_certificate_is_not_ready(self):
        o = FakeOps(); o.cert_failure = True
        with self.assertRaisesRegex(el.Pending, 'CA unavailable'): self.run_ops(o)
        self.assertEqual(o.restart_calls, 0)
    def test_failed_renewal_preserves_valid_certificate_but_warns(self):
        o = FakeOps(); o.cert = {'fingerprint': 'old', 'not_after_epoch': time.time()+86400}; o.cert_failure = True
        result, _ = self.run_ops(o)
        self.assertTrue(result['success']); self.assertIn('CA unavailable', result['warning']); self.assertEqual(o.restart_calls, 0)
    def test_acme_retry_cooldown_blocks_new_issuance(self):
        o = FakeOps(); o.force_not_due = True
        with self.assertRaisesRegex(el.Pending, 'cooldown'): self.run_ops(o)
        self.assertEqual(o.cert_calls, 0)
    def test_https_failure_never_sets_ready(self):
        o = FakeOps(); o.https = [False]
        states = []
        with self.assertRaises(el.Pending): el.Runner(o, states.append, clock=lambda: o.now).run()
        self.assertFalse(any(x.get('success') for x in states))
    def test_unconfigured_edge_does_not_invent_dns_success(self):
        o = FakeOps(); o.configured = False
        result, _ = self.run_ops(o)
        self.assertTrue(result['success']); self.assertFalse(result['dns_synced']); self.assertEqual(result['status'], 'unconfigured')
        self.assertEqual(o.route_calls[0][1], 0)
    def test_http_profile_does_not_issue_tls_certificate(self):
        o = FakeOps(); o.http_only = True
        result, _ = self.run_ops(o)
        self.assertTrue(result['success']); self.assertTrue(result['http_only']); self.assertEqual(o.cert_calls, 0)
    def test_policy_rejects_invalid_timeouts_and_weak_confirmation_count(self):
        for env in ({'TASKFORGE_EDGE_SETTLE_SECONDS': 'bad'}, {'TASKFORGE_EDGE_ROUTE_CONFIRMATIONS': '1'},
                    {'TASKFORGE_ACME_ROUTE_WAIT_SECONDS': '15', 'TASKFORGE_EDGE_ROUTE_CONFIRMATIONS': '8'}):
            with self.assertRaises(el.Pending): el.Policy.from_env(env)


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.webroot = Path(self.temp.name); self.calls = []
        self.cf_headers = 'CF-Ray: proof-AMS\r\nCF-Cache-Status: DYNAMIC\r\n'
        self.status = '200'; self.bad_body = False; self.raise_cmd = False; self.wrong_tls_node = False
    def command(self, args, timeout):
        self.calls.append(args)
        self.assertEqual(args[0:2], ['curl', '-q'])
        self.assertNotIn('-k', args); self.assertNotIn('--resolve', args); self.assertNotIn('-L', args)
        if self.raise_cmd: raise el.Cancelled('stop')
        u = urlsplit(args[-1])
        if u.scheme == 'http':
            marker = self.webroot / u.path.lstrip('/')
            body = marker.read_text()
        else:
            self.assertEqual(u.path, '/ha/live')
            body = json.dumps({'node': 'A' if self.wrong_tls_node else 'B', 'status': 'ok', 'service': 'gateway'})
        if self.bad_body: body = 'old-node-marker'
        Path(args[args.index('-o')+1]).write_text(body)
        Path(args[args.index('-D')+1]).write_text(f'HTTP/1.1 {self.status} Result\r\n'+self.cf_headers+'\r\n')
        return subprocess.CompletedProcess(args, 0, self.status, '')
    def route(self): return PublicRoute(self.webroot, ['taskforge.by', 'ct.taskforge.by'], 'B', True, self.command)
    def test_unique_nonce_all_hosts_and_cleanup(self):
        r=self.route(); self.assertTrue(r.probe()['ok']); self.assertTrue(r.probe()['ok'])
        self.assertEqual(len(self.calls), 4)
        self.assertNotEqual(urlsplit(self.calls[0][-1]).path, urlsplit(self.calls[2][-1]).path)
        self.assertFalse(list((self.webroot/'.well-known/acme-challenge').iterdir()))
    def test_wrong_body_is_rejected(self):
        self.bad_body=True; self.assertFalse(self.route().probe()['ok'])
    def test_cf_response_required(self):
        self.cf_headers=''; self.assertFalse(self.route().probe()['ok'])
    def test_cache_hit_rejected_even_with_matching_body(self):
        self.cf_headers='CF-Ray: proof\r\nCF-Cache-Status: HIT\r\n'; self.assertFalse(self.route().probe()['ok'])
    def test_redirect_is_not_mistaken_for_route_proof(self):
        self.status='301'; self.cf_headers += 'Location: https://taskforge.by/\r\n'
        result=self.route().probe(); self.assertFalse(result['ok']); self.assertIn('redirect', result['hosts'][0]['reason'])
    def test_marker_cleanup_on_cancellation(self):
        self.raise_cmd=True
        with self.assertRaises(el.Cancelled): self.route().probe()
        self.assertFalse(list((self.webroot/'.well-known/acme-challenge').iterdir()))
    def test_https_checks_gateway_identity_not_nonexistent_https_acme(self):
        self.assertTrue(self.route().probe('https')['ok'])
        self.wrong_tls_node=True; self.assertFalse(self.route().probe('https')['ok'])
    def test_challenge_symlink_is_rejected(self):
        elsewhere=self.webroot/'elsewhere'; elsewhere.mkdir(); (self.webroot/'.well-known').symlink_to(elsewhere)
        with self.assertRaises(RouteError): self.route().probe()
    def test_invalid_host_cannot_inject_url_or_path(self):
        for host in ['https://test.by', 'test.by:8080', 'a/b', 'a@b', '*.by', 'a..by']:
            with self.assertRaises(RouteError): normalize_hosts([host])


class MockCFClient:
    def __init__(self, rows): self.rows=copy.deepcopy(rows); self.calls=[]; self.fail_second=False; self.timeout_after_write=False
    def records_by_name(self, zone, name): return [copy.deepcopy(x) for x in self.rows.values() if x['name']==name]
    def record(self, zone, id): return copy.deepcopy(self.rows[id])
    def patch_content(self, zone, id, ip):
        self.calls.append((id,ip))
        if self.fail_second and len(self.calls)==2: raise cf.CFError('API 500')
        self.rows[id]['content']=ip
        if self.timeout_after_write and len(self.calls)==1: raise cf.CFError('response lost after commit')
        return copy.deepcopy(self.rows[id])


class CFTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'node-id').write_text('B')
        self.cluster={'nodes':[{'id':n,'public_host':f'{i}.{i}.{i}.{i}','app':{'profile':'lite' if n=='C' else 'full','can_be_primary':True},'wireguard':{'ip':f'10.80.0.{i}'}}
                               for i,n in enumerate('ABC',1)]}
        self.cp=self.root/'cluster.json'; self.cp.write_text(json.dumps(self.cluster))
        self.records=[{'id':'1','name':'taskforge.by','type':'A','proxied':True}, {'id':'2','name':'ct.taskforge.by','type':'A','proxied':True}]
        self.config={'zone_id':'zone','managed_hosts':['taskforge.by','ct.taskforge.by'],'public_candidates':cf.public_candidates(self.cluster),'records':self.records,'dependent_hosts':[]}
        self.client=MockCFClient({r['id']:{**r,'content':'1.1.1.1'} for r in self.records})
        self.args=SimpleNamespace(config=str(self.cp),runtime=str(self.root),node='B',reason='test')
        self.p1=patch.object(cf,'load_installed',return_value=(self.client,self.config)); self.p1.start(); self.addCleanup(self.p1.stop)
        self.p2=patch.object(cf,'exact_one_leader',return_value={'timeline':5}); self.p2.start(); self.addCleanup(self.p2.stop)
    def test_reconcile_then_idempotent_noop(self):
        result=cf.reconcile(self.args); self.assertTrue(result['success']); self.assertEqual(len(self.client.calls),2)
        self.assertEqual(cf.reconcile(self.args)['changed'],[]); self.assertEqual(len(self.client.calls),2)
    def test_partial_failure_rolls_back_only_while_still_primary(self):
        self.client.fail_second=True
        with self.assertRaises(cf.CFError): cf.reconcile(self.args)
        self.assertTrue(all(row['content']=='1.1.1.1' for row in self.client.rows.values()))
    def test_lost_api_response_is_included_in_rollback(self):
        self.client.timeout_after_write=True
        with self.assertRaises(cf.CFError): cf.reconcile(self.args)
        self.assertEqual(self.client.rows['1']['content'],'1.1.1.1')
    def test_lost_leadership_stops_remaining_writes_and_rollback(self):
        def guard():
            if self.client.calls: raise el.Cancelled('lost leader')
        with self.assertRaises(cf.CFError): cf.reconcile(self.args,guard=guard)
        self.assertEqual(self.client.calls,[('1','2.2.2.2')])
    def test_fence_prevents_dns_changes(self):
        (self.root/'migration-fence').touch()
        with self.assertRaises(cf.CFError): cf.reconcile(self.args)
        self.assertFalse(self.client.calls)
    def test_duplicate_aaaa_unknown_ip_and_disabled_proxy_block(self):
        original=copy.deepcopy(self.client.rows)
        for kind in ('duplicate','aaaa','external','proxy'):
            self.client.rows=copy.deepcopy(original)
            if kind=='duplicate': self.client.rows['3']={**self.client.rows['1'],'id':'3'}
            if kind=='aaaa': self.client.rows['3']={**self.client.rows['1'],'id':'3','type':'AAAA'}
            if kind=='external': self.client.rows['1']['content']='9.9.9.9'
            if kind=='proxy': self.client.rows['1']['proxied']=False
            with self.assertRaises(cf.CFError): cf.reconcile(self.args)
        self.assertFalse(self.client.calls)
    def test_c_lite_can_update_edge(self):
        self.args.node='C'; (self.root/'node-id').write_text('C')
        result=cf.reconcile(self.args); self.assertEqual(result['target_node'],'C'); self.assertEqual(result['target_ip'],'3.3.3.3')
    def test_cname_loop_rejected(self):
        self.client.rows.update({'x':{'id':'x','name':'x.by','type':'CNAME','content':'y.by','proxied':True},'y':{'id':'y','name':'y.by','type':'CNAME','content':'x.by','proxied':True}})
        with self.assertRaises(cf.CFError): cf.choose_record_plan(self.client,'zone',['taskforge.by','x.by','y.by'],self.config['public_candidates'])
    def test_local_lock_prevents_concurrent_dns_writer(self):
        with (self.root/'cloudflare-dns.lock').open('a') as f:
            fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(cf.CFError): cf.reconcile(self.args)
        self.assertFalse(self.client.calls)


class SystemOpsTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); (self.root/'enabled').touch(); (self.root/'node-id').write_text('A')
        self.cp=self.root/'cluster.json'; self.ep=self.root/'env'
        self.cp.write_text(json.dumps({'nodes':[{'id':'A','wireguard':{'ip':'127.0.0.1'},'app':{'profile':'full','can_be_primary':True}}]}))
        self.ep.write_text('DOMAIN=test.invalid\nCT_DOMAIN=ct.test.invalid\n')
        self.ops=el.SystemOps(ROOT,self.cp,self.ep,self.root,threading.Event(),['gateway'])
    def test_directory_instead_of_both_cf_files_is_not_unconfigured(self):
        (self.root/'cloudflare-api-token').mkdir();(self.root/'cloudflare-failover.json').mkdir()
        with self.assertRaises(el.Pending):el.SystemOps(ROOT,self.cp,self.ep,self.root,threading.Event(),['gateway'])
        with self.assertRaises(cf.CFError):cf.load_installed(SimpleNamespace(token_path=str(self.root/'cloudflare-api-token'),cf_config=str(self.root/'cloudflare-failover.json')))
    def test_guard_checks_timeline_and_primary_even_when_agent_claims_primary(self):
        with patch.object(cf,'exact_one_leader',side_effect=[{'timeline':1},{'timeline':2}]):
            self.ops.guard()
            with self.assertRaises(el.Cancelled): self.ops.guard()
    def test_guard_does_not_trust_stale_config(self):
        self.ep.write_text(self.ep.read_text()+'OTHER=value\n')
        with self.assertRaises(el.Cancelled): self.ops.guard()
    def test_guard_respects_fence(self):
        (self.root/'join-rehome-fence').touch()
        with self.assertRaises(el.Cancelled): self.ops.guard()
    def test_guard_rejects_missing_timeline(self):
        with patch.object(cf,'exact_one_leader',return_value={}):
            with self.assertRaises(el.Cancelled): self.ops.guard()
    def test_certbot_retry_ledger_survives_new_process(self):
        self.ops.ledger={'last_attempt_epoch':time.time(),'next_certbot_epoch':time.time()+900}; self.ops._save_ledger()
        other=el.SystemOps(ROOT,self.cp,self.ep,self.root,threading.Event(),['gateway'])
        self.assertFalse(other.cert_due(None)); self.assertGreater(other.cert_wait(),850)
        self.assertEqual(os.stat(other.ledger_path).st_mode & 0o777,0o600)
    def test_partial_token_configuration_is_fail_closed(self):
        (self.root/'cloudflare-api-token').write_text('test')
        with self.assertRaises(el.Pending): el.SystemOps(ROOT,self.cp,self.ep,self.root,threading.Event(),['gateway'])
    def test_guarded_command_is_cancelled_and_child_reaped(self):
        self.ops.guard=lambda: None
        timer=threading.Timer(0.3,self.ops.cancel.set); timer.start(); self.addCleanup(timer.cancel)
        before=time.monotonic()
        with self.assertRaises(el.Cancelled): self.ops.command([sys.executable,'-c','import time;time.sleep(60)'],timeout=30)
        self.assertLess(time.monotonic()-before,4)
    def test_local_tls_uses_ca_validation_and_both_domains(self):
        calls=[]
        self.ops.command=lambda args,timeout:(calls.append(args) or subprocess.CompletedProcess(args,0,'ok',''))
        self.assertTrue(self.ops.local_tls()); self.assertEqual(len(calls),2)
        self.assertTrue(all('-k' not in x and '--insecure' not in x for x in calls))


class SupervisorTests(unittest.TestCase):
    def test_slow_edge_worker_does_not_block_live_http_or_telemetry_and_cancels(self):
        with tempfile.TemporaryDirectory() as raw:
            runtime=Path(raw); (runtime/'readiness').mkdir(); (runtime/'readiness/traffic-ready').touch()
            waiting=threading.Event(); release=threading.Event()
            class Slow(FakeOps):
                def __init__(self,*args): super().__init__(); self.cancel=args[4]
                def pause(self,seconds):
                    waiting.set()
                    while not release.wait(0.01):
                        if self.cancel.is_set(): raise el.Cancelled('demotion')
                    super().pause(seconds)
            supervisor=el.Supervisor(ROOT, runtime/'cfg',runtime/'env',runtime,['gateway'],factory=Slow)
            supervisor.update(True); self.assertTrue(waiting.wait(2))
            obj=agent.Agent.__new__(agent.Agent)
            obj.node_id='B';obj.bundle_revision='57';obj.edge_controller=supervisor
            obj._cloudflare_configured=lambda:True
            obj.telemetry={'ha':{'reconciled':True,'traffic_ready':True},'node':{'id':'B'},'edge':{}}
            class Handler(agent.Handler): pass
            Handler.agent=obj
            server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                for path in ['/ha/live','/ha/telemetry']:
                    before=time.monotonic()
                    with urlopen(f'http://127.0.0.1:{server.server_port}{path}',timeout=1) as r: doc=json.load(r)
                    self.assertLess(time.monotonic()-before,1)
                    if path.endswith('telemetry'): self.assertFalse(doc['ha']['traffic_ready'])
                state=supervisor.update(False)
                self.assertFalse(state['success']);self.assertFalse((runtime/'readiness/traffic-ready').exists())
                release.set();supervisor.thread.join(2)
                self.assertFalse(supervisor.snapshot()['success'])
            finally:
                release.set();supervisor.close();server.shutdown();server.server_close();thread.join(2)
    def test_stale_proof_expires(self):
        with tempfile.TemporaryDirectory() as raw:
            now=[0.0]; supervisor=el.Supervisor(ROOT,Path(raw)/'c',Path(raw)/'e',Path(raw),[],clock=lambda:now[0])
            supervisor.eligible=True;supervisor.ready_at=0;supervisor.state={'success':True,'configured':True,'status':'synced','phase':'ready'}
            self.assertTrue(supervisor.snapshot()['success']);now[0]=181;self.assertFalse(supervisor.snapshot()['success'])


class RealCurlTests(unittest.TestCase):
    def test_real_http_challenge_and_real_tls_gateway_identity(self):
        # Local network integration only. The transport adapter maps the test
        # domain/port to two loopback servers; production uses real public DNS.
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw); web=root/'web';web.mkdir();cert=root/'cert.pem';key=root/'key.pem'
            subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                            '-subj','/CN=localhost','-addext','subjectAltName=DNS:localhost',
                            '-keyout',str(key),'-out',str(cert)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self,*a): pass
                def do_GET(self):
                    path=urlsplit(self.path).path
                    if path=='/ha/live': body=b'{"status":"ok","service":"gateway","node":"B"}'
                    else:
                        try: body=(web/path.lstrip('/')).read_bytes()
                        except FileNotFoundError: self.send_error(404);return
                    self.send_response(200);self.send_header('CF-Ray','emulated-edge');self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
            http_server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            https_server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
            tls=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);tls.load_cert_chain(cert,key);https_server.socket=tls.wrap_socket(https_server.socket,server_side=True)
            threads=[]
            for server in (http_server,https_server):
                t=threading.Thread(target=server.serve_forever,daemon=True);t.start();threads.append(t)
            def command(args,timeout):
                args=list(args);u=urlsplit(args[-1]);port=http_server.server_port if u.scheme=='http' else https_server.server_port
                args[-1]=f'{u.scheme}://localhost:{port}{u.path}'+(('?'+u.query) if u.query else '')
                env=os.environ.copy();env['CURL_CA_BUNDLE']=str(cert)
                return subprocess.run(args,timeout=timeout,env=env,text=True,capture_output=True)
            try:
                route=PublicRoute(web,['localhost'],'B',True,command)
                self.assertTrue(route.probe('http')['ok']);self.assertTrue(route.probe('https')['ok'])
                self.assertFalse(list((web/'.well-known/acme-challenge').iterdir()))
            finally:
                for server in (http_server,https_server):server.shutdown();server.server_close()
                for t in threads:t.join(2)


if __name__=='__main__':unittest.main(verbosity=2)
