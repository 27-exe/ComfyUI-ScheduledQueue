"""Non-blocking (to ComfyUI's event loop) scheduler worker."""
from __future__ import annotations
import json, logging, threading, time, urllib.request, urllib.error
log=logging.getLogger(__name__)
class SchedulerThread:
    POLL_INTERVAL=1.0; MAX_RETRIES=3
    def __init__(self,db,comfyui_url="http://127.0.0.1:8188"):
        self.db=db; self.comfyui_url=comfyui_url.rstrip('/'); self._stop=threading.Event(); self._thread=None
    def start(self):
        if self._thread and self._thread.is_alive(): return
        self._stop.clear(); self._thread=threading.Thread(target=self._run,name='ScheduledQueue-Scheduler',daemon=True); self._thread.start()
    def stop(self,timeout=5):
        self._stop.set()
        if self._thread:self._thread.join(timeout)
    RECONCILE_INTERVAL = 5.0
    HISTORY_TIMEOUT = 4.0

    def _run(self):
        last_reconcile = 0.0
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('scheduler tick failed')
            now = time.monotonic()
            if now - last_reconcile >= self.RECONCILE_INTERVAL:
                last_reconcile = now
                try:
                    self.reconcile()
                except Exception:
                    log.exception('scheduler reconcile failed')
            self._stop.wait(self.POLL_INTERVAL)
    def tick(self):
        if self.db.get_state('paused') != '0': return
        job=self.db.claim_next_due_job()
        if not job:return
        try:
            body={'prompt':json.loads(job['payload']),'client_id':job.get('client_id') or 'scheduled_queue'}
            req=urllib.request.Request(self.comfyui_url+'/prompt',data=json.dumps(body).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=10) as response:
                if response.status < 200 or response.status >= 300: raise RuntimeError(f'HTTP {response.status}')
                result=json.loads(response.read().decode() or '{}')
            prompt_id=result.get('prompt_id')
            if not prompt_id: raise RuntimeError('ComfyUI response did not include prompt_id')
            self.db.mark_running(job['id'],prompt_id); self.db.set_state('last_dispatch_at',str(time.time()))
            log.info('dispatched %s as %s',job['id'],prompt_id)
        except Exception as exc:
            self._dispatch_failure(job['id'],str(exc))
    def _dispatch_failure(self,job_id,error):
        n=self.db.increment_retry(job_id)
        if n > self.MAX_RETRIES:
            self.db.mark_failed(job_id,f'dispatch failed after {self.MAX_RETRIES} retries: {error}')
        else:
            self.db.update_job(job_id,status='scheduled',scheduled_at=time.time()+min(60,2**n),error=f'dispatch retry {n}/{self.MAX_RETRIES}: {error}')
        self.db.set_state('last_error',error)
    def reconcile(self,history_fetcher=None):
        """Reconcile running jobs from ComfyUI history. Unknown remains running."""
        for job in self.db.list_jobs(['running'],10000):
            try:
                record=history_fetcher(job['prompt_id']) if history_fetcher else self._history(job['prompt_id'])
                if not record: continue
                status=str(record.get('status','')).lower(); outputs=record.get('outputs')
                if status in ('success','completed','done'): self.db.mark_done(job['id'],job['prompt_id'],outputs)
                elif status in ('error','failed','failure'): self.db.mark_failed(job['id'],str(record.get('error') or 'ComfyUI reported failure'))
            except Exception: log.exception('reconcile failed for %s',job['id'])
    def _history(self,prompt_id):
        req=urllib.request.Request(self.comfyui_url+'/history/'+urllib.parse.quote(prompt_id))
        try:
            with urllib.request.urlopen(req,timeout=self.HISTORY_TIMEOUT) as r:
                data=json.loads(r.read().decode() or '{}')
            return data.get(prompt_id) or data
        except (urllib.error.HTTPError,urllib.error.URLError): return None
