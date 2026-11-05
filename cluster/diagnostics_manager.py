from __future__ import annotations
import hashlib, hmac, json, os, re, secrets, subprocess, threading, time
from datetime import datetime, timezone
from pathlib import Path

TOKEN_PURPOSE=b'taskforge-cluster-agent-diagnostics-v1'
SINCE_RE=re.compile(r'^[1-9][0-9]{0,3}[smhdw]$')
JOB_RE=re.compile(r'^[A-Za-z0-9_-]{8,80}$')


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def diagnostics_token(env):
    key=str(env.get('TASKFORGE_INTERNAL_KEY',''))
    if len(key)<40:
        return ''
    return hmac.new(key.encode(),TOKEN_PURPOSE,hashlib.sha256).hexdigest()


class DiagnosticsError(Exception):
    def __init__(self,status,code,message):
        super().__init__(message); self.status=status; self.code=code; self.message=message


class DiagnosticsManager:
    def __init__(self,root:Path,runtime:Path,node_id:str,env:dict[str,str]):
        self.root=root; self.runtime=runtime; self.node_id=node_id; self.env=env
        self.output_dir=runtime/'diagnostics'; self.output_dir.mkdir(parents=True,exist_ok=True)
        try: os.chmod(self.output_dir,0o700)
        except OSError: pass
        self.collector=root/'scripts/diagnostics/collect-taskforge-diagnostics.sh'
        self.lock=threading.RLock(); self.jobs={}; self.active=None
        self.cleanup()

    def is_busy(self):
        with self.lock:
            return bool(self.active and self.jobs.get(self.active,{}).get('status') in {'queued','running'})

    def cleanup(self):
        try:
            files=sorted(self.output_dir.glob('taskforge_diagnostics_*.tar.gz'),key=lambda p:p.stat().st_mtime,reverse=True)
            now=time.time()
            for i,p in enumerate(files):
                if i>=8 or now-p.stat().st_mtime>3*86400:
                    try:p.unlink()
                    except OSError:pass
            for p in self.output_dir.glob('.diag-*.progress.json'):
                try:
                    if now-p.stat().st_mtime>86400:p.unlink()
                except OSError:pass
        except OSError:pass

    def start(self,mode='standard',since='6h',max_log_mb=32):
        mode=str(mode or 'standard').strip().lower(); since=str(since or '6h').strip().lower()
        if mode not in {'quick','standard','full'}: raise DiagnosticsError(400,'DIAGNOSTICS_MODE_INVALID','mode must be quick, standard or full')
        if not SINCE_RE.fullmatch(since): raise DiagnosticsError(400,'DIAGNOSTICS_SINCE_INVALID','since must look like 30m, 6h, 2d or 1w')
        try:max_log_mb=int(max_log_mb)
        except Exception: raise DiagnosticsError(400,'DIAGNOSTICS_MAX_LOG_INVALID','max_log_mb must be an integer')
        if not 1<=max_log_mb<=512: raise DiagnosticsError(400,'DIAGNOSTICS_MAX_LOG_INVALID','max_log_mb must be between 1 and 512')
        with self.lock:
            if self.active and self.jobs.get(self.active,{}).get('status') in {'queued','running'}:
                raise DiagnosticsError(409,'DIAGNOSTICS_BUSY',f'diagnostics already running: {self.active}')
            job_id=f"diag-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
            job={
                'job_id':job_id,'node':self.node_id,'status':'queued','mode':mode,'since':since,'max_log_mb':max_log_mb,
                'created_at':utc_now(),'started_at':None,'finished_at':None,'archive_name':None,'size_bytes':None,'message':'queued',
                'progress':{
                    'phase':'queued','step':'queued','completed':0,'total':0,'percent':None,
                    'current_item':'','current_index':0,'containers_completed':0,'containers_total':0,
                    'files_collected':0,'bytes_collected':0,'archive_bytes':0,'updated_at':utc_now(),
                },
            }
            self.jobs[job_id]=job; self.active=job_id
            while len(self.jobs)>20:
                oldest=next(iter(self.jobs));
                if oldest==job_id:break
                self.jobs.pop(oldest,None)
        threading.Thread(target=self._run,args=(job_id,),name=f'diagnostics-{job_id}',daemon=True).start()
        return self.status(job_id)

    def _progress_path(self,job_id):
        return self.output_dir/f'.{job_id}.progress.json'

    def _read_progress(self,path:Path):
        try:
            raw=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(raw,dict):return None
            return raw
        except (OSError,ValueError,TypeError):
            return None

    def _apply_progress(self,job_id,progress):
        if not progress:return
        with self.lock:
            job=self.jobs.get(job_id)
            if not job or job.get('status') not in {'queued','running'}:return
            job['progress']=dict(progress)
            phase=str(progress.get('phase') or '').strip()
            current=str(progress.get('current_item') or '').strip()
            message=str(progress.get('message') or '').strip()
            if current:job['message']=current
            elif message:job['message']=message
            elif phase:job['message']=phase

    def _run(self,job_id):
        with self.lock:
            job=self.jobs[job_id]; job['status']='running';job['started_at']=utc_now();job['message']='collecting'
            mode=job['mode'];since=job['since'];max_log_mb=job['max_log_mb']
        print(f'[TFDIAG START] node={self.node_id} job={job_id} mode={mode} since={since} max_log_mb={max_log_mb}',flush=True)
        progress_path=self._progress_path(job_id)
        try:progress_path.unlink()
        except OSError:pass
        args=['bash',str(self.collector),'--out',str(self.output_dir),'--progress-file',str(progress_path)]
        if mode=='quick':args+=['--quick','--since',since,'--max-log-mb',str(max_log_mb)]
        elif mode=='full':args+=['--full-logs']
        else:args+=['--since',since,'--max-log-mb',str(max_log_mb)]
        proc=None
        try:
            env=os.environ.copy(); env.setdefault('TASKFORGE_CONFIG_ROOT','/etc/taskforge')
            proc=subprocess.Popen(args,text=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=env)
            deadline=time.monotonic()+2700
            last_mtime_ns=-1
            while proc.poll() is None:
                if time.monotonic()>=deadline:
                    proc.kill()
                    raise TimeoutError('diagnostics collector timed out after 2700 seconds')
                try:
                    stamp=progress_path.stat().st_mtime_ns
                except OSError:
                    stamp=-1
                if stamp>=0 and stamp!=last_mtime_ns:
                    self._apply_progress(job_id,self._read_progress(progress_path));last_mtime_ns=stamp
                time.sleep(0.20)
            stdout,stderr=proc.communicate(timeout=5)
            self._apply_progress(job_id,self._read_progress(progress_path))
            if proc.returncode!=0:
                msg=(stderr or stdout or f'collector exit {proc.returncode}').strip().replace('\n',' ')[:1200]
                raise RuntimeError(msg)
            lines=[x.strip() for x in (stdout or '').splitlines() if x.strip()]
            archive=Path(lines[-1]).resolve() if lines else None
            if archive is None or archive.parent!=self.output_dir.resolve() or not archive.is_file() or not archive.name.startswith('taskforge_diagnostics_') or archive.suffixes[-2:]!=['.tar','.gz']:
                raise RuntimeError('collector did not return a valid archive path')
            size=archive.stat().st_size
            progress=self._read_progress(progress_path) or {}
            progress.update({'phase':'completed','step':'completed','percent':100,'archive_bytes':size,'message':'ready','updated_at':utc_now()})
            with self.lock:
                job=self.jobs[job_id];job.update(status='completed',finished_at=utc_now(),archive_name=archive.name,size_bytes=size,message='ready',progress=progress)
                if self.active==job_id:self.active=None
            self.cleanup()
            print(f'[TFDIAG DONE] node={self.node_id} job={job_id} file={archive.name} bytes={size}',flush=True)
        except Exception as e:
            if proc is not None and proc.poll() is None:
                try:proc.kill()
                except OSError:pass
            progress=self._read_progress(progress_path) or {}
            progress.update({'phase':'failed','step':'failed','message':str(e)[:1200],'updated_at':utc_now()})
            with self.lock:
                job=self.jobs[job_id];job.update(status='failed',finished_at=utc_now(),message=str(e)[:1200],progress=progress)
                if self.active==job_id:self.active=None
            print(f'[TFDIAG FAIL] node={self.node_id} job={job_id} error={type(e).__name__}:{str(e)[:600]}',flush=True)
        finally:
            try:progress_path.unlink()
            except OSError:pass

    def status(self,job_id):
        if not JOB_RE.fullmatch(str(job_id or '')): raise DiagnosticsError(400,'DIAGNOSTICS_JOB_INVALID','invalid diagnostics job id')
        with self.lock:
            job=self.jobs.get(job_id)
            if not job: raise DiagnosticsError(404,'DIAGNOSTICS_JOB_NOT_FOUND','diagnostics job not found')
            result=dict(job)
            if isinstance(job.get('progress'),dict):result['progress']=dict(job['progress'])
            return result

    def archive(self,job_id):
        job=self.status(job_id)
        if job['status']!='completed' or not job.get('archive_name'): raise DiagnosticsError(409,'DIAGNOSTICS_NOT_READY','diagnostics archive is not ready')
        path=(self.output_dir/job['archive_name']).resolve()
        if path.parent!=self.output_dir.resolve() or not path.is_file(): raise DiagnosticsError(404,'DIAGNOSTICS_ARCHIVE_MISSING','diagnostics archive is missing')
        return path
