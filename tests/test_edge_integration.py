#!/usr/bin/env python3
"""Local integration contracts; fake Docker transport, real shells/OpenSSL/HTTP."""
from __future__ import annotations
import copy
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
import cloudflare_dns as cf
import edge_lifecycle as el
import primary


class CertificateShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.bin=self.root/'bin';self.bin.mkdir();self.web=self.root/'web';self.web.mkdir()
        self.le=self.root/'le';self.live=self.le/'live/test.invalid';self.live.mkdir(parents=True)
        self.runtime=self.root/'runtime';self.runtime.mkdir();self.envfile=self.root/'env'
        self.envfile.write_text('DOMAIN=test.invalid\nCT_DOMAIN=ct.test.invalid\nCOMPOSE_PROJECT_NAME=taskforge-prod\n')
        self.baseline=self.runtime/'storage-baseline-r55.json'
        self.baseline.write_text(json.dumps({'logical_volumes':{'letsencrypt':'old_custom_le','certbot-webroot':'old_custom_web'}}))
        self.calls=self.root/'docker-calls'
        docker=self.bin/'docker'
        docker.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
args=sys.argv[1:]
with open(os.environ['MOCK_CALLS'],'a') as f:f.write(json.dumps(args)+'\\n')
if args[0]=='inspect':
    if os.environ.get('MOCK_MISMATCH'):print('unrelated_volume');sys.exit(0)
    print('old_custom_le' if '/etc/letsencrypt' in args[2] else 'old_custom_web');sys.exit(0)
if args[:2]==['volume','ls']:sys.exit(0)
if args[:2]==['volume','inspect']:
    name=args[-1]
    if name not in ('old_custom_le','old_custom_web'):sys.exit(1)
    if '{{.Mountpoint}}' in args:print(os.environ['MOCK_LE'] if name=='old_custom_le' else os.environ['MOCK_WEB'])
    else:print('[]')
    sys.exit(0)
sys.exit(91)
''');docker.chmod(0o755)
        self.env=os.environ.copy();self.env.update(PATH=str(self.bin)+os.pathsep+os.environ['PATH'],TASKFORGE_ENV_FILE=str(self.envfile),TASKFORGE_CLUSTER_RUNTIME=str(self.runtime),TASKFORGE_CONFIG_ROOT=str(self.root),MOCK_LE=str(self.le),MOCK_WEB=str(self.web),MOCK_CALLS=str(self.calls))
        self.make_cert(['test.invalid','ct.test.invalid'])
    def make_cert(self,domains):
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2','-subj','/CN=test.invalid','-addext','subjectAltName='+','.join('DNS:'+h for h in domains),'-keyout',str(self.live/'privkey.pem'),'-out',str(self.live/'fullchain.pem')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    def cmd(self,mode):
        return subprocess.run(['bash',str(ROOT/'cluster/ops/current/local-cert.sh'),mode],env=self.env,text=True,capture_output=True,timeout=15)
    def test_real_shell_uses_pinned_custom_volume_and_validates_actual_certificate(self):
        p=self.cmd('status');self.assertEqual(p.returncode,0,p.stderr)
        doc=json.loads(p.stdout);self.assertEqual(doc['volume'],'old_custom_le');self.assertGreater(doc['not_after_epoch'],time.time());self.assertEqual(len(doc['fingerprint']),64)
        self.assertEqual(self.cmd('webroot-path').stdout.strip(),str(self.web))
        calls=[json.loads(x) for x in self.calls.read_text().splitlines()]
        self.assertFalse(any(x[:2] in (['volume','create'],['volume','rm'],['volume','prune']) for x in calls))
    def test_baseline_disagreement_is_not_accepted_as_another_certificate(self):
        self.env['MOCK_MISMATCH']='1';self.assertNotEqual(self.cmd('status').returncode,0);self.assertNotEqual(self.cmd('webroot-path').returncode,0)
    def test_wrong_san_is_rejected(self):
        self.make_cert(['wrong.invalid']);self.assertNotEqual(self.cmd('status').returncode,0)
    def test_mismatched_private_key_is_rejected(self):
        subprocess.run(['openssl','genpkey','-algorithm','RSA','-pkeyopt','rsa_keygen_bits:2048','-out',str(self.live/'privkey.pem')],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.assertNotEqual(self.cmd('status').returncode,0)
    def test_readonly_inspection_never_creates_missing_volume(self):
        self.baseline.write_text(json.dumps({'logical_volumes':{'certbot-webroot':'missing'}}));self.assertNotEqual(self.cmd('webroot-path').returncode,0)
        self.assertNotIn('create',self.calls.read_text())


class PatroniHTTPTests(unittest.TestCase):
    def test_guard_uses_leader_lock_endpoint_and_rejects_http_503(self):
        state={'allow':True,'paths':[]}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self,*a):pass
            def do_GET(self):
                state['paths'].append(self.path)
                if self.path=='/primary':doc={'name':'B','state':'running','role':'primary','timeline':7};status=200 if state['allow'] else 503
                elif self.path=='/cluster':doc={'members':[{'name':'B','role':'leader'},{'name':'A','role':'replica'}]};status=200
                else:doc={};status=404
                self.send_response(status);self.end_headers();self.wfile.write(json.dumps(doc).encode())
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler);th=threading.Thread(target=server.serve_forever,daemon=True);th.start()
        try:
            cluster={'nodes':[{'id':'B','wireguard':{'ip':'127.0.0.1'},'postgres':{'patroni_rest_port':server.server_port}}]}
            with patch.dict(os.environ,{'HTTP_PROXY':'http://invalid:9','http_proxy':'http://invalid:9'}):
                self.assertEqual(cf.exact_one_leader(cluster,'B')['timeline'],7)
            self.assertEqual(state['paths'],['/primary','/cluster']);state['allow']=False
            with self.assertRaises(cf.CFError):cf.exact_one_leader(cluster,'B')
        finally:server.shutdown();server.server_close();th.join(2)


class PrimaryWaitTests(unittest.TestCase):
    def telemetry(self):
        return {'node':{'id':'B','bundle_revision':'57'},'ha':{'reconciled':True,'control_plane_fenced':False,'role':'primary','leader':'B','applications_active':True,'traffic_ready':True},'edge':{'configured':True,'state':{'status':'synced','success':True,'phase':'ready','route_ready':True,'tls_ready':True,'route_confirmations':3,'route_required':3,'target_node':'B'}}}
    def test_revalidation_route_check_does_not_finish_wait_even_with_old_fresh_proof(self):
        good=self.telemetry();checking=copy.deepcopy(good);checking['edge']['state'].update({'phase':'route-check','route_confirmations':1,'route_required':3})
        with patch.object(primary,'agent_telemetry',side_effect=[(200,checking),(200,good)]) as read,patch.object(primary.time,'sleep'):
            self.assertEqual(primary.wait_for_primary('B',{},True,10),good);self.assertEqual(read.call_count,2)
    def test_ready_phase_requires_all_declared_confirmations(self):
        good=self.telemetry();short=copy.deepcopy(good);short['edge']['state']['route_confirmations']=2
        with patch.object(primary,'agent_telemetry',side_effect=[(200,short),(200,good)]) as read,patch.object(primary.time,'sleep'):
            self.assertEqual(primary.wait_for_primary('B',{},True,10),good);self.assertEqual(read.call_count,2)
    def test_dns_success_alone_cannot_finish_public_switch(self):
        good=self.telemetry();dns_only=copy.deepcopy(good);dns_only['edge']['state']['route_ready']=False
        with patch.object(primary,'agent_telemetry',side_effect=[(200,dns_only),(200,good)]) as read,patch.object(primary.time,'sleep'):
            self.assertEqual(primary.wait_for_primary('B',{},True,10),good);self.assertEqual(read.call_count,2)
    def test_wrong_node_identity_or_old_target_is_rejected(self):
        good=self.telemetry();wrong=copy.deepcopy(good);wrong['node']['id']='A';old=copy.deepcopy(good);old['edge']['state']['target_node']='A'
        with patch.object(primary,'agent_telemetry',side_effect=[(200,wrong),(200,old),(200,good)]) as read,patch.object(primary.time,'sleep'):
            self.assertEqual(primary.wait_for_primary('B',{},True,10),good);self.assertEqual(read.call_count,3)
    def test_old_agent_revision_fails_immediately_instead_of_waiting_timeout(self):
        old=self.telemetry();old['node']['bundle_revision']='56'
        with patch.object(primary,'agent_telemetry',return_value=(200,old)) as read,patch.object(primary.time,'sleep') as sleeper:
            with self.assertRaisesRegex(primary.PrimaryError,'migrate the node first'):
                primary.wait_for_primary('B',{},True,600)
            self.assertEqual(read.call_count,1);sleeper.assert_not_called()
    def test_unavailable_agent_still_prints_progress(self):
        good=self.telemetry()
        with patch.object(primary,'agent_telemetry',side_effect=[(0,{}),(200,good)]),patch.object(primary.time,'sleep'),patch('builtins.print') as out:
            primary.wait_for_primary('B',{},True,10)
            self.assertTrue(any('Agent unavailable' in str(x) for x in out.call_args_list))


class CertbotContainerTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.rt=Path(self.tmp.name);(self.rt/'node-id').write_text('B');(self.rt/'enabled').touch()
        c=self.rt/'cluster.json';c.write_text(json.dumps({'nodes':[{'id':'B','wireguard':{'ip':'127.0.0.1'},'app':{'can_be_primary':True,'profile':'full'}}]}));e=self.rt/'env';e.write_text('DOMAIN=test.invalid\nCT_DOMAIN=ct.test.invalid\n')
        self.ops=el.SystemOps(ROOT,c,e,self.rt,threading.Event(),['gateway'])
        self.ops.guard=lambda:None;self.ops.core_ready=lambda:None
    def test_unowned_container_is_not_deleted(self):
        self.ops.command=lambda *a,**k:subprocess.CompletedProcess([],0,json.dumps([{'Config':{'Labels':{}}}]),'')
        with patch.object(el.subprocess,'run') as cleanup:
            with self.assertRaises(el.Pending):self.ops.certbot()
            cleanup.assert_not_called()
    def test_owned_certbot_is_cleaned_on_demotion(self):
        commands=[]
        def command(args,timeout):
            commands.append(args)
            if args[:2]==['docker','inspect']:return subprocess.CompletedProcess(args,1,'','not found')
            raise el.Cancelled('demoted')
        self.ops.command=command
        with patch.object(el.subprocess,'run') as cleanup:
            with self.assertRaises(el.Cancelled):self.ops.certbot()
            self.assertEqual(cleanup.call_args.args[0],['docker','rm','-f','taskforge-prod-certbot-edge'])
        self.assertIn('--label',commands[1]);self.assertIn('by.taskforge.edge-certbot=true',commands[1]);self.assertNotIn('--force-renewal',commands[1])
    def test_successful_cli_without_certificate_is_not_success(self):
        self.ops.command=lambda args,**k:subprocess.CompletedProcess(args,1 if args[:2]==['docker','inspect'] else 0,'','')
        self.ops.certificate=lambda:None
        with patch.object(el.subprocess,'run'):
            with self.assertRaisesRegex(el.Pending,'no valid matching'):self.ops.certbot()
        self.assertGreater(self.ops.cert_wait(),800)
    def test_unhealthy_core_blocks_dns_lifecycle(self):
        self.ops.command=lambda *a,**k:subprocess.CompletedProcess([],0,'/taskforge-prod-gateway-1|running|unhealthy\n','')
        # Use the real core_ready implementation rather than the fixture override.
        with self.assertRaises(el.Pending):el.SystemOps.core_ready(self.ops)


if __name__=='__main__':unittest.main(verbosity=2)
