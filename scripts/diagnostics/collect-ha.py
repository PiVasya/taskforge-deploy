#!/usr/bin/env python3
"""Read-only HA collector. Archive is created in the calling directory by default."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import socket
import subprocess
import sys
import tarfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

LIMIT = 16*1024*1024


def stamp(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)


def scrub(text):
    text = re.sub(r'(?i)(authorization\s*[:=]\s*)(?:Basic|Bearer)\s+\S+', r'\1[REDACTED]', text)
    text = re.sub(r'(?i)((?:password|token|secret|api[_-]?key)\s*[=:]\s*)[^\s,;]+', r'\1[REDACTED]', text)
    text = re.sub(r'(https?://)[^/\s:@]+:[^/@\s]+@', r'\1[REDACTED]@', text)
    return text


def run(args, env, timeout=35, limit=LIMIT):
    # Concurrent draining bounds retained output even for a flooded journal.
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                            start_new_session=True)
    tail = bytearray()
    def consume():
        for block in iter(lambda: proc.stdout.read(8192), b''):
            tail.extend(block)
            if len(tail)>limit:del tail[:-limit]
    thread=threading.Thread(target=consume,daemon=True);thread.start()
    try:
        rc=proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        import signal
        os.killpg(proc.pid,signal.SIGKILL);proc.wait();rc=124
    thread.join(3)
    return rc, scrub(tail.decode('utf-8','replace'))


def docker_json_window(path, start, end, limit=LIMIT, deadline=None):
    """Read only; never truncate/rewrite a live Docker log. Skip partial final JSON."""
    keep=bytearray(); selected=0; invalid=0; scanned=0
    path=Path(path)
    if path.is_symlink() or not path.is_file():
        return '', {'error':'missing-or-symlink'}
    opener=gzip.open if path.name.endswith('.gz') else open
    with opener(path,'rb') as handle:
        for line in handle:
            scanned+=1
            if scanned % 1024==0 and deadline and time.monotonic()>deadline:
                return keep.decode('utf-8','replace'), {'selected':selected,'invalid':invalid,'truncated_scan':True}
            try:
                record=json.loads(line)
                at=stamp(str(record['time']))
            except (ValueError,KeyError,TypeError):
                invalid+=1;continue
            if start<=at<=end:
                selected+=1
                value=(str(record['time'])+' '+str(record.get('stream',''))+' '+str(record.get('log',''))).encode('utf-8')
                keep.extend(value)
                if not value.endswith(b'\n'):keep.extend(b'\n')
                if len(keep)>limit:del keep[:-limit]
    return keep.decode('utf-8','replace'), {'selected':selected,'invalid':invalid,'scanned':scanned}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',default='.')
    parser.add_argument('--since',default='24h')
    parser.add_argument('--until',default=None)
    args=parser.parse_args()
    if os.geteuid()!=0:parser.error('run with sudo; no configuration is changed')
    end=stamp(args.until) if args.until else datetime.now(timezone.utc)
    match=re.fullmatch(r'(\d+)([hmd])',args.since)
    start=(end-timedelta(seconds=int(match[1])*{'h':3600,'m':60,'d':86400}[match[2]])) if match else stamp(args.since)
    if start>=end:parser.error('--since must precede --until')
    root=Path(args.out).resolve();root.mkdir(parents=True,exist_ok=True)
    name='taskforge-ha-'+socket.gethostname().split('.')[0]+'-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out=root/name;out.mkdir(mode=0o700)
    config=out/'docker-config';config.mkdir(mode=0o700);(config/'config.json').write_text('{}\n')
    env={**os.environ,'DOCKER_CONFIG':str(config),'TZ':'UTC','LC_ALL':'C','SYSTEMD_PAGER':'cat'}
    manifest={'started_at':datetime.now(timezone.utc).isoformat(),'since':start.isoformat(),
              'until':end.isoformat(),'commands':[], 'warnings':['Review logs for sensitive data; automatic redaction is best-effort.']}
    def collect(filename,cmd,timeout=35):
        print('COLLECT '+filename,flush=True)
        try:rc,text=run(cmd,env,timeout)
        except OSError as error:rc,text=127,type(error).__name__
        (out/filename).write_text(text)
        manifest['commands'].append({'file':filename,'returncode':rc,'bytes':len(text.encode()),'command':cmd})
        return rc,text
    project='taskforge-prod'
    envfile=Path('/etc/taskforge/taskforge.env')
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if line.startswith('COMPOSE_PROJECT_NAME='):project=line.split('=',1)[1].strip(' \"\'')
    collect('status.txt',['bash','/opt/taskforge/control/cluster.sh','status'],60)
    collect('containers.txt',['docker','ps','-a','--format','{{.Names}} | {{.Status}} | {{.Image}}'])
    for service in ('postgres','etcd','observability-api','gateway'):
        namec=project+'-'+service+'-1'
        collect(service+'-docker.txt',['docker','logs','--timestamps','--since',start.isoformat(),
                '--until',end.isoformat(),'--tail','30000',namec],60)
        if service not in ('postgres','etcd'):continue
        # Only the safe log path is inspected; no Env or application secrets.
        rc,text=collect(service+'-log-path.txt',['docker','inspect','-f','{{.LogPath}}',namec])
        if rc!=0:continue
        candidate=Path(text.strip())
        if not candidate.is_absolute() or not candidate.name.endswith('-json.log') or not re.fullmatch(r'[0-9a-f]{64}',candidate.parent.name):
            manifest['warnings'].append(service+': unsupported log path/driver; Docker output retained');continue
        # Scan existing generations without changing Docker descriptors/caches.
        paths=sorted(candidate.parent.glob(candidate.name+'.*'),key=lambda p:p.stat().st_mtime)+[candidate]
        selected=out/(service+'-json-file.txt');limit_remaining=LIMIT
        with selected.open('w') as handle:
            for path in paths[-8:]:
                try:
                    data,counts=docker_json_window(path,start,end,limit=max(1024,limit_remaining),deadline=time.monotonic()+35)
                    handle.write(scrub(data));limit_remaining=max(0,limit_remaining-len(data.encode()))
                    manifest.setdefault('raw_log_reads',[]).append({'service':service,'file':path.name,**counts})
                    if not limit_remaining:break
                except (OSError,ValueError) as error:
                    manifest['warnings'].append(service+': raw log read '+type(error).__name__)
    since=start.strftime('%Y-%m-%d %H:%M:%S');until=end.strftime('%Y-%m-%d %H:%M:%S')
    collect('system.txt',['journalctl','--utc','-u','taskforge-cluster','-u','docker','-u','containerd',
            '-u','wg-quick@wg-taskforge','-u','NetworkManager','--since',since,'--until',until,'-n','12000','-o','short-iso','--no-pager'],60)
    # _TRANSPORT spans retained boots; journalctl -k otherwise limits to current boot.
    collect('kernel.txt',['journalctl','--utc','_TRANSPORT=kernel','--since',since,'--until',until,
            '-n','6000','-o','short-iso','--no-pager'],60)
    collect('wg-handshakes.txt',['wg','show','wg-taskforge','latest-handshakes'])
    collect('wg-transfer.txt',['wg','show','wg-taskforge','transfer'])
    collect('agent-process.txt',['systemctl','show','taskforge-cluster','-p','MainPID','-p','NRestarts','-p','ExecMainStartTimestamp','-p','ExecMainStatus'])
    runtime=Path('/var/lib/taskforge/cluster')
    for file in ('ha-health.json','ha-health-history.jsonl','ha-health-history.jsonl.1','ha-decisions.jsonl','ha-decisions.jsonl.1','agent-events.json','agent-state.json','ha-timing-apply.json','ha-timing-before-v41.5.9.json'):
        path=runtime/file
        if path.is_file() and not path.is_symlink():
            (out/file).write_text(scrub(path.read_bytes()[-LIMIT:].decode('utf-8','replace')))
    manifest['completed_at']=datetime.now(timezone.utc).isoformat()
    (out/'manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    archive=root/(name+'.tar.gz')
    with tarfile.open(archive,'w:gz') as packed:packed.add(out,arcname=out.name)
    uid,gid=int(os.environ.get('SUDO_UID','0')),int(os.environ.get('SUDO_GID','0'))
    for path in [out,*out.rglob('*'),archive]:
        path.chmod(0o700 if path.is_dir() else 0o600);os.chown(path,uid,gid)
    print('ARCHIVE='+str(archive),flush=True)
    print('All collection failures/timeouts are recorded in manifest.json; no containers were changed.')
    return 0


if __name__=='__main__':
    raise SystemExit(main())
