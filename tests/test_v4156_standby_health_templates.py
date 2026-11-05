#!/usr/bin/env python3
"""v41.5.6: absent Docker State.Health is normal on a staged standby."""
from __future__ import annotations
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
from managed_updates import DockerApplications

PROJECT = 'taskforge-prod'
SAFE = '{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}'
SAFE_STATUS = '{{with index .State "Health"}}{{.Status}}{{else}}{{.State.Status}}{{end}}'


def completed(code=0, out='', err=''):
    return CompletedProcess([], code, out, err)


class SafeDocker:
    def __init__(self):
        self.containers = {}
        self.calls = []
    def __call__(self, *args, timeout=60):
        self.calls.append(args)
        if args[:2] == ('inspect', '-f'):
            pattern = args[2]
            if SAFE not in pattern:
                return completed(1, err='template: map has no entry for key "Health"')
            names = args[3:]
            if any(n not in self.containers for n in names):
                return completed(1, err='No such object')
            result = []
            for name in names:
                item = self.containers[name]
                result.append('|'.join([
                    '/' + name, item['Id'], item['Image'], item['State']['Status'],
                    (item['State'].get('Health') or {}).get('Status', 'none'),
                    str(item['RestartCount']), item['State']['StartedAt'],
                    str(item['State']['ExitCode'])
                ]))
            return completed(0, '\n'.join(result) + '\n')
        if args[:3] == ('ps', '-a', '--format'):
            return completed(0, ''.join(f'{n}|{i["Id"][:12]}\n' for n, i in self.containers.items()))
        if args[:2] == ('container', 'inspect'):
            ids = set(args[2:])
            matches = [i for i in self.containers.values() if i['Id'][:12] in ids]
            return completed(0, json.dumps(matches))
        raise AssertionError('Unexpected Docker call: ' + str(args))
    def put(self, service, health=None, state='created'):
        name = f'{PROJECT}-{service}-1'
        details = {
            'Name': '/' + name, 'Id': ('abc000' + service.encode().hex())[:64].ljust(64, '0'),
            'Image': 'sha256:' + service, 'RestartCount': 0,
            'State': {'Status': state, 'StartedAt': '2026-10-19T20:00:00Z', 'ExitCode': 0},
        }
        if health:
            details['State']['Health'] = {'Status': health}
        self.containers[name] = details


class HealthTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root/'runtime').mkdir()
        self.backend = DockerApplications(root, root/'runtime', PROJECT, {})
        self.fake = SafeDocker()
        self.backend.cli = self.fake
    def test_absent_health_on_stopped_container_needs_only_one_inspect(self):
        self.fake.put('front')
        actual = self.backend.observe(['front'])
        self.assertEqual(actual['front']['state'], 'created')
        self.assertEqual(actual['front']['health'], 'none')
        self.assertEqual(self.backend.observe_counters()['fallback_calls'], 0)
        self.assertEqual(len(self.fake.calls), 1)
    def test_mixed_healthy_and_unhealthy_containers_without_fallback(self):
        self.fake.put('front', 'healthy', 'running')
        self.fake.put('solutions-api', 'unhealthy', 'running')
        self.fake.put('java-runner')
        data = self.backend.observe(['front', 'solutions-api', 'java-runner'])
        self.assertEqual([data[x]['health'] for x in ['front','solutions-api','java-runner']],
                         ['healthy','unhealthy','none'])
        self.assertEqual(len(self.fake.calls), 1)
    def test_genuinely_missing_container_still_fallbacks_not_assumed_healthy(self):
        self.fake.put('front', 'healthy', 'running')
        data = self.backend.observe(['front','support-bot'])
        self.assertEqual(data['front']['health'], 'healthy')
        self.assertEqual(data['support-bot']['state'], 'missing')
        self.assertEqual(self.backend.observe_counters()['fallback_successes'], 1)
        self.assertEqual(self.backend.observe_counters()['missing_confirmed'], 1)
        self.assertEqual(len(self.fake.calls), 3)
    def test_no_new_infrastructure_mutation(self):
        self.fake.put('front')
        self.backend.observe(['front'])
        self.assertFalse(any(x in ('stop','rm','up','create','start','kill')
                             for args in self.fake.calls for x in args))
    def test_all_active_healthcheck_templates_are_safe(self):
        paths=[]
        for path in (ROOT/'cluster').rglob('*'):
            if path.suffix not in ('.py','.sh'):
                continue
            content=path.read_text()
            self.assertNotIn('{{if .State.Health}}', content, path.as_posix())
            if 'index .State "Health"' in content:
                paths.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(len(paths), 10, paths)
        for expected in ('cluster/agent.py', 'cluster/edge_lifecycle.py',
                         'cluster/managed_updates.py',
                         'cluster/ops/current/optimize-rabbitmq-standby.sh',
                         'cluster/ops/current/sql_status.py'):
            self.assertIn(expected, paths)
    def test_version_and_rabbitmq_health_retained(self):
        self.assertEqual((ROOT/'VERSION').read_text().strip(), 'v41.5.9')
        self.assertEqual((ROOT/'REVISION').read_text().strip(), '410509')
        compose = (ROOT/'compose'/'00-storage.yaml').read_text()
        self.assertIn('rabbitmq-diagnostics', compose)
        self.assertIn('interval: 30s', compose)
        self.assertIn('retries: 10', compose)
        self.assertNotIn('docker-compose down', (ROOT/'cluster/managed_updates.py').read_text())
    @unittest.skipUnless(shutil.which('go'), 'Go is optional for standalone package QA')
    def test_actual_go_template_missingkey_error_behavior(self):
        go_code = r'''package main
import("bytes"; "fmt"; "text/template")
func main(){
 for _,tc:=range []struct{status string; health string}{ {"created",""}, {"running","healthy"}, {"running","unhealthy"} } {
  state:=map[string]interface{}{"Status":tc.status}
  if tc.health!="" { state["Health"]=map[string]interface{}{"Status":tc.health} }
  input:=map[string]interface{}{"State":state}
  for _,source:=range []string{
    `{{with index .State "Health"}}{{.Status}}{{else}}none{{end}}`,
    `{{with index .State "Health"}}{{.Status}}{{else}}{{.State.Status}}{{end}}`,
  }{
    t,err:=template.New("test").Option("missingkey=error").Parse(source)
    if err!=nil {panic(err)}
    var b bytes.Buffer
    if err=t.Execute(&b,input);err!=nil {panic(err)}
    fmt.Println(b.String())
  }
 }
}'''
        with tempfile.TemporaryDirectory() as td:
            file=Path(td)/'test.go'
            file.write_text(go_code)
            result=subprocess.run(['go','run',str(file)],capture_output=True,text=True,
                                  timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(),
                         ['none','created','healthy','healthy','unhealthy','unhealthy'])

if __name__ == '__main__':
    unittest.main(verbosity=2)
