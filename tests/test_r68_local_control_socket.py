#!/usr/bin/env python3
import importlib.util, json, socket, sys, tempfile, threading
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
spec=importlib.util.spec_from_file_location('taskforge_agent_r68',ROOT/'cluster'/'agent.py')
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

class Diagnostics:
    def start(self,mode,since,max_log_mb):
        return {'job_id':'diag-test-12345678','status':'queued','mode':mode,'since':since,'max_log_mb':max_log_mb}
    def status(self,job):
        return {'job_id':job,'status':'completed','archive_name':'taskforge_diagnostics_test.tar.gz','size_bytes':12}

class Edge:
    def snapshot(self): return {'success':True}


def request(path,raw):
    s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(3);s.connect(path);s.sendall(raw)
    chunks=[]
    while True:
        part=s.recv(65536)
        if not part: break
        chunks.append(part)
    s.close();blob=b''.join(chunks);head,_,body=blob.partition(b'\r\n\r\n')
    status=int(head.split(b' ',2)[1]);return status,json.loads(body.decode())

with tempfile.TemporaryDirectory(prefix='taskforge-r68-socket-') as raw:
    path=str(Path(raw)/'node-agent.sock')
    module.Handler.agent=SimpleNamespace(
        diagnostics_token='secret-token', diagnostics=Diagnostics(), node_id='A', bundle_revision='68',
        telemetry={'ha':{'reconciled':True}}, edge_controller=Edge(), _cloudflare_configured=lambda:True,
    )
    server=module.ThreadingUnixHTTPServer(path,module.Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        status,payload=request(path,b'GET /ha/live HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n')
        assert status==200 and payload=={'ok':True,'node':'A','revision':68,'reconciled':True},(status,payload)
        body=b'{"mode":"standard","since":"3h","max_log_mb":16}'
        raw_req=(b'POST /ha/diagnostics HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Type: application/json\r\n'
                 b'X-TaskForge-Cluster-Token: secret-token\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
        status,payload=request(path,raw_req)
        assert status==202 and payload['job_id']=='diag-test-12345678' and payload['mode']=='standard',(status,payload)
        bad=b'POST /ha/diagnostics HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\nContent-Length: 2\r\n\r\n{}'
        status,payload=request(path,bad)
        assert status==401 and payload['code']=='DIAGNOSTICS_UNAUTHORIZED',(status,payload)
    finally:
        server.shutdown();server.server_close();thread.join(timeout=2)

print('PASS: r68 local Unix Node Agent control serves liveness and authenticated diagnostics without TCP hairpin')
