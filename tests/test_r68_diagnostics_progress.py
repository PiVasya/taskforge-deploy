#!/usr/bin/env python3
from __future__ import annotations
import json, os, subprocess, tempfile, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
collector=ROOT/'scripts'/'diagnostics'/'collect-taskforge-diagnostics.sh'

with tempfile.TemporaryDirectory(prefix='taskforge-r68-progress-') as raw:
    td=Path(raw); bindir=td/'bin'; bindir.mkdir(); out=td/'out'; out.mkdir(); cfg=td/'config'; cfg.mkdir()
    docker=bindir/'docker'
    docker.write_text(r'''#!/usr/bin/env bash
set -Eeuo pipefail
cmd="${1:-}"; shift || true
case "$cmd" in
  ps)
    if printf '%s\n' "$@" | grep -Fq -- '--format'; then
      printf '%s\n' 'taskforge-prod-a-1' 'taskforge-prod-b-1' 'taskforge-prod-c-1'
    else
      printf 'CONTAINER ID IMAGE COMMAND CREATED STATUS PORTS NAMES\n'
    fi
    ;;
  stats) printf 'stats\n' ;;
  inspect)
    if printf '%s\n' "$@" | grep -Fq -- '--format'; then
      printf '/taskforge-prod-a-1|img:a|sha256:a|running|now|0\n'
      printf '/taskforge-prod-b-1|img:b|sha256:b|running|now|0\n'
      printf '/taskforge-prod-c-1|img:c|sha256:c|running|now|0\n'
    else
      name="${1:-taskforge-prod-a-1}"
      printf '[{"Name":"/%s","Image":"sha256:test","RestartCount":0,"State":{"Status":"running","Running":true,"Restarting":false,"OOMKilled":false,"Dead":false,"ExitCode":0,"Error":"","StartedAt":"now","FinishedAt":""},"Config":{"Image":"example/test:latest","Labels":{"com.docker.compose.project":"taskforge-prod"}},"HostConfig":{"ReadonlyRootfs":false,"RestartPolicy":{"Name":"unless-stopped"},"Memory":0,"MemorySwap":0,"NanoCpus":0},"NetworkSettings":{"Networks":{}},"Mounts":[]}]\n' "$name"
    fi
    ;;
  logs) sleep 0.08; printf '2026-09-17T00:00:00Z hello from %s\n' "${*: -1}" ;;
  events) printf 'event\n' ;;
  images|volume|network|info) printf '%s\n' "$cmd" ;;
  *) exit 0 ;;
esac
''')
    docker.chmod(0o755)
    progress=td/'progress.json'
    env=os.environ.copy(); env['PATH']=f"{bindir}:{env.get('PATH','')}"; env['TASKFORGE_CONFIG_ROOT']=str(cfg); env['TASKFORGE_DIAGNOSTICS_PROGRESS_STDERR']='1'
    proc=subprocess.Popen([
        'bash',str(collector),'--out',str(out),'--since','1h','--max-log-mb','1','--progress-file',str(progress)
    ],cwd=ROOT,env=env,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    seen=[]; deadline=time.time()+12
    while proc.poll() is None and time.time()<deadline:
        try:
            state=json.loads(progress.read_text())
        except (OSError,ValueError):
            time.sleep(0.02); continue
        if state.get('phase')=='containers':
            seen.append((state.get('current_index'),state.get('containers_completed'),state.get('containers_total'),state.get('current_item'),state.get('step')))
        time.sleep(0.02)
    stdout,stderr=proc.communicate(timeout=5)
    assert proc.returncode==0,(proc.returncode,stderr[-3000:])
    final=json.loads(progress.read_text())
    assert final['phase']=='completed',final
    assert final['containers_total']==3 and final['containers_completed']==3,final
    assert final['completed']==final['total'] and final['percent']==100,final
    assert final['archive_bytes']>0 and final['files_collected']>0 and final['bytes_collected']>0,final
    assert any(x[0]==1 and x[2]==3 and x[3]=='taskforge-prod-a-1' for x in seen),seen
    assert any(x[0]==3 and x[2]==3 and x[3]=='taskforge-prod-c-1' for x in seen),seen
    assert 'container 1/3' in stderr and 'container 3/3' in stderr,stderr
    archive=Path(stdout.strip().splitlines()[-1])
    assert archive.is_file() and archive.stat().st_size==final['archive_bytes'],(archive,final)

print('PASS: r68 collector publishes real container/stage/file/byte progress while collection is running')
