import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'cluster'))
from log_cleanup import LogCleanupManager
spec=importlib.util.spec_from_file_location('ha_collect',ROOT/'scripts/diagnostics/collect-ha.py')
collect=importlib.util.module_from_spec(spec);spec.loader.exec_module(collect)


class DiagnosticTests(unittest.TestCase):
    def test_direct_log_reader_finds_existing_history_without_touching_source(self):
        with tempfile.TemporaryDirectory() as raw:
            path=Path(raw,'abc-json.log')
            rows=[{'time':'2026-10-22T23:10:13Z','stream':'stderr','log':'Error communicating with DCS\n'},
                  {'time':'2026-10-22T23:10:17.012345678Z','stream':'stderr','log':'demoting self\n'},
                  {'time':'2026-10-23T16:41:07Z','stream':'stdout','log':'current log\n'}]
            original=''.join(json.dumps(row)+'\n' for row in rows)+'{"partial":'
            path.write_text(original)
            data,count=collect.docker_json_window(path,collect.stamp('2026-10-22T23:08:00Z'),collect.stamp('2026-10-22T23:17:00Z'))
            self.assertIn('demoting self',data);self.assertNotIn('current log',data)
            self.assertEqual(count['selected'],2);self.assertEqual(count['invalid'],1)
            self.assertEqual(path.read_text(),original)
    def test_archive_defaults_to_current_directory(self):
        source=(ROOT/'scripts/diagnostics/collect-ha.py').read_text()
        self.assertIn("parser.add_argument('--out',default='.')",source)
        self.assertIn("os.environ.get('SUDO_UID','0')",source)
    def test_obvious_credentials_redacted(self):
        value=collect.scrub('Authorization: Bearer aaaa password=foo https://name:pass@example.test/log')
        self.assertNotIn('aaaa',value);self.assertNotIn('foo',value);self.assertNotIn('name:pass',value)
    def test_ha_logs_excluded_from_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root=Path(raw);containers=root/'containers';containers.mkdir()
            rows=[]
            for service,char in [('postgres','a'),('etcd','b')]:
                cid=char*64;directory=containers/cid;directory.mkdir();path=directory/(cid+'-json.log');path.write_text('keep evidence\n')
                rows.append({'Id':cid,'Config':{'Labels':{'com.docker.compose.project':'taskforge-prod','com.docker.compose.service':service}},'HostConfig':{'LogConfig':{'Type':'json-file'}},'LogPath':str(path)})
            def runner(args,timeout=30,env=None):
                if args[:3]==['docker','ps','-aq']:return subprocess.CompletedProcess(args,0,'\n'.join(r['Id'] for r in rows),'')
                if args[:3]==['docker','info','--format']:return subprocess.CompletedProcess(args,0,str(root),'')
                if args[:2]==['docker','inspect']:return subprocess.CompletedProcess(args,0,json.dumps(rows),'')
                raise AssertionError(args)
            manager=LogCleanupManager(root/'runtime','taskforge-prod',runner=runner)
            result=manager._clear_docker_logs()
            self.assertEqual(result['containers_cleared'],0)
            self.assertEqual(result['skipped_drivers']['protected-ha'],2)
            for row in rows:self.assertEqual(Path(row['LogPath']).read_text(),'keep evidence\n')


if __name__=='__main__':unittest.main(verbosity=2)
