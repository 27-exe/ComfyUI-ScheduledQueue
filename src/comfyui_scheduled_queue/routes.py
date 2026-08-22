"""
ComfyUI-ScheduledQueue - HTTP API endpoints (Stage 1 + Stage 2 + Stage 3)

Implements spec section 5 (Stage 1), section 3 (Stage 2), and the v0.3.8
backend feature flush (Stage 3 — batch add / paginated list / clear /
repeat / export / per-job detail).

Endpoints (all under /api/schedule/*):
    GET    /api/schedule/list               - list jobs (filter + paginate)
    POST   /api/schedule/add                - create a new scheduled job
    POST   /api/schedule/add-batch          - bulk create (Stage 3)
    POST   /api/schedule/cancel/{id}        - soft-delete (status='cancelled')
    POST   /api/schedule/update/{id}        - patch whitelisted fields only
    POST   /api/schedule/reorder/{id}       - move job up/down in queue
    GET    /api/schedule/job/{id}           - one job incl. outputs (Stage 3)
    GET    /api/schedule/export/{id}        - payload as JSON download (Stage 3)
    DELETE /api/schedule/clear              - wipe rows by status (Stage 3)
    POST   /api/schedule/repeat/{id}        - clone as new job (Stage 3)
    GET    /api/schedule/status             - global scheduler status snapshot
    POST   /api/schedule/pause-all          - Stage 2: pause scheduler
    POST   /api/schedule/resume-all         - Stage 2: resume + reset interrupted jobs
    POST   /api/schedule/run-now/{id}       - Stage 2: immediate reschedule of a job
    GET    /api/schedule/orphan-status      - Stage 2: interrupted job inventory

Module-level rules (spec section 11 risk table):
- DO NOT import server / aiohttp.web / PromptServer at module level.
  All ComfyUI/aiohttp access is inside setup_routes() and the handlers,
  because custom_nodes are imported BEFORE PromptServer.instance exists.
- All responses go through web.json_response().
- Error envelope: {"error": "<message>"}, status codes 400 / 404 / 500.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

# NOTE: keep module imports minimal. aiohttp.web + server.PromptServer are
# imported lazily inside setup_routes() and the handlers.
#
# Sentinel so test code can do `patch.object(routes, 'PromptServer')` per
# the spec verification script. The real import happens inside setup_routes().
try:
    from server import PromptServer  # type: ignore
except Exception:
    class PromptServer:  # type: ignore[no-redef]
        instance = None

_log = logging.getLogger("scheduled_queue.routes")

# Fields the /update endpoint is allowed to write. Status and payload are
# intentionally excluded (spec section 5.2 update endpoint constraints).
_UPDATE_ALLOWED_FIELDS = frozenset({"scheduled_at", "priority", "note", "auto_retry"})

# Per-endpoint limit cap for /list. Spec (Stage 3): default 50, max 200.
_LIST_MAX_LIMIT = 200
_LIST_DEFAULT_LIMIT = 50

# Status counts returned by /status. Keys must match spec section 5.2.
_STATUS_COUNT_KEYS = (
    "scheduled",
    "running",
    "interrupted",
    "done",
    "failed",
    "cancelled",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bad_request(reason: str):
    """Lazy-import web so the module is importable without aiohttp at parse time."""
    try:
        from aiohttp import web
        return web.json_response({"error": reason}, status=400)
    except Exception:
        return _StubResponse({"error": reason}, 400)


def _not_found(reason: str = "job not found"):
    try:
        from aiohttp import web
        return web.json_response({"error": reason}, status=404)
    except Exception:
        return _StubResponse({"error": reason}, 404)


def _server_error(reason: str = "internal error"):
    try:
        from aiohttp import web
        return web.json_response({"error": reason}, status=500)
    except Exception:
        return _StubResponse({"error": reason}, 500)


def _json_response(data: Any, status: int = 200):
    try:
        from aiohttp import web
        return web.json_response(data, status=status)
    except Exception:
        return _StubResponse(data, status)


class _StubResponse:
    """Drop-in replacement for aiohttp.web.Response used only when
    aiohttp isn't installed (e.g. unit tests). The handler tests poke
    .status and .body directly without going through aiohttp."""

    def __init__(self, data: Any, status: int = 200):
        self.status = status
        if isinstance(data, (bytes, bytearray)):
            self.body = bytes(data)
        elif isinstance(data, str):
            self.body = data.encode()
        else:
            self.body = json.dumps(data, ensure_ascii=False).encode()
        self._data = data
        self.headers: dict[str, str] = {}

    def __repr__(self) -> str:
        return f"_StubResponse(status={self.status}, body={self.body[:80]!r})"


def _parse_int(value: str | None, default: int, *, field: str) -> int:
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be an integer")


def _parse_status_filter(raw: str | None) -> list[str] | None:
    """Parse ?status=scheduled,interrupted into a list. None / empty -> no filter."""
    if raw is None:
        return None
    parts = [s.strip() for s in raw.split(",") if s.strip()]
    return parts or None


def _strip_payload(row: dict) -> dict:
    """Drop the heavy `payload` field from a job row before returning it
    to the HTTP client. payload is only retrievable via the future
    /api/schedule/job/{id} endpoint (Stage 2)."""
    if "payload" in row:
        row = {k: v for k, v in row.items() if k != "payload"}
    return row


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

async def list_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """GET /api/schedule/list

    Query params (all optional):
      status  - comma-separated status filter
                (scheduled|dispatched|running|interrupted|cancelled|done|failed)
      limit   - 1..200, default 50
      offset  - >=0, default 0
    Response:
      {"jobs": [...stripped rows...],
       "total": <total matching the status filter>,
       "limit": <echoed back>,
       "offset": <echoed back>,
       "has_more": <total > offset+len(jobs)>}
    payload is never returned here — fetch /api/schedule/job/{id} for full
    details (Stage 3).
    """
    db = request.app.get("sq_db")  # set in setup_routes()
    if db is None:
        return _server_error("db not initialized")

    try:
        status_filter = _parse_status_filter(request.query.get("status"))
        limit = _parse_int(request.query.get("limit"), _LIST_DEFAULT_LIMIT, field="limit")
        limit = max(1, min(limit, _LIST_MAX_LIMIT))
        offset = _parse_int(request.query.get("offset"), 0, field="offset")
        offset = max(0, offset)
    except ValueError as e:
        return _bad_request(str(e))

    try:
        total = db.count_jobs(status_filter)
        rows = db.list_jobs_paginated(
            statuses=status_filter, limit=limit, offset=offset,
        )
    except Exception:
        _log.exception("list_jobs failed")
        return _server_error("database error")

    jobs = [_strip_payload(r) for r in rows]
    return _json_response({
        "jobs": jobs,
        "total": int(total),
        "limit": limit,
        "offset": offset,
        "has_more": (offset + len(jobs)) < int(total),
    })


async def add_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/add

    Required body: payload (dict) + scheduled_at (positive float).
    Optional: priority (int), note (str), auto_retry (int), client_id (str).
    Returns 201 + {id, scheduled_at, status}.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _bad_request("body must be valid JSON")
    except Exception:
        return _bad_request("body must be valid JSON")

    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")

    # payload: required, must be a dict
    payload = body.get("payload")
    if payload is None:
        return _bad_request("payload is required")
    if not isinstance(payload, dict):
        return _bad_request("payload must be a dict")

    # scheduled_at: required, positive float
    scheduled_at = body.get("scheduled_at")
    if scheduled_at is None:
        return _bad_request("scheduled_at is required")
    if not isinstance(scheduled_at, (int, float)):
        return _bad_request("scheduled_at must be a number")
    if isinstance(scheduled_at, bool) or not float(scheduled_at) > 0:
        return _bad_request("scheduled_at must be a positive float")

    # Optional fields with type checks
    priority = body.get("priority", 100)
    if not isinstance(priority, int) or isinstance(priority, bool):
        return _bad_request("priority must be an integer")

    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return _bad_request("note must be a string")

    auto_retry = body.get("auto_retry", 0)
    if not isinstance(auto_retry, int) or isinstance(auto_retry, bool):
        return _bad_request("auto_retry must be an integer")

    client_id = body.get("client_id")
    if client_id is not None and not isinstance(client_id, str):
        return _bad_request("client_id must be a string")

    try:
        job_id = db.add_job(
            payload=payload,
            scheduled_at=float(scheduled_at),
            priority=int(priority),
            note=note,
            client_id=client_id,
            auto_retry=int(auto_retry),
        )
    except Exception:
        _log.exception("add_job failed")
        return _server_error("database error")

    return _json_response(
        {
            "id": job_id,
            "scheduled_at": float(scheduled_at),
            "status": "scheduled",
        },
        status=201,
    )


async def cancel_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/cancel/{job_id}

    Soft-delete: sets status='cancelled'. Returns 200 + {id, status} or 404.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        ok = db.cancel_job(job_id)
    except Exception:
        _log.exception("cancel_job failed")
        return _server_error("database error")

    if not ok:
        return _not_found("job not found")

    return _json_response({"id": job_id, "status": "cancelled"})


async def update_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/update/{job_id}

    Body may contain ONLY: scheduled_at / priority / note / auto_retry.
    Any other field (including `status` and `payload`) returns 400.
    Returns 200 + {id, updated_fields} or 404.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _bad_request("body must be valid JSON")
    except Exception:
        return _bad_request("body must be valid JSON")

    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")

    # Whitelist enforcement: any unknown key => 400.
    unknown = set(body.keys()) - _UPDATE_ALLOWED_FIELDS
    if unknown:
        return _bad_request(
            f"fields not allowed: {sorted(unknown)} "
            f"(allowed: {sorted(_UPDATE_ALLOWED_FIELDS)})"
        )

    if not body:
        return _bad_request("at least one field is required")

    # Per-field type validation, mirroring database.py constraints.
    fields: dict[str, Any] = {}
    if "scheduled_at" in body:
        v = body["scheduled_at"]
        if not isinstance(v, (int, float)) or isinstance(v, bool) or not float(v) > 0:
            return _bad_request("scheduled_at must be a positive float")
        fields["scheduled_at"] = float(v)

    if "priority" in body:
        v = body["priority"]
        if not isinstance(v, int) or isinstance(v, bool):
            return _bad_request("priority must be an integer")
        fields["priority"] = int(v)

    if "note" in body:
        v = body["note"]
        if not isinstance(v, str):
            return _bad_request("note must be a string")
        fields["note"] = v

    if "auto_retry" in body:
        v = body["auto_retry"]
        if not isinstance(v, int) or isinstance(v, bool):
            return _bad_request("auto_retry must be an integer")
        fields["auto_retry"] = int(v)

    # Verify the job exists so we can return 404 distinctly from a no-op
    # update (which would otherwise silently succeed).
    try:
        if db.get_job(job_id) is None:
            return _not_found("job not found")
        db.update_job(job_id, **fields)
    except ValueError as e:
        return _bad_request(str(e))
    except Exception:
        _log.exception("update_job failed")
        return _server_error("database error")

    return _json_response({
        "id": job_id,
        "updated_fields": sorted(fields.keys()),
    })


async def reorder_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/reorder/{job_id}

    Body: ``{"direction": -1 | 1}``.
    Swaps the job's ``queue_order`` with its pending neighbour. No-op
    (200 with ``"moved": false``) when the job is at the edge or not in a
    pending state. 404 if the job doesn't exist.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        body = await request.json()
    except Exception:
        return _bad_request("body must be valid JSON")
    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")

    direction = body.get("direction")
    if not isinstance(direction, int) or isinstance(direction, bool):
        return _bad_request("direction must be an integer")
    if direction not in (-1, 1):
        return _bad_request("direction must be -1 or 1")

    if db.get_job(job_id) is None:
        return _not_found("job not found")

    try:
        moved = db.reorder_job(job_id, direction)
    except ValueError as e:
        return _bad_request(str(e))
    except Exception:
        _log.exception("reorder_job failed")
        return _server_error("database error")

    return _json_response({"id": job_id, "direction": direction, "moved": moved})


async def status_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """GET /api/schedule/status

    Returns the global scheduler snapshot. In Stage 1 there is no scheduler
    thread, so `paused` is always True and `last_dispatch_at` / `last_error`
    come from scheduler_state (or null if never set).
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    try:
        last_dispatch_raw = db.get_state("last_dispatch_at")
        last_error = db.get_state("last_error")
        paused_raw = db.get_state("paused")
        # Default to "1" (paused) when never set; treat any non-"0" as paused.
        paused = paused_raw != "0"

        # Parse last_dispatch_at as float if present.
        last_dispatch_at: float | None
        if last_dispatch_raw is None or last_dispatch_raw == "":
            last_dispatch_at = None
        else:
            try:
                last_dispatch_at = float(last_dispatch_raw)
            except (TypeError, ValueError):
                last_dispatch_at = None

        # Counts: query each known status. scheduled_jobs holds active rows;
        # job_history holds terminal rows (done/failed). Done/failed are NOT
        # in scheduled_jobs anymore (they move to history once observed).
        counts: dict[str, int] = {key: 0 for key in _STATUS_COUNT_KEYS}
        for status in ("scheduled", "running", "interrupted", "cancelled"):
            rows = db.list_jobs(status_filter=[status], limit=_LIST_MAX_LIMIT)
            counts[status] = len(rows)
        try:
            history_rows = db.list_history(limit=_LIST_MAX_LIMIT)
        except Exception:
            history_rows = []
        for row in history_rows:
            s = row.get("status")
            if s in counts:
                counts[s] += 1
    except Exception:
        _log.exception("status query failed")
        return _server_error("database error")

    return _json_response({
        "paused": paused,
        "last_dispatch_at": last_dispatch_at,
        "last_error": last_error,
        "counts": counts,
    })


# ---------------------------------------------------------------------------
# Stage 2 handlers (spec section 3)
# ---------------------------------------------------------------------------

async def pause_all_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/pause-all

    Body: empty.
    Response: 200 {"paused": true}.
    Side effect: db.set_state("paused", "1").

    The actual scheduler thread (Stage 2 — scheduler.py) reads this
    state every tick; flip to "1" halts dispatching immediately. In-flight
    ComfyUI prompts are NOT cancelled (the native queue keeps running);
    pause only stops NEW dispatches from this scheduler.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    try:
        db.set_state("paused", "1")
    except Exception:
        _log.exception("pause_all: set_state failed")
        return _server_error("database error")

    return _json_response({"paused": True})


async def resume_all_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/resume-all

    Body: empty.
    Response: 200 {"paused": false, "resumed_count": <N>}.

    Behaviour (spec §3):
      1. db.set_state("paused", "0")  — un-pause scheduler
      2. db.reset_all_interrupted()    — bulk-flip status='interrupted' back to
         status='scheduled' and bump their scheduled_at to now so they become
         immediately due. Returns the count of rows that were reset.

    The resumed_count reflects ONLY the rows that were status='interrupted'
    at the moment of the call (i.e. orphans from previous ComfyUI runs).
    Already-'scheduled' rows are not touched and not counted.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    try:
        db.set_state("paused", "0")
        resumed_count = db.reset_all_interrupted()
    except AttributeError:
        # Subagent A (database.py) hasn't landed its Stage 2 methods yet.
        # We still flip the paused flag so the scheduler can resume once
        # the missing methods are merged; but report 0 because we couldn't
        # actually reset anything.
        _log.warning(
            "resume_all: db.reset_all_interrupted() missing — "
            "subagent A's database.py patch not yet applied."
        )
        try:
            db.set_state("paused", "0")
        except Exception:
            pass
        return _json_response({"paused": False, "resumed_count": 0})
    except Exception:
        _log.exception("resume_all: database error")
        return _server_error("database error")

    return _json_response({"paused": False, "resumed_count": int(resumed_count)})


async def run_now_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/run-now/{job_id}

    Body: empty.
    Response: 200 {"id": ..., "scheduled_at": <new>}.

    Behaviour (spec §3):
      1. Look up the job. 404 if missing.
      2. db.reset_for_reschedule(job_id) — moves status='interrupted' (or any
         non-terminal state) back to 'scheduled' and bumps retry_count.
      3. db.update_job(job_id, scheduled_at=time.time()) — make it due now.

    Returns the new scheduled_at so the agent can confirm dispatch timing.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    # Existence check first — distinguishes 404 from a silent no-op.
    try:
        row = db.get_job(job_id)
    except Exception:
        _log.exception("run_now: get_job failed for %s", job_id)
        return _server_error("database error")
    if row is None:
        return _not_found("job not found")

    try:
        db.reset_for_reschedule(job_id)
        new_scheduled_at = float(time.time())
        db.update_job(job_id, scheduled_at=new_scheduled_at)
    except AttributeError:
        _log.warning(
            "run_now: db.reset_for_reschedule() missing — "
            "subagent A's database.py patch not yet applied."
        )
        # Best-effort: still set scheduled_at to now so it becomes due.
        try:
            new_scheduled_at = float(time.time())
            db.update_job(job_id, scheduled_at=new_scheduled_at)
        except Exception:
            _log.exception("run_now: update_job fallback failed for %s", job_id)
            return _server_error("database error")
    except Exception:
        _log.exception("run_now: database error for %s", job_id)
        return _server_error("database error")

    return _json_response({
        "id": job_id,
        "scheduled_at": new_scheduled_at,
    })


async def orphan_status_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """GET /api/schedule/orphan-status

    Response: 200 {
        "interrupted_count": <int>,
        "interrupted_ids":   [<id>, ...],
        "auto_retry_count":  <count where auto_retry=1>
    }.

    Behaviour (spec §3):
      SELECT id, auto_retry FROM scheduled_jobs WHERE status='interrupted'.
    No payload is returned (matches /list pattern of never exposing payload
    over HTTP — Stage 2 spec kept that discipline).

    This is the primary signal an agent uses to decide whether to call
    /resume-all after a ComfyUI crash.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    try:
        rows = db.list_jobs(status_filter=["interrupted"], limit=_LIST_MAX_LIMIT)
    except Exception:
        _log.exception("orphan_status: list_jobs failed")
        return _server_error("database error")

    interrupted_ids = [r["id"] for r in rows if "id" in r]
    auto_retry_count = sum(
        1 for r in rows if int(r.get("auto_retry", 0) or 0) == 1
    )

    return _json_response({
        "interrupted_count": len(interrupted_ids),
        "interrupted_ids": interrupted_ids,
        "auto_retry_count": auto_retry_count,
    })


# ---------------------------------------------------------------------------
# Stage 3 handlers (spec section 4 / this v0.3.8 batch)
# ---------------------------------------------------------------------------

# Cap for /add-batch. Bigger batches would slow the request thread and
# risk OOM on the JSON parse; 50 keeps a single bulk insert snappy.
_ADD_BATCH_MAX_ITEMS = 50


def _validate_add_item(item: Any) -> tuple[dict, str | None]:
    """Validate one item from /add-batch.

    Returns ``(args, None)`` on success where ``args`` is the kwargs dict
    ready to hand to ``db.add_job``; or ``({}, reason)`` when the item
    should be skipped (per spec: a single bad item must not fail the
    whole batch).
    """
    if not isinstance(item, dict):
        return {}, "item must be an object"

    payload = item.get("payload")
    if payload is None:
        return {}, "payload is required"
    if not isinstance(payload, dict):
        return {}, "payload must be a dict"

    scheduled_at = item.get("scheduled_at")
    if scheduled_at is None:
        return {}, "scheduled_at is required"
    if isinstance(scheduled_at, bool) or not isinstance(scheduled_at, (int, float)):
        return {}, "scheduled_at must be a number"
    if float(scheduled_at) <= 0:
        return {}, "scheduled_at must be a positive float"

    args: dict[str, Any] = {
        "payload": payload,
        "scheduled_at": float(scheduled_at),
    }

    if "priority" in item:
        priority = item["priority"]
        if isinstance(priority, bool) or not isinstance(priority, int):
            return {}, "priority must be an integer"
        args["priority"] = int(priority)

    if "note" in item:
        note = item["note"]
        if note is not None and not isinstance(note, str):
            return {}, "note must be a string"
        args["note"] = note

    if "auto_retry" in item:
        ar = item["auto_retry"]
        if isinstance(ar, bool) or not isinstance(ar, int):
            return {}, "auto_retry must be an integer"
        args["auto_retry"] = int(ar)

    if "client_id" in item:
        cid = item["client_id"]
        if cid is not None and not isinstance(cid, str):
            return {}, "client_id must be a string"
        args["client_id"] = cid

    return args, None


async def add_batch_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/add-batch

    Body: ``{"items": [<single-add body>, ...]}`` (max 50 items per request).

    Each item is validated independently; per-item validation failures are
    silently skipped (they don't fail the whole batch). Database errors on
    a specific item are logged and that item is dropped from the response —
    the rest of the batch still lands.

    Response: 201 ``{"added": [{"id": ..., "scheduled_at": ...}, ...],
    "count": N}``. ``count`` reflects how many items actually got added;
    compare with the input length to detect partial success.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return _bad_request("body must be valid JSON")
    except Exception:
        return _bad_request("body must be valid JSON")

    if not isinstance(body, dict):
        return _bad_request("body must be a JSON object")

    items = body.get("items")
    if not isinstance(items, list):
        return _bad_request("items must be a list")
    if len(items) == 0:
        return _bad_request("items must not be empty")
    if len(items) > _ADD_BATCH_MAX_ITEMS:
        return _bad_request(
            f"too many items (max {_ADD_BATCH_MAX_ITEMS}, got {len(items)})"
        )

    added: list[dict[str, Any]] = []
    for idx, item in enumerate(items):
        args, err = _validate_add_item(item)
        if err is not None:
            _log.warning("add_batch: skipping item %d: %s", idx, err)
            continue
        try:
            job_id = db.add_job(**args)
        except Exception:
            _log.exception("add_batch: add_job failed for item %d", idx)
            continue
        added.append({"id": job_id, "scheduled_at": args["scheduled_at"]})

    return _json_response({"added": added, "count": len(added)}, status=201)


async def job_detail_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """GET /api/schedule/job/{id}

    Returns the full record for one job, including ``payload`` and
    ``outputs`` (the latter from job_history when the job is finished).

    Response: 200 with the job dict, or 404 if no such id.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        row = db.get_job_with_outputs(job_id)
    except Exception:
        _log.exception("get_job_with_outputs failed for %s", job_id)
        return _server_error("database error")

    if row is None:
        return _not_found("job not found")
    return _json_response(row)


async def clear_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """DELETE /api/schedule/clear?statuses=done,failed,cancelled

    Wipes every row whose status is in the comma-separated list (default
    ``done,failed,cancelled``). Hits BOTH scheduled_jobs (cancelled,
    interrupted, ...) and job_history (done, failed).

    Response: 200 ``{"cleared": <count>}``.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    raw = request.query.get("statuses")
    if raw is None or raw.strip() == "":
        statuses = ["done", "failed", "cancelled"]
    else:
        statuses = [s.strip() for s in raw.split(",") if s.strip()]
        if not statuses:
            statuses = ["done", "failed", "cancelled"]

    # Validation: refuse unknown statuses with 400 rather than silently
    # treating them as no-ops — saves the operator from typos.
    valid = set(_STATUS_IN_SCHEDULED) | set(_STATUS_IN_HISTORY)
    unknown = [s for s in statuses if s not in valid]
    if unknown:
        return _bad_request(f"unknown statuses: {unknown}")

    try:
        cleared = db.clear_by_status(statuses)
    except Exception:
        _log.exception("clear_by_status failed")
        return _server_error("database error")

    return _json_response({"cleared": int(cleared)})


async def repeat_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/repeat/{job_id}

    Copies a job's payload (from scheduled_jobs OR job_history) into a
    fresh scheduled job with a new UUID. New job gets scheduled_at=now,
    priority=100 (per spec — not the original).

    Response: 201 ``{"id": <new>, "source_id": <old>}`` or 404 if the
    source job has no payload to copy.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        new_id = db.repeat_job(job_id)
    except Exception:
        _log.exception("repeat_job failed for %s", job_id)
        return _server_error("database error")

    if new_id is None:
        return _not_found("source job not found or has no payload")
    return _json_response({"id": new_id, "source_id": job_id}, status=201)


async def export_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """GET /api/schedule/export/{job_id}

    Returns the job's payload as a downloadable JSON file. The job_id
    may live in scheduled_jobs OR job_history.

    Response: 200 ``application/json`` with body ``{"payload": ...}``
    and ``Content-Disposition: attachment; filename=...``. 404 if not found.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        row = db.get_job_with_outputs(job_id)
    except Exception:
        _log.exception("get_job_with_outputs failed for %s", job_id)
        return _server_error("database error")
    if row is None:
        return _not_found("job not found")

    payload = row.get("payload")
    # History rows from old DBs may have no payload column populated.
    body = json.dumps(
        {"payload": payload}, ensure_ascii=False, separators=(",", ":"),
    ).encode()

    try:
        from aiohttp import web
        resp = web.Response(body=body, content_type="application/json")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="job-{job_id[:8]}.json"'
        )
        return resp
    except Exception:
        # Test environment fallback — wrap in our stub so tests still see
        # the JSON bytes and the Content-Disposition header.
        resp = _StubResponse(body, 200)
        resp.headers = {
            "Content-Type": "application/json",
            "Content-Disposition": f'attachment; filename="job-{job_id[:8]}.json"',
        }
        return resp


# Make validation logic accessible to the test suite without re-importing.
_STATUS_IN_SCHEDULED = (
    "scheduled", "dispatched", "running", "interrupted", "cancelled",
)
_STATUS_IN_HISTORY = ("done", "failed")


# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------

def setup_routes(db, interceptor=None) -> None:
    """Register all /api/schedule/* endpoints on the running ComfyUI server.

    Spec section 5.1: PromptServer.instance must be touched INSIDE this
    function (and inside the handlers), never at module import time.

    The db instance is also stashed on the aiohttp app via app["sq_db"] so
    each handler can fetch it via request.app["sq_db"]. interceptor is
    accepted for forward-compatibility with Stage 2 (when prompt
    interception will be wired up); it's not used in Stage 1 handlers.
    """
    # Lazy imports — custom_nodes are imported BEFORE PromptServer is ready.
    # Reference the module-level `PromptServer` (not a fresh `from server import`)
    # so spec verification scripts can `patch.object(routes, 'PromptServer')`.
    try:
        from aiohttp import web  # noqa: F401
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning(
            "[ScheduledQueue] aiohttp unavailable; routes not registered: %s", exc
        )
        return
    global PromptServer
    try:
        from server import PromptServer as _ServerPromptServer
        PromptServer = _ServerPromptServer
    except Exception:
        pass
    server = PromptServer.instance
    if server is None:
        # PromptServer hasn't been built yet (custom_node loads before
        # ComfyUI bootstrap). Stay silent -- the ComfyUI loader will
        # retry by re-importing our package only if it calls back into
        # us. We expose try_install() for that purpose; tests/CI can also
        # call it.
        return
    # Spec §5.1: route via server.routes.app.router. In modern ComfyUI
    # `server.routes` is a RouteTableDef, so we accept both spellings —
    # `routes.app` if present (older custom_node convention), else fall
    # back to `server.app` (the real Application instance).
    routes_obj = server.routes
    app = getattr(routes_obj, "app", None) or server.app

    # Stash db on the app so handlers can reach it through request.app.
    app["sq_db"] = db
    if interceptor is not None:
        app["sq_interceptor"] = interceptor

    app.router.add_get("/api/schedule/list", list_handler)
    app.router.add_post("/api/schedule/add", add_handler)
    app.router.add_post("/api/schedule/cancel/{job_id}", cancel_handler)
    app.router.add_post("/api/schedule/update/{job_id}", update_handler)
    app.router.add_post("/api/schedule/reorder/{job_id}", reorder_handler)
    app.router.add_get("/api/schedule/status", status_handler)

    # Stage 2 additions (spec section 3) — appended, never reorder stage-1.
    app.router.add_post("/api/schedule/pause-all", pause_all_handler)
    app.router.add_post("/api/schedule/resume-all", resume_all_handler)
    app.router.add_post("/api/schedule/run-now/{job_id}", run_now_handler)
    app.router.add_get("/api/schedule/orphan-status", orphan_status_handler)

    # Stage 3 additions (v0.3.8 batch — backend feature flush).
    app.router.add_post("/api/schedule/add-batch", add_batch_handler)
    app.router.add_get("/api/schedule/job/{job_id}", job_detail_handler)
    app.router.add_delete("/api/schedule/clear", clear_handler)
    app.router.add_post("/api/schedule/repeat/{job_id}", repeat_handler)
    app.router.add_get("/api/schedule/export/{job_id}", export_handler)


__all__ = [
    "setup_routes",
    "list_handler",
    "add_handler",
    "add_batch_handler",
    "cancel_handler",
    "update_handler",
    "reorder_handler",
    "status_handler",
    "pause_all_handler",
    "resume_all_handler",
    "run_now_handler",
    "orphan_status_handler",
    "job_detail_handler",
    "clear_handler",
    "repeat_handler",
    "export_handler",
]
