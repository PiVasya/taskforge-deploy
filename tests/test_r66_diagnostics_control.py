#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("diagnostics_manager", ROOT / "cluster" / "diagnostics_manager.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
DiagnosticsManager = module.DiagnosticsManager
DiagnosticsError = module.DiagnosticsError
diagnostics_token = module.diagnostics_token


def wait_terminal(manager, job_id, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = manager.status(job_id)
        if state["status"] in {"completed", "failed"}:
            return state
        time.sleep(0.03)
    raise AssertionError(f"job did not finish: {manager.status(job_id)}")



def args_without_progress_file(lines):
    out=[]; i=0
    while i < len(lines):
        if lines[i] == '--progress-file':
            i += 2
            continue
        out.append(lines[i]); i += 1
    return out

def assert_error(code, fn):
    try:
        fn()
    except DiagnosticsError as exc:
        assert exc.code == code, (exc.code, code)
    else:
        raise AssertionError(f"expected DiagnosticsError {code}")


key = "k" * 64
expected = hmac.new(key.encode(), b"taskforge-cluster-agent-diagnostics-v1", hashlib.sha256).hexdigest()
assert diagnostics_token({"TASKFORGE_INTERNAL_KEY": key}) == expected
assert diagnostics_token({"TASKFORGE_INTERNAL_KEY": "short"}) == ""

with tempfile.TemporaryDirectory(prefix="taskforge-r66-diag-") as raw:
    td = Path(raw)
    root = td / "control"
    runtime = td / "runtime"
    scripts = root / "scripts" / "diagnostics"
    scripts.mkdir(parents=True)
    runtime.mkdir()
    collector = scripts / "collect-taskforge-diagnostics.sh"
    collector.write_text(r'''#!/usr/bin/env bash
set -Eeuo pipefail
out=''; progress_file=''; args_file="${TF_TEST_ARGS_FILE:?}"
printf '%s\n' "$@" > "$args_file"
while [ $# -gt 0 ]; do
  case "$1" in
    --out) out="$2"; shift 2 ;;
    --progress-file) progress_file="$2"; shift 2 ;;
    *) shift ;;
  esac
done
[ -n "$out" ]; [ -n "$progress_file" ]
mkdir -p "$out" "$(dirname "$progress_file")"
printf '%s\n' '{"version":1,"phase":"containers","step":"logs","completed":7,"total":20,"percent":35,"current_item":"tasks-api","current_index":3,"containers_completed":2,"containers_total":8,"files_collected":14,"bytes_collected":4096,"archive_bytes":0}' > "$progress_file"
if [ "${TF_TEST_SLEEP:-0}" != 0 ]; then sleep "$TF_TEST_SLEEP"; fi
archive="$out/taskforge_diagnostics_test.tar.gz"
printf 'diagnostics-payload\n' > "$archive"
printf '%s\n' '{"version":1,"phase":"archive","step":"complete","completed":20,"total":20,"percent":100,"current_item":"","current_index":0,"containers_completed":8,"containers_total":8,"files_collected":20,"bytes_collected":8192,"archive_bytes":20}' > "$progress_file"
printf '%s\n' "$archive"
''')
    collector.chmod(0o755)

    args_file = td / "args.txt"
    import os
    old_args = os.environ.get("TF_TEST_ARGS_FILE")
    old_sleep = os.environ.get("TF_TEST_SLEEP")
    os.environ["TF_TEST_ARGS_FILE"] = str(args_file)
    try:
        manager = DiagnosticsManager(root, runtime, "A", {"TASKFORGE_INTERNAL_KEY": key})

        assert_error("DIAGNOSTICS_MODE_INVALID", lambda: manager.start("shell", "6h", 32))
        assert_error("DIAGNOSTICS_SINCE_INVALID", lambda: manager.start("standard", "../../etc", 32))
        assert_error("DIAGNOSTICS_MAX_LOG_INVALID", lambda: manager.start("standard", "6h", 0))
        assert_error("DIAGNOSTICS_JOB_INVALID", lambda: manager.status("../bad"))

        job = manager.start("standard", "3h", 16)
        state = wait_terminal(manager, job["job_id"])
        assert state["status"] == "completed", state
        assert state["archive_name"] == "taskforge_diagnostics_test.tar.gz"
        assert manager.archive(job["job_id"]).parent == (runtime / "diagnostics").resolve()
        args = args_file.read_text().splitlines()
        assert '--progress-file' in args, args
        assert args_without_progress_file(args) == ["--out", str(runtime / "diagnostics"), "--since", "3h", "--max-log-mb", "16"], args
        assert state['progress']['containers_total'] == 8 and state['progress']['containers_completed'] == 8, state

        # Let the fake collector reuse its deterministic name after the first job.
        (runtime / "diagnostics" / "taskforge_diagnostics_test.tar.gz").unlink(missing_ok=True)
        job = manager.start("quick", "30m", 8)
        state = wait_terminal(manager, job["job_id"])
        assert state["status"] == "completed", state
        args = args_file.read_text().splitlines()
        assert '--progress-file' in args, args
        assert "--quick" in args and "30m" in args and "8" in args, args

        (runtime / "diagnostics" / "taskforge_diagnostics_test.tar.gz").unlink(missing_ok=True)
        job = manager.start("full", "7d", 256)
        state = wait_terminal(manager, job["job_id"])
        assert state["status"] == "completed", state
        args = args_file.read_text().splitlines()
        assert '--progress-file' in args, args
        assert args_without_progress_file(args) == ["--out", str(runtime / "diagnostics"), "--full-logs"], args

        # Only one collector may be active on a node.
        (runtime / "diagnostics" / "taskforge_diagnostics_test.tar.gz").unlink(missing_ok=True)
        os.environ["TF_TEST_SLEEP"] = "0.35"
        running = manager.start("standard", "1h", 32)
        assert_error("DIAGNOSTICS_BUSY", lambda: manager.start("quick", "1h", 32))
        deadline = time.time() + 2.0
        observed = None
        while time.time() < deadline:
            candidate = manager.status(running["job_id"])
            if candidate.get('progress', {}).get('phase') == 'containers':
                observed = candidate
                break
            time.sleep(0.02)
        assert observed is not None, manager.status(running["job_id"])
        assert observed['progress']['current_item'] == 'tasks-api', observed
        assert observed['progress']['current_index'] == 3, observed
        assert observed['progress']['containers_completed'] == 2, observed
        assert observed['progress']['containers_total'] == 8, observed
        assert wait_terminal(manager, running["job_id"])["status"] == "completed"
    finally:
        if old_args is None:
            os.environ.pop("TF_TEST_ARGS_FILE", None)
        else:
            os.environ["TF_TEST_ARGS_FILE"] = old_args
        if old_sleep is None:
            os.environ.pop("TF_TEST_SLEEP", None)
        else:
            os.environ["TF_TEST_SLEEP"] = old_sleep

agent = (ROOT / "cluster" / "agent.py").read_text()
for token in (
    "X-TaskForge-Cluster-Token",
    "hmac.compare_digest",
    "['ha','diagnostics']",
    "a.diagnostics.start",
    "a.diagnostics.archive",
):
    assert token in agent, token

print("PASS: r66 authenticated asynchronous diagnostics control")
