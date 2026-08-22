"""SQLite persistence and state transitions for ScheduledQueue."""
from __future__ import annotations
import json, os, sqlite3, time, uuid
from pathlib import Path

STATUSES = ("scheduled", "dispatched", "running", "interrupted", "cancelled")
TERMINAL_HISTORY = ("done", "failed")

def _default_db_path() -> str:
    try:
        import folder_paths
        user = folder_paths.get_user_directory()
    except Exception:
        user = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "user")
    Path(user).mkdir(parents=True, exist_ok=True)
    return os.path.join(user, "scheduled_queue.sqlite3")

def _dict(row): return None if row is None else {k: row[k] for k in row.keys()}

class ScheduledQueueDB:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or _default_db_path()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, timeout=5, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        try: self._conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError: pass
        with self._conn:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_jobs (
              id TEXT PRIMARY KEY, prompt_id TEXT, payload TEXT NOT NULL,
              client_id TEXT, note TEXT, priority INTEGER NOT NULL DEFAULT 100,
              scheduled_at REAL NOT NULL, created_at REAL NOT NULL,
              dispatched_at REAL, finished_at REAL, status TEXT NOT NULL DEFAULT 'scheduled',
              error TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
              auto_retry INTEGER NOT NULL DEFAULT 0, queue_order INTEGER
            );
            CREATE TABLE IF NOT EXISTS job_history (
              id TEXT PRIMARY KEY, prompt_id TEXT, finished_at REAL NOT NULL,
              status TEXT NOT NULL, outputs TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS scheduler_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_sq_due ON scheduled_jobs(status, scheduled_at);
            """)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(scheduled_jobs)")}
            if "queue_order" not in cols:
                self._conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN queue_order INTEGER")
            self._conn.execute("UPDATE scheduled_jobs SET queue_order=rowid*1000 WHERE queue_order IS NULL")
            if self.get_state("paused") is None: self.set_state("paused", "1")

    def add_job(self, payload, scheduled_at, priority=100, note=None, client_id=None, auto_retry=0):
        jid = str(uuid.uuid4()); now = time.time()
        row = self._conn.execute("SELECT COALESCE(MAX(queue_order),0)+1000 FROM scheduled_jobs WHERE status IN ('scheduled','interrupted')").fetchone()
        order = int(row[0] or 1000)
        with self._conn:
            self._conn.execute("""INSERT INTO scheduled_jobs
              (id,payload,client_id,note,priority,scheduled_at,created_at,status,auto_retry,queue_order)
              VALUES (?,?,?,?,?,?,?,'scheduled',?,?)""", (jid,json.dumps(payload,ensure_ascii=False,separators=(",",":")),client_id,note,int(priority),float(scheduled_at),now,int(auto_retry),order))
        return jid

    def get_job(self, job_id): return _dict(self._conn.execute("SELECT * FROM scheduled_jobs WHERE id=?",(job_id,)).fetchone())

    def list_jobs(self, status_filter=None, limit=200):
        limit=max(1,min(int(limit),10000)); params=[]; where=""
        if status_filter:
            where="WHERE status IN (%s)"%(",".join("?"*len(status_filter))); params.extend(status_filter)
        rows=self._conn.execute(f"SELECT * FROM scheduled_jobs {where} ORDER BY CASE WHEN status IN ('scheduled','interrupted') THEN 0 WHEN status IN ('dispatched','running') THEN 1 ELSE 2 END, queue_order ASC, priority DESC, scheduled_at ASC, created_at ASC, id ASC LIMIT ?",(*params,limit)).fetchall()
        return [_dict(r) for r in rows]

    def list_history(self, limit=200):
        return [_dict(r) for r in self._conn.execute("SELECT * FROM job_history ORDER BY finished_at DESC LIMIT ?",(max(1,min(int(limit),10000)),)).fetchall()]

    def update_job(self, job_id, **fields):
        allowed={"status","prompt_id","client_id","note","priority","scheduled_at","dispatched_at","finished_at","error","retry_count","auto_retry","queue_order"}
        if "payload" in fields: raise ValueError("payload cannot be updated")
        bad=set(fields)-allowed
        if bad: raise ValueError(f"unknown fields: {sorted(bad)}")
        if not fields:return False
        with self._conn:
            cur=self._conn.execute("UPDATE scheduled_jobs SET "+", ".join(f"{k}=?" for k in fields)+" WHERE id=?",(*fields.values(),job_id))
        return cur.rowcount>0

    def cancel_job(self, job_id):
        with self._conn:
            cur=self._conn.execute("UPDATE scheduled_jobs SET status='cancelled', error=NULL WHERE id=? AND status IN ('scheduled','interrupted')",(job_id,))
        return cur.rowcount>0

    def reorder_job(self, job_id, direction):
        if direction not in (-1,1): raise ValueError("direction must be -1 or 1")
        with self._conn:
            row=self._conn.execute("SELECT * FROM scheduled_jobs WHERE id=? AND status IN ('scheduled','interrupted')",(job_id,)).fetchone()
            if not row:return False
            pending=self._conn.execute("SELECT id,queue_order FROM scheduled_jobs WHERE status IN ('scheduled','interrupted') ORDER BY queue_order,priority DESC,scheduled_at,created_at,id").fetchall()
            ids=[r[0] for r in pending]; i=ids.index(job_id); j=i+direction
            if j<0 or j>=len(ids): return False
            a,b=pending[i],pending[j]
            self._conn.execute("UPDATE scheduled_jobs SET queue_order=? WHERE id=?",(b[1],a[0]))
            self._conn.execute("UPDATE scheduled_jobs SET queue_order=? WHERE id=?",(a[1],b[0]))
        return True

    def get_state(self,key):
        r=self._conn.execute("SELECT value FROM scheduler_state WHERE key=?",(key,)).fetchone(); return None if r is None else r[0]
    def set_state(self,key,value):
        with self._conn:self._conn.execute("INSERT INTO scheduler_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))

    def recover_orphans(self):
        with self._conn:
            cur=self._conn.execute("UPDATE scheduled_jobs SET status='interrupted',error='orphan from previous ComfyUI run' WHERE status IN ('dispatched','running')")
        return cur.rowcount

    def claim_next_due_job(self):
        now=time.time()
        with self._conn:
            r=self._conn.execute("SELECT * FROM scheduled_jobs WHERE status='scheduled' AND scheduled_at<=? ORDER BY queue_order,priority DESC,scheduled_at,created_at,id LIMIT 1",(now,)).fetchone()
            if not r:return None
            self._conn.execute("UPDATE scheduled_jobs SET status='dispatched',dispatched_at=? WHERE id=? AND status='scheduled'",(now,r['id']))
            d=_dict(r); d.update(status='dispatched',dispatched_at=now); return d

    def mark_running(self, job_id, prompt_id):
        return self.update_job(job_id,status='running',prompt_id=prompt_id)

    def _finish(self, job_id, status, prompt_id=None, outputs=None, error=None):
        now=time.time()
        with self._conn:
            r=self._conn.execute("SELECT prompt_id FROM scheduled_jobs WHERE id=?",(job_id,)).fetchone()
            if not r:return False
            self._conn.execute("INSERT OR REPLACE INTO job_history(id,prompt_id,finished_at,status,outputs,error) VALUES(?,?,?,?,?,?)",(job_id,prompt_id or r[0],now,status,json.dumps(outputs,ensure_ascii=False,separators=(",",":")) if outputs is not None else None,error))
            self._conn.execute("DELETE FROM scheduled_jobs WHERE id=?",(job_id,))
        return True
    def mark_done(self,job_id,prompt_id=None,outputs=None): return self._finish(job_id,'done',prompt_id,outputs)
    def mark_failed(self,job_id,error): return self._finish(job_id,'failed',error=error)

    def reset_for_reschedule(self,job_id):
        with self._conn:
            cur=self._conn.execute("UPDATE scheduled_jobs SET status='scheduled',scheduled_at=?,retry_count=retry_count+1,error=NULL WHERE id=? AND status='interrupted'",(time.time(),job_id))
        return cur.rowcount>0
    def reset_all_interrupted(self):
        with self._conn:
            cur=self._conn.execute("UPDATE scheduled_jobs SET status='scheduled',scheduled_at=?,error=NULL WHERE status='interrupted'",(time.time(),))
        return cur.rowcount
    def increment_retry(self,job_id):
        with self._conn:self._conn.execute("UPDATE scheduled_jobs SET retry_count=retry_count+1 WHERE id=?",(job_id,))
        r=self._conn.execute("SELECT retry_count FROM scheduled_jobs WHERE id=?",(job_id,)).fetchone(); return int(r[0]) if r else 0
    def close(self):
        try:self._conn.close()
        except sqlite3.Error:pass
