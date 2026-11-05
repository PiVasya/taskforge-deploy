#!/usr/bin/env python3
"""Offline SQL topology/HA checks. No Docker, DB or network mutation."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import yaml
import os

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'cluster'))
import agent

spec = importlib.util.spec_from_file_location('sql_prepare', ROOT / 'scripts/sql/prepare-runtime.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class SqlRuntimeTests(unittest.TestCase):
    def test_full_lite_optional_and_assist_inventory(self):
        cfg = json.loads((ROOT / 'cluster/cluster.default.json').read_text())
        profiles = {n['id']: n['app']['profile'] for n in cfg['nodes']}
        self.assertEqual(profiles, {'A': 'full', 'B': 'full', 'C': 'lite'})
        self.assertTrue(agent.SQL_SERVICES.issubset(set(agent.APP_FULL)))
        self.assertFalse(agent.SQL_SERVICES.intersection(agent.APP_LITE))
        self.assertTrue(agent.SQL_SERVICES.issubset(agent.OPTIONAL_SERVICES))
        self.assertTrue(agent.SQL_SERVICES.issubset(agent.DEFAULT_ASSIST_EXCLUDES))
        critical = set(agent.APP_FULL) - agent.OPTIONAL_SERVICES
        self.assertFalse(agent.SQL_SERVICES.intersection(critical))

    def fake_agent(self):
        a = object.__new__(agent.Agent)
        a._sql_lock = threading.Lock()
        a._sql_desired = False
        a._sql_force = False
        a.cname = lambda service: 'test-' + service
        a.inspect_health_many = Mock(side_effect=lambda names: {name:{'status':'running','health':'healthy','restart':0,'oom':False} for name in names})
        a.compose = Mock(return_value=subprocess.CompletedProcess([], 0))
        a.watchtower = Mock()
        return a

    def test_start_sql_is_deferred_not_in_primary_compose(self):
        a = self.fake_agent()
        a.start_service_set(['gateway', 'sql-worker', 'sql-postgres', 'sql-mysql'])
        self.assertTrue(a._sql_desired)
        a.compose.assert_not_called()
        a.inspect_health_many.assert_not_called()

    def test_stop_sql_is_deferred_not_in_primary_docker_stop(self):
        a = self.fake_agent()
        a._sql_desired = True
        with patch.object(agent, 'run') as run:
            a.stop_service_set(['gateway', 'sql-worker', 'sql-postgres', 'sql-mysql'])
        self.assertFalse(a._sql_desired)
        run.assert_not_called()
        a.inspect_health_many.assert_not_called()

    def test_sql_networks_and_cache_are_separate(self):
        compose = yaml.safe_load((ROOT / 'compose/35-sql.yaml').read_text())
        for service, network in [('sql-postgres', 'sql-postgres-net'), ('sql-mysql', 'sql-mysql-net')]:
            c = compose['services'][service]
            self.assertEqual(c['networks'], [network])
            self.assertTrue(compose['networks'][network]['internal'])
            self.assertNotIn('ports', c)
            self.assertTrue(c['tmpfs'])
            self.assertEqual(c['labels']['taskforge.ha.critical'], 'false')
            self.assertTrue(all('entrypoint-initdb.d:ro' in v for v in c.get('volumes', [])))
        self.assertEqual(compose['services']['sql-mysql']['restart'], 'on-failure:5')
        self.assertNotIn('volumes', compose)

    def test_sql_cpu_memory_and_privilege_boundaries(self):
        services = yaml.safe_load((ROOT / 'compose/35-sql.yaml').read_text())['services']
        for name in agent.SQL_SERVICES:
            c = services[name]
            self.assertTrue(c['read_only'])
            self.assertEqual(c['cap_drop'], ['ALL'])
            self.assertIn('no-new-privileges:true', c['security_opt'])
            for key in ('mem_limit', 'memswap_limit', 'cpus', 'pids_limit'):
                self.assertIn(key, c)
            self.assertEqual(c['cpu_shares'], 128)
            self.assertEqual(c['oom_score_adj'], 700)
            self.assertNotIn('docker.sock', json.dumps(c))
            self.assertNotIn('ports', c)

    def test_go_worker_has_native_health_and_thread_budget(self):
        worker = yaml.safe_load((ROOT / 'compose/35-sql.yaml').read_text())['services']['sql-worker']
        self.assertEqual(worker['healthcheck']['test'], ['CMD', '/app/sql-worker', 'health'])
        self.assertEqual(worker['pids_limit'], 256)
        self.assertEqual(worker['environment']['GOMEMLIMIT'], '128MiB')
        self.assertEqual(worker['stop_grace_period'], '45s')
        self.assertNotIn('python', json.dumps(worker))

    def test_lite_preparation_does_not_touch_docker_or_generate_engine_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'sql-runtime.env'
            args = ['prepare', '--env-file', str(output), '--node', 'C', '--profile', 'lite', '--pull']
            with patch.object(sys, 'argv', args), patch.object(prepare, 'pin') as pin, contextlib.redirect_stdout(io.StringIO()):
                prepare.main()
            pin.assert_not_called()
            self.assertEqual(prepare.read_env(output), {'SQL_ENABLED': 'false', 'SQL_WORKER_ID': 'sql-C'})
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_preparation_preserves_secrets_and_selects_preferred_mysql_on_v2_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / 'sql-runtime.env'
            saved = {'SQL_SANDBOX_MARKER': 'a'*64, 'SQL_POSTGRES_PASSWORD': 'b'*64, 'SQL_MYSQL_PASSWORD': 'c'*64,
                     'SQL_POSTGRES_IMAGE': 'postgres@sha256:'+'d'*64, 'SQL_MYSQL_IMAGE': 'mysql@sha256:'+'e'*64}
            output.write_text('\n'.join(k+'='+v for k,v in saved.items()))
            args = ['prepare', '--env-file', str(output), '--node', 'B', '--pull']
            with patch.object(sys, 'argv', args), \
                 patch.object(prepare, 'pin', side_effect=lambda image,pull: (image,image.split('@')[-1])) as pin, \
                 patch.object(prepare, 'host_needs_cpuv1', return_value=False), \
                 patch.object(prepare, 'observed_x86_v2_failure', return_value=False), \
                 contextlib.redirect_stdout(io.StringIO()):
                prepare.main()
            current = prepare.read_env(output)
            self.assertEqual(current['SQL_SANDBOX_MARKER'], saved['SQL_SANDBOX_MARKER'])
            self.assertEqual(current['SQL_POSTGRES_PASSWORD'], saved['SQL_POSTGRES_PASSWORD'])
            self.assertEqual(current['SQL_MYSQL_PASSWORD'], saved['SQL_MYSQL_PASSWORD'])
            self.assertEqual(current['SQL_POSTGRES_IMAGE'], saved['SQL_POSTGRES_IMAGE'])
            self.assertEqual(current['SQL_MYSQL_IMAGE'], prepare.MYSQL_PREFERRED_IMAGE)
            self.assertEqual(current['SQL_MYSQL_RUNTIME_VARIANT'], 'preferred')
            self.assertEqual(current['SQL_MYSQL_RUNTIME_FAMILY'], prepare.MYSQL_RUNTIME_FAMILY)
            self.assertEqual(current['SQL_WORKER_ID'], 'sql-B')
            self.assertEqual(pin.call_count, 2)
            self.assertEqual(pin.call_args_list[1].args[0], prepare.MYSQL_PREFERRED_IMAGE)

    def test_mysql_cpuv1_is_selected_by_host_capability_or_observed_failure(self):
        for needs_cpuv1, observed in ((True, False), (False, True)):
            with self.subTest(needs_cpuv1=needs_cpuv1, observed=observed), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / 'sql-runtime.env'
                output.write_text('SQL_SANDBOX_MARKER='+('a'*64)+'\nSQL_POSTGRES_PASSWORD='+('b'*64)+'\nSQL_MYSQL_PASSWORD='+('c'*64)+'\n')
                args = ['prepare', '--env-file', str(output), '--node', 'B', '--pull']
                with patch.object(sys, 'argv', args), \
                     patch.object(prepare, 'pin', side_effect=lambda image,pull: (image,image.split('@')[-1] if '@' in image else 'sha256:'+('9'*64))) as pin, \
                     patch.object(prepare, 'host_needs_cpuv1', return_value=needs_cpuv1), \
                     patch.object(prepare, 'observed_x86_v2_failure', return_value=observed), \
                     contextlib.redirect_stdout(io.StringIO()):
                    prepare.main()
                current = prepare.read_env(output)
                self.assertEqual(current['SQL_MYSQL_IMAGE'], prepare.MYSQL_CPUV1_IMAGE)
                self.assertEqual(current['SQL_MYSQL_RUNTIME_VARIANT'], 'cpuv1')
                self.assertEqual(pin.call_args_list[1].args[0], prepare.MYSQL_CPUV1_IMAGE)

    def test_mysql_runtime_family_is_stable_and_r70_ol9_is_upgraded(self):
        cases = (
            (prepare.MYSQL_CPUV1_IMAGE, prepare.MYSQL_RUNTIME_FAMILY, True, prepare.MYSQL_CPUV1_IMAGE, 'stable-hardware-selection'),
            (prepare.MYSQL_PREFERRED_IMAGE, prepare.MYSQL_RUNTIME_FAMILY, True, prepare.MYSQL_CPUV1_IMAGE, 'corrected-stale-hardware-selection'),
            (prepare.MYSQL_R70_OL9_IMAGE, 'mysql-8.4.0-ol8-ol9-v1', False, prepare.MYSQL_PREFERRED_IMAGE, 'runtime-family-upgrade'),
        )
        for existing, family, needs_cpuv1, expected, reason in cases:
            with self.subTest(existing=existing, family=family, needs_cpuv1=needs_cpuv1), tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'sql-runtime.env'
                output.write_text('SQL_SANDBOX_MARKER='+('a'*64)+'\nSQL_POSTGRES_PASSWORD='+('b'*64)+'\nSQL_MYSQL_PASSWORD='+('c'*64)+'\nSQL_MYSQL_IMAGE='+existing+'\nSQL_MYSQL_RUNTIME_FAMILY='+family+'\n')
                args=['prepare','--env-file',str(output),'--node','B','--pull']
                stdout=io.StringIO()
                with patch.object(sys,'argv',args), patch.object(prepare,'pin',side_effect=lambda image,pull:(image,image.split('@')[-1] if '@' in image else 'sha256:'+('9'*64))), \
                     patch.object(prepare,'host_needs_cpuv1',return_value=needs_cpuv1), patch.object(prepare,'observed_x86_v2_failure',return_value=False), \
                     contextlib.redirect_stdout(stdout):
                    prepare.main()
                current=prepare.read_env(output)
                self.assertEqual(current['SQL_MYSQL_IMAGE'],expected)
                self.assertEqual(current['SQL_MYSQL_RUNTIME_FAMILY'],prepare.MYSQL_RUNTIME_FAMILY)
                self.assertIn(reason,stdout.getvalue())

    def test_mysql_operator_override_still_wins_release_compatibility_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); output=root/'sql-runtime.env'; base=root/'taskforge.env'
            output.write_text('SQL_SANDBOX_MARKER='+('a'*64)+'\nSQL_POSTGRES_PASSWORD='+('b'*64)+'\nSQL_MYSQL_PASSWORD='+('c'*64)+'\n')
            base.write_text('SQL_MYSQL_IMAGE=registry.example/mysql-custom@sha256:'+('f'*64)+'\n')
            args=['prepare','--env-file',str(output),'--base-env',str(base),'--node','A','--pull']
            with patch.object(sys,'argv',args), patch.object(prepare,'pin',side_effect=lambda image,pull:(image,image.split('@')[-1] if '@' in image else 'sha256:'+('9'*64))) as pin, \
                 patch.object(prepare,'host_needs_cpuv1',return_value=True), patch.object(prepare,'observed_x86_v2_failure',return_value=True), \
                 contextlib.redirect_stdout(io.StringIO()):
                prepare.main()
            self.assertEqual(pin.call_args_list[1].args[0],'registry.example/mysql-custom@sha256:'+('f'*64))
            current=prepare.read_env(output)
            self.assertEqual(current['SQL_MYSQL_RUNTIME_FAMILY'],'operator-custom')

    def test_mysql_init_script_is_safe_when_sourced_by_upstream_entrypoint(self):
        script = ROOT / 'infrastructure/sql/mysql-init/10-sandbox-guard.sh'
        text = script.read_text()
        # Regression: r61 used `set -Eeuo pipefail`. Because MySQL sources init
        # scripts, `-u` leaked into docker-entrypoint.sh and caused
        # `MYSQL_ONETIME_PASSWORD: unbound variable` after our script returned.
        self.assertNotRegex(text, r'(?m)^\s*set\s+[^\n]*u')
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / 'bin'
            bindir.mkdir()
            fake = bindir / 'mysql'
            fake.write_text('#!/usr/bin/env bash\ncat >/dev/null\nexit 0\n')
            fake.chmod(0o755)
            env = os.environ.copy()
            env.update({
                'PATH': str(bindir) + os.pathsep + env.get('PATH', ''),
                'SQL_SANDBOX_MARKER': 'a' * 64,
                'MYSQL_ROOT_PASSWORD': 'secret',
            })
            probe = subprocess.run(
                ['bash', '-c', 'set +u; source "$1"; case "$-" in *u*) exit 9;; esac', 'probe', str(script)],
                env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            self.assertEqual(probe.returncode, 0, probe.stderr)

    def test_package_has_local_sql_init_and_no_r57_runtime_dependency(self):
        for relative in ('infrastructure/sql/postgres-init/10-sandbox-guard.sh',
                         'infrastructure/sql/mysql-init/10-sandbox-guard.sh',
                         'scripts/sql/prepare-runtime.py', 'docs/sql/DEPLOYMENT.md'):
            self.assertTrue((ROOT / relative).is_file(), relative)
        source = (ROOT / 'cluster/ops/current/migrate.sh').read_text()
        self.assertNotIn('../taskforge-prod-linux-v40-cluster-control-final-r57', source)
        self.assertNotIn('../taskforge-prod-linux-v40-cluster-control-final-r58', source)
        self.assertIn('profile=lite', source)
        self.assertEqual({p.name for p in ROOT.glob('*.md')}, {'00_AI_READ_THIS_FIRST.md'})


if __name__ == '__main__':
    unittest.main(verbosity=2)
