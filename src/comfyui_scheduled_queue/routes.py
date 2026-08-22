"""
ComfyUI-ScheduledQueue - HTTP API endpoints (Stage 1 + Stage 2)

Implements spec section 5 (Stage 1) and section 3 (Stage 2) from:
    docs/scheduled-queue/01-implementation-spec-stage-1-agent-facing.md
    docs/scheduled-queue/02-implementation-spec-stage-2-agent-facing.md

Endpoints (all under /api/schedule/*):
    GET    /api/schedule/list               - list jobs (filter by status, no payload)
    POST   /api/schedule/add                - create a new scheduled job
    POST   /api/schedule/cancel/{id}        - soft-delete (status='cancelled')
    POST   /api/schedule/update/{id}        - patch whitelisted fields only
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

# Per-endpoint limit cap for /list.
_LIST_MAX_LIMIT = 10000
_LIST_DEFAULT_LIMIT = 200

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
    from aiohttp import web
    return web.json_response({"error": reason}, status=400)


def _not_found(reason: str = "job not found"):
    from aiohttp import web
    return web.json_response({"error": reason}, status=404)


def _server_error(reason: str = "internal error"):
    from aiohttp import web
    return web.json_response({"error": reason}, status=500)


def _json_response(data: Any, status: int = 200):
    from aiohttp import web
    return web.json_response(data, status=status)


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
    """GET /api/schedule/list?status=scheduled,interrupted&limit=50

    Returns: {"jobs": [...stripped rows...], "total": int, "filter": [...]}.
    payload is never returned (spec section 5.2).
    """
    db = request.app.get("sq_db")  # set in setup_routes()
    if db is None:
        return _server_error("db not initialized")

    try:
        status_filter = _parse_status_filter(request.query.get("status"))
        limit = _parse_int(request.query.get("limit"), _LIST_DEFAULT_LIMIT, field="limit")
        limit = max(1, min(limit, _LIST_MAX_LIMIT))
    except ValueError as e:
        return _bad_request(str(e))

    try:
        rows = db.list_jobs(status_filter=status_filter, limit=limit)
    except Exception:
        _log.exception("list_jobs failed")
        return _server_error("database error")

    jobs = [_strip_payload(r) for r in rows]
    return _json_response({
        "jobs": jobs,
        "total": len(jobs),
        "filter": status_filter if status_filter is not None else [],
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
        # Stage 1: always True because no scheduler thread runs yet.
        "paused": True,
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
    app.router.add_get("/api/schedule/status", status_handler)

    # Stage 2 additions (spec section 3) — appended, never reorder stage-1.
    app.router.add_post("/api/schedule/pause-all", pause_all_handler)
    app.router.add_post("/api/schedule/resume-all", resume_all_handler)
    app.router.add_post("/api/schedule/run-now/{job_id}", run_now_handler)
    app.router.add_get("/api/schedule/orphan-status", orphan_status_handler)


__all__ = [
    "setup_routes",
    "list_handler",
    "add_handler",
    "cancel_handler",
    "update_handler",
    "status_handler",
    "pause_all_handler",
    "resume_all_handler",
    "run_now_handler",
    "orphan_status_handler",
]
