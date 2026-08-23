"""SQLite persistence and state transitions for ScheduledQueue."""
from __future__ import annotations
import json, os, sqlite3, time, uuid
from pathlib import Path

STATUSES = ("scheduled", "dispatched", "running", "interrupted", "cancelled")
TERMINAL_HISTORY = ("done", "failed")

# Conservative default wall-clock estimate (seconds) for how long a ComfyUI
# workflow takes from dispatch to finish. Used by ``list_jobs_paginated`` to
# synthesise a synthetic ``dispatched_at`` for legacy ``job_history`` rows
# archived before v0.3.11 added the column — there is no real timestamp to
# recover, so we pick a small, round number that keeps the sidebar's
# "完成于 HH:MM:SS · Ns" duration row honest (it under-reports rather than
# invents a wildly different time). 30s lines up with typical SDXL sampling
# runs; the sidebar tags the result with ``dispatched_at_estimated=True`` so
# future telemetry / better backfills can replace it without losing info.
_LEGACY_DISPATCHED_AT_ESTIMATE = 30.0

def _default_db_path() -> str:
    try:
        import folder_paths
        user = folder_paths.get_user_directory()
    except Exception:
        user = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "user")
    Path(user).mkdir(parents=True, exist_ok=True)
    return os.path.join(user, "scheduled_queue.sqlite3")

def _dict(row): return None if row is None else {k: row[k] for k in row.keys()}

def _with_duration(row):
    if row is None:
        return None
    data = _dict(row)
    started = data.get("started_at")
    end = data.get("finished_at")
    data["duration"] = (None if started is None else max(0.0, float(((end if end is not None else time.time()) - started))))
    return data

def _safe_json(value):
    """Decode a JSON column into a Python object, returning None on
    empty / malformed values rather than raising. Used by the Stage 3
    endpoints so the public API only ever sees decoded payloads."""
    if value is None or value == "":
        return None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value

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
              dispatched_at REAL, started_at REAL, finished_at REAL, status TEXT NOT NULL DEFAULT 'scheduled',
              error TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
              auto_retry INTEGER NOT NULL DEFAULT 0, queue_order INTEGER,
              workflow_title TEXT
            );
            CREATE TABLE IF NOT EXISTS job_history (
              id TEXT PRIMARY KEY, prompt_id TEXT, finished_at REAL NOT NULL,
              status TEXT NOT NULL, outputs TEXT, error TEXT, payload TEXT,
              workflow_title TEXT, dispatched_at REAL, started_at REAL
            );
            CREATE TABLE IF NOT EXISTS scheduler_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_sq_due ON scheduled_jobs(status, scheduled_at);
            """)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(scheduled_jobs)")}
            if "queue_order" not in cols:
                self._conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN queue_order INTEGER")
            self._conn.execute("UPDATE scheduled_jobs SET queue_order=rowid*1000 WHERE queue_order IS NULL")
            # v0.3.10: track the ComfyUI workflow filename alongside every job so
            # the sidebar can show "which workflow" without re-fetching payload.
            # Older DBs need an ALTER. Stays NULL / empty for legacy rows.
            if "workflow_title" not in cols:
                self._conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN workflow_title TEXT")
            if "started_at" not in cols:
                self._conn.execute("ALTER TABLE scheduled_jobs ADD COLUMN started_at REAL")
            # job_history gained a `payload` column in v0.3.8 so finished jobs
            # can be re-submitted via repeat_job. Older DBs need a migration.
            hcols = {r[1] for r in self._conn.execute("PRAGMA table_info(job_history)")}
            if "payload" not in hcols:
                self._conn.execute("ALTER TABLE job_history ADD COLUMN payload TEXT")
            # Mirror workflow_title on the history row so sidebar lists still
            # display "which workflow" after the live row is archived.
            if "workflow_title" not in hcols:
                self._conn.execute("ALTER TABLE job_history ADD COLUMN workflow_title TEXT")
            # Mirror dispatched_at on the history row so the sidebar's
            # "完成于 —" field can show when the live row was actually
            # handed off to ComfyUI (NULL for jobs that were cancelled /
            # failed before they ever dispatched).
            if "dispatched_at" not in hcols:
                self._conn.execute("ALTER TABLE job_history ADD COLUMN dispatched_at REAL")
            if "started_at" not in hcols:
                self._conn.execute("ALTER TABLE job_history ADD COLUMN started_at REAL")
            if self.get_state("paused") is None: self.set_state("paused", "1")

    def add_job(self, payload, scheduled_at, priority=100, note=None, client_id=None, auto_retry=0, workflow_title=None):
        jid = str(uuid.uuid4()); now = time.time()
        row = self._conn.execute("SELECT COALESCE(MAX(queue_order),0)+1000 FROM scheduled_jobs WHERE status IN ('scheduled','interrupted')").fetchone()
        order = int(row[0] or 1000)
        # Normalise: empty string == "no title" == NULL. Storing NULL keeps the
        # column tidy for legacy rows and lets the sidebar fall back to the
        # node-derived nickname / note when no filename is known.
        wtitle = workflow_title if isinstance(workflow_title, str) and workflow_title else None
        with self._conn:
            self._conn.execute("""INSERT INTO scheduled_jobs
              (id,payload,client_id,note,priority,scheduled_at,created_at,status,auto_retry,queue_order,workflow_title)
              VALUES (?,?,?,?,?,?,?,'scheduled',?,?,?)""", (jid,json.dumps(payload,ensure_ascii=False,separators=(",",":")),client_id,note,int(priority),float(scheduled_at),now,int(auto_retry),order,wtitle))
        return jid

    def get_job(self, job_id): return _with_duration(self._conn.execute("SELECT * FROM scheduled_jobs WHERE id=?",(job_id,)).fetchone())

    def list_jobs(self, status_filter=None, limit=200):
        limit=max(1,min(int(limit),10000)); params=[]; where=""
        if status_filter:
            where="WHERE status IN (%s)"%(",".join("?"*len(status_filter))); params.extend(status_filter)
        rows=self._conn.execute(f"SELECT * FROM scheduled_jobs {where} ORDER BY CASE WHEN status IN ('scheduled','interrupted') THEN 0 WHEN status IN ('dispatched','running') THEN 1 ELSE 2 END, queue_order ASC, priority DESC, scheduled_at ASC, created_at ASC, id ASC LIMIT ?",(*params,limit)).fetchall()
        return [_with_duration(r) for r in rows]

    def list_history(self, limit=200):
        return [_with_duration(r) for r in self._conn.execute("SELECT * FROM job_history ORDER BY finished_at DESC LIMIT ?",(max(1,min(int(limit),10000)),)).fetchall()]

    # --- pagination/listing/clear for the v0.3.8+ list endpoint -----------------
    # status values come from both tables:
    #   scheduled_jobs: scheduled, dispatched, running, interrupted, cancelled
    #   job_history   : done, failed
    # `count_jobs` / `list_jobs_paginated` / `clear_by_status` therefore have to
    # look at both stores. They treat `status` as a virtual key: each store is
    # queried only for statuses it actually holds.

    _STATUS_IN_SCHEDULED = ("scheduled", "dispatched", "running", "interrupted", "cancelled")
    _STATUS_IN_HISTORY   = ("done", "failed")

    def _split_statuses(self, statuses):
        in_sched = tuple(s for s in statuses if s in self._STATUS_IN_SCHEDULED)
        in_hist  = tuple(s for s in statuses if s in self._STATUS_IN_HISTORY)
        return in_sched, in_hist

    def count_jobs(self, statuses=None):
        """Return the number of rows whose status is in `statuses`.

        `statuses` is a list/tuple of status strings; pass None or an empty
        iterable to count everything in both stores.
        """
        sched, hist = self._split_statuses(statuses or ())
        total = 0
        if not statuses:
            # Count both tables in one shot.
            total += self._conn.execute("SELECT COUNT(*) FROM scheduled_jobs").fetchone()[0]
            total += self._conn.execute("SELECT COUNT(*) FROM job_history").fetchone()[0]
            return int(total)
        if sched:
            placeholders = ",".join("?" * len(sched))
            total += int(self._conn.execute(
                f"SELECT COUNT(*) FROM scheduled_jobs WHERE status IN ({placeholders})",
                sched,
            ).fetchone()[0])
        if hist:
            placeholders = ",".join("?" * len(hist))
            total += int(self._conn.execute(
                f"SELECT COUNT(*) FROM job_history WHERE status IN ({placeholders})",
                hist,
            ).fetchone()[0])
        return total

    def list_jobs_paginated(self, statuses=None, limit=50, offset=0):
        """Paginated, status-filtered view across scheduled_jobs + job_history.

        Returned list is ordered: live jobs first (by queue_order, priority,
        scheduled_at, created_at, id) followed by history rows (most-recently
        finished first). The unified ordering matches the frontend's mental
        model of "active queue at the top, completed items underneath".
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        sched, hist = self._split_statuses(statuses or ())

        out = []
        if sched:
            placeholders = ",".join("?" * len(sched))
            rows = self._conn.execute(
                f"SELECT * FROM scheduled_jobs WHERE status IN ({placeholders}) "
                f"ORDER BY CASE WHEN status IN ('scheduled','interrupted') THEN 0 "
                f"WHEN status IN ('dispatched','running') THEN 1 ELSE 2 END, "
                f"queue_order ASC, priority DESC, scheduled_at ASC, created_at ASC, id ASC "
                f"LIMIT ? OFFSET ?",
                (*sched, limit, offset),
            ).fetchall()
            out.extend(_with_duration(r) for r in rows)
        if hist:
            placeholders = ",".join("?" * len(hist))
            rows = self._conn.execute(
                f"SELECT * FROM job_history WHERE status IN ({placeholders}) "
                f"ORDER BY finished_at DESC LIMIT ? OFFSET ?",
                (*hist, limit, offset),
            ).fetchall()
            out.extend(_with_duration(r) for r in rows)
        if not sched and not hist:
            # No status filter (or every provided status was unknown): return
            # rows from BOTH stores. scheduled_jobs keeps its live-first
            # ordering; job_history is appended most-recent-finished first.
            rows = self._conn.execute(
                "SELECT * FROM scheduled_jobs "
                "ORDER BY CASE WHEN status IN ('scheduled','interrupted') THEN 0 "
                "WHEN status IN ('dispatched','running') THEN 1 ELSE 2 END, "
                "queue_order ASC, priority DESC, scheduled_at ASC, created_at ASC, id ASC "
                "LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            out.extend(_with_duration(r) for r in rows)
            rows = self._conn.execute(
                "SELECT * FROM job_history "
                "ORDER BY finished_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            out.extend(_with_duration(r) for r in rows)

        # job_history stores `outputs` as a JSON string and `payload` as a
        # JSON string; decode them here so callers (and the HTTP layer that
        # re-serialises via json.dumps) get Python objects. The live table
        # also stores `payload` as JSON but only `outputs` matters for the
        # list endpoint — `payload` is stripped by routes._strip_payload
        # before it leaves the server, so we leave it alone on live rows
        # to avoid surprising existing tests that expect raw strings.
        for row in out:
            if row.get("status") in TERMINAL_HISTORY:
                row["outputs"] = _safe_json(row.get("outputs"))
                # Legacy rows (archived before v0.3.11 added `dispatched_at`
                # to job_history) carry NULL in `dispatched_at` because the
                # column didn't exist when they were inserted; the ALTER
                # TABLE migration above backfills the column itself, but
                # leaves every pre-existing row at NULL. Synthesise a
                # synthetic dispatched_at by subtracting a conservative
                # default run-time estimate so the sidebar can still show
                # "完成于 HH:MM:SS · ~Xs" instead of dropping the duration
                # component. We never overwrite a real value here, and the
                # JSON we return to callers marks the synthesis so future
                # telemetry can distinguish real vs. estimated starts.
                if row.get("dispatched_at") is None and row.get("finished_at") is not None:
                    row["dispatched_at"] = max(
                        0.0, float(row["finished_at"]) - _LEGACY_DISPATCHED_AT_ESTIMATE
                    )
                    row["dispatched_at_estimated"] = True

        return out

    def get_job_with_outputs(self, job_id):
        """Return one job, merged with its outputs if it lives in job_history.

        Live jobs (``scheduled_jobs``) get their row plus a synthetic
        ``outputs`` field of ``None`` so the frontend always sees the same
        shape. History rows keep their stored JSON ``outputs`` (or None).

        Both ``payload`` and ``outputs`` columns are returned as decoded
        Python objects (dicts / lists) instead of raw JSON strings, since
        the public API serialises the result with json.dumps anyway.

        Returns None if no such job exists in either table.
        """
        row = self.get_job(job_id)
        if row is not None:
            row["outputs"] = None
            row["payload"] = _safe_json(row.get("payload"))
            return row
        h = self._conn.execute(
            "SELECT * FROM job_history WHERE id=?", (job_id,),
        ).fetchone()
        if h is None:
            return None
        d = _with_duration(h)
        if d is None:
            return None
        # job_history rows don't store note/priority — fill None so the
        # API response shape stays consistent with the live job variant.
        d["payload"] = _safe_json(d.get("payload"))
        d["outputs"] = _safe_json(d.get("outputs"))
        d.setdefault("note", None)
        d.setdefault("priority", None)
        return d

    def clear_by_status(self, statuses):
        """Delete every row whose status is in `statuses`. Returns the count.

        Removes matching rows from BOTH ``scheduled_jobs`` (cancelled,
        interrupted, ...) and ``job_history`` (done, failed, ...).
        """
        sched, hist = self._split_statuses(statuses or ())
        removed = 0
        with self._conn:
            if sched:
                placeholders = ",".join("?" * len(sched))
                removed += self._conn.execute(
                    f"DELETE FROM scheduled_jobs WHERE status IN ({placeholders})",
                    sched,
                ).rowcount
            if hist:
                placeholders = ",".join("?" * len(hist))
                removed += self._conn.execute(
                    f"DELETE FROM job_history WHERE status IN ({placeholders})",
                    hist,
                ).rowcount
        return int(removed)

    def repeat_job(self, job_id, priority=100, scheduled_at=None):
        """Resurrect a finished job (typically from job_history) as a fresh
        ``scheduled_jobs`` row carrying the same payload.

        Returns the new job's id, or None if no source job (history or live)
        carries a payload we can copy.

        ``workflow_title`` is propagated from the source row (history or live)
        so the new entry keeps the sidebar's "which workflow" association.
        """
        src = self._conn.execute(
            "SELECT payload, workflow_title FROM job_history WHERE id=?", (job_id,),
        ).fetchone()
        wtitle = None
        if src is None:
            live = self._conn.execute(
                "SELECT payload, workflow_title FROM scheduled_jobs WHERE id=?", (job_id,),
            ).fetchone()
            if live is None or not live["payload"]:
                return None
            payload = json.loads(live["payload"])
            wtitle = live["workflow_title"]
        else:
            payload = json.loads(src["payload"]) if src["payload"] else None
            wtitle = src["workflow_title"]
        if payload is None:
            return None
        return self.add_job(
            payload=payload,
            scheduled_at=time.time() if scheduled_at is None else float(scheduled_at),
            priority=int(priority),
            note=f"repeat of {job_id[:8]}",
            workflow_title=wtitle,
        )

    def update_job(self, job_id, **fields):
        allowed={"status","prompt_id","client_id","note","priority","scheduled_at","dispatched_at","finished_at","error","retry_count","auto_retry","queue_order","workflow_title"}
        if "payload" in fields: raise ValueError("payload cannot be updated")
        bad=set(fields)-allowed
        if bad: raise ValueError(f"unknown fields: {sorted(bad)}")
        # Normalise empty-string workflow_title to NULL to keep the column tidy.
        if "workflow_title" in fields:
            v = fields["workflow_title"]
            if v is None:
                fields["workflow_title"] = None
            elif isinstance(v, str) and v == "":
                fields["workflow_title"] = None
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
            # Priority must dominate scheduled_at: a high-priority job due
            # later still jumps ahead of a low-priority job already due.
            # Explicit ASC on scheduled_at mirrors the same intent in code.
            r=self._conn.execute(
                "SELECT * FROM scheduled_jobs "
                "WHERE status='scheduled' AND scheduled_at<=? "
                "ORDER BY queue_order, priority DESC, scheduled_at ASC, created_at, id "
                "LIMIT 1",
                (now,),
            ).fetchone()
            if not r:return None
            self._conn.execute("UPDATE scheduled_jobs SET status='dispatched',dispatched_at=? WHERE id=? AND status='scheduled'",(now,r['id']))
            d=_dict(r); d.update(status='dispatched',dispatched_at=now); return d

    def mark_running(self, job_id, prompt_id):
        now = time.time()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE scheduled_jobs SET status='running', prompt_id=?, started_at=? WHERE id=? AND status='dispatched'",
                (prompt_id, now, job_id),
            )
        return cur.rowcount > 0

    def mark_dispatched(self, job_id, prompt_id):
        """Flip status='dispatched' and stamp ``dispatched_at``.

        Called immediately after the POST /prompt response carries a
        ComfyUI ``prompt_id`` back. The job is now sitting in ComfyUI's
        native queue (possibly behind another job) but has not yet been
        picked up for execution — ``reconcile`` is responsible for
        promoting it to ``running`` once ``/queue`` shows it as the
        currently-running item.

        ``dispatched_at`` is re-stamped here (claim_next_due_job may have
        already set it at claim time) so the value reflects the actual
        POST time, which is what the UI's "已派发" duration counts from.

        ``prompt_id`` is the value ComfyUI returned; we trust it (ComfyUI
        is the source of truth for prompt identity).
        """
        now = time.time()
        with self._conn:
            cur = self._conn.execute(
                "UPDATE scheduled_jobs "
                "SET status='dispatched', prompt_id=?, dispatched_at=? "
                "WHERE id=?",
                (prompt_id, now, job_id),
            )
        return cur.rowcount > 0

    def _finish(self, job_id, status, prompt_id=None, outputs=None, error=None):
        now=time.time()
        with self._conn:
            # Read dispatched_at from the live row so the history row mirrors
            # "when this job actually started running" (NULL for jobs that
            # finished without ever being dispatched — e.g. auto-retry
            # cancellations handled before claim_next_due_job).
            r=self._conn.execute("SELECT prompt_id,payload,workflow_title,dispatched_at,started_at FROM scheduled_jobs WHERE id=?",(job_id,)).fetchone()
            if not r:return False
            self._conn.execute("INSERT OR REPLACE INTO job_history(id,prompt_id,finished_at,status,outputs,error,payload,workflow_title,dispatched_at,started_at) VALUES(?,?,?,?,?,?,?,?,?,?)",(job_id,prompt_id or r[0],now,status,json.dumps(outputs,ensure_ascii=False,separators=(",",":")) if outputs is not None else None,error,r['payload'],r['workflow_title'],r['dispatched_at'],r['started_at']))
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
    def reclaim_dispatched(self):
        """Return POSTed-but-not-started jobs to the scheduler queue."""
        with self._conn:
            cur = self._conn.execute(
                "UPDATE scheduled_jobs SET status='scheduled', prompt_id=NULL, dispatched_at=NULL, started_at=NULL, scheduled_at=? WHERE status='dispatched'",
                (time.time(),),
            )
        return cur.rowcount

    def increment_retry(self,job_id):
        with self._conn:self._conn.execute("UPDATE scheduled_jobs SET retry_count=retry_count+1 WHERE id=?",(job_id,))
        r=self._conn.execute("SELECT retry_count FROM scheduled_jobs WHERE id=?",(job_id,)).fetchone(); return int(r[0]) if r else 0
    def close(self):
        try:self._conn.close()
        except sqlite3.Error:pass
