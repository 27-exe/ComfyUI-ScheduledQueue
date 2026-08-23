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
    POST   /api/schedule/requeue-dispatched/{id} - pull one pending prompt back
    POST   /api/schedule/cancel-running/{id} - interrupt and fail one running job
    POST   /api/schedule/update/{id}        - patch whitelisted fields only
    POST   /api/schedule/reorder/{id}       - move job up/down in queue
    GET    /api/schedule/job/{id}           - one job incl. outputs (Stage 3)
    GET    /api/schedule/export/{id}        - payload as JSON download (Stage 3)
    DELETE /api/schedule/clear              - wipe rows by status (Stage 3)
    POST   /api/schedule/repeat/{id}        - clone as new job (Stage 3)
    GET    /api/schedule/status             - global scheduler status snapshot
    POST   /api/schedule/pause-all          - Stage 2: pause scheduler
    POST   /api/schedule/pause-running-all  - pause scheduler + interrupt all running jobs
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

import asyncio
import json
import logging
import time
import urllib.error
import urllib.request
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
# `workflow_title` is added in v0.3.10 so the sidebar can correct / retag a
# job's "which workflow" label without round-tripping through payload edits.
_UPDATE_ALLOWED_FIELDS = frozenset({"scheduled_at", "priority", "note", "auto_retry", "workflow_title"})

# Per-endpoint limit cap for /list. Spec (Stage 3): default 50, max 200.
_LIST_MAX_LIMIT = 200
_LIST_DEFAULT_LIMIT = 50

# Status counts returned by /status. Keys must match spec section 5.2.
# `dispatched` was added alongside the dispatched/running split — jobs
# that have been POSTed to ComfyUI but are queued behind another job in
# ComfyUI's native queue land here until reconcile promotes them.
_STATUS_COUNT_KEYS = (
    "scheduled",
    "dispatched",
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
# ComfyUI queue cancellation helpers (used by pause_all_handler).
#
# ComfyUI exposes two distinct cancellation surfaces — both must be hit
# for a true "pause and pull every in-flight job back":
#
#   POST /queue  {"delete": [<prompt_id>, ...]}   -- removes PENDING
#                                                   entries from
#                                                   ComfyUI's
#                                                   queue_pending.
#   POST /interrupt  {"prompt_id": "..."}         -- interrupts the
#                                                   currently-RUNNING
#                                                   prompt; we send one
#                                                   request per running
#                                                   job because the
#                                                   interrupt endpoint
#                                                   only accepts one
#                                                   prompt_id at a time.
#
# All HTTP calls go through ``_comfyui_post_json`` so the unit tests can
# inject a fake fetcher via ``patch.object(routes, '_comfyui_post_json')``.
# ---------------------------------------------------------------------------

_DEFAULT_COMFYUI_URL = "http://127.0.0.1:8188"
# ComfyUI's /queue and /interrupt calls during a pause should be quick,
# BUT the /interrupt call waits for the currently-running step to
# finish first -- a H3 step can take 5-10s, so the simple 5s ceiling
# produced bogus "HTTP 0" reports on working cancels. We give each
# cancel attempt up to 30s, which comfortably covers one step + cleanup.
# The HTTP helper also distinguishes between "ECONNREFUSED / ENOENT"
# (ComfyUI genuinely unreachable -- counted as an error) and a plain
# read timeout (ComfyUI very likely *did* process the cancel; counted
# as a success because reconcile() will treat the prompt as gone the
# next time it polls /history).
_COMFYUI_HTTP_TIMEOUT = 30.0


def _comfyui_url(request):
    """Resolve the ComfyUI base URL from app state, falling back to the
    process-wide default. Centralised so the row handlers and the
    global pause handlers don't drift.
    """
    return request.app.get("sq_comfyui_url") or _DEFAULT_COMFYUI_URL


def _default_comfyui_fetcher(url, body, timeout):
    """Built-in fetcher: POST ``body`` (bytes) to ``url`` via urllib.

    Returns ``(status, text, kind)``:
      * status -- HTTP code on a real response, 0 on any network error
      * text   -- response body on 2xx (best-effort decoded), else ''
      * kind   -- 'success' / 'http_error' / 'refused' / 'unreachable' /
                  'timeout'. ``kind`` is used by ``_comfyui_post_json``
                  to distinguish "we never reached ComfyUI" (refused /
                  unreachable) from "ComfyUI took longer than our
                  timeout but probably got the message" (timeout).

    Never raises.
    """
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace"), "success"
    except urllib.error.HTTPError as exc:
        return getattr(exc, "code", 0), "", "http_error"
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", None)
        # ``reason`` is either an ``OSError`` (e.g. ECONNREFUSED,
        # ENOENT, EAI_AGAIN) or a ``TimeoutError``. urllib returns it
        # as the underlying exception's instance when the wrapper
        # catches it; we classify below.
        if isinstance(reason, TimeoutError) or (reason is not None and
                                                getattr(reason, "errno", None) is None and
                                                "timed out" in str(reason).lower()):
            return 0, "", "timeout"
        if isinstance(reason, OSError):
            return 0, "", "refused"
        return 0, "", "unreachable"
    except (TimeoutError, ConnectionError, OSError) as exc:
        if isinstance(exc, TimeoutError):
            return 0, "", "timeout"
        if isinstance(exc, OSError):
            return 0, "", "refused"
        return 0, "", "unreachable"


def _comfyui_post_json(url, body, *, fetcher=None, timeout=None):
    """POST *body* as JSON to *url*. Returns ``(ok, payload_or_err)``.

    ``ok`` is True on any 2xx response. 4xx / 5xx and "we never
    reached ComfyUI" (refused / unreachable) return ``ok=False``.
    Plain read timeouts return ``ok=True`` because ComfyUI most likely
    did process the request -- it was busy finishing the current
    sampling step. The caller still needs a defensive sweep via
    reconcile() to confirm the prompt disappeared.

    Tests inject ``fetcher`` to avoid hitting a real ComfyUI. The fetcher
    signature is ``fetcher(url, body, timeout) -> (status, text, kind)``;
    the default uses ``urllib.request``.

    ``timeout`` overrides the module-level ``_COMFYUI_HTTP_TIMEOUT``
    for this single call. Defaults to ``None`` (use the module global).
    Callers that hit a fast in-memory endpoint (e.g. ``/queue {delete:
    [...]}`` which returns in <100ms) MUST pass a short timeout
    (≤5s) so a stuck / unresponsive ComfyUI cannot block the aiohttp
    worker for 30s.
    """
    payload = json.dumps(body).encode()
    do_fetch = fetcher if fetcher is not None else _default_comfyui_fetcher
    to = _COMFYUI_HTTP_TIMEOUT if timeout is None else float(timeout)
    try:
        status, text, kind = do_fetch(url, payload, to)
    except Exception as exc:  # noqa: BLE001 — defensive, never raise from HTTP helper
        return False, f"fetcher raised: {exc!r}"
    if 200 <= status < 300:
        return True, text
    if kind == "timeout":
        # Treat as success; the row is reclaimed by reconcile() if it
        # wasn't actually accepted by ComfyUI.
        return True, text or "timeout (assumed success)"
    return False, f"HTTP {status}"


# Cap for the /queue {delete: [...]} HTTP call. /queue is a fast
# in-memory mutation (ComfyUI normally acks in <100ms) so we don't
# need the global 30s ceiling that the /interrupt call requires.
# A stuck / unresponsive ComfyUI must not block the aiohttp worker
# for the full 30s — that previously froze the sidebar for half a
# minute when the user hit Pause.
_QUEUE_DELETE_TIMEOUT = 5.0


def _clear_prompt_id_dispatched_snapshot(db, snapshot_ids):
    """NULL out ``prompt_id``/``dispatched_at`` for snapshot rows
    that are STILL ``dispatched`` at the moment of the call.

    Mirrors ``_reclaim_dispatched_snapshot``'s race-recovery
    semantics: a row that transitioned ``dispatched`` → ``running``
    between the handler's snapshot and this call is left alone
    (ComfyUI is now responsible for it; we don't want to lose the
    prompt_id that's actively being polled by ``/history``).

    Lives in routes.py (rather than database.py) because it is a
    race-recovery helper specific to the pause-all flow.
    """
    if not snapshot_ids:
        return 0
    placeholders = ",".join("?" for _ in snapshot_ids)
    with db._IO_LOCK:
        with db._conn:
            cur = db._conn.execute(
                f"UPDATE scheduled_jobs "
                f"SET prompt_id=NULL, dispatched_at=NULL "
                f"WHERE id IN ({placeholders}) AND status='dispatched'",
                tuple(snapshot_ids),
            )
    return cur.rowcount


def _reclaim_dispatched_snapshot(db, snapshot_ids):
    """Reclaim only the rows that were 'dispatched' at the snapshot.

    ``db`` is the ``ScheduledQueueDB`` instance. ``snapshot_ids`` is
    the list of job_ids observed as ``dispatched`` by the previous
    ``db.list_in_flight_with_prompt_id()`` call.

    The single UPDATE is scoped to ``id IN (snapshot_ids) AND
    status='dispatched'`` so a row that transitioned
    ``dispatched`` → ``running`` between the snapshot and this call
    is left untouched (ComfyUI is already running it; flipping it
    back to ``scheduled`` would create a duplicate prompt on
    resume). The whole operation runs inside ``with db._conn:`` so
    the read-and-update pair is atomic from the scheduler's
    perspective.

    Returns the number of rows actually flipped to ``scheduled``.

    Lives in routes.py (rather than database.py) because it is a
    race-recovery helper specific to the pause-all flow — the
    generic ``db.reclaim_dispatched()`` keeps its original
    "every dispatched row" semantics for any future caller that
    doesn't have a snapshot whitelist handy.
    """
    if not snapshot_ids:
        return 0
    placeholders = ",".join("?" for _ in snapshot_ids)
    with db._IO_LOCK:
        with db._conn:
            cur = db._conn.execute(
                f"UPDATE scheduled_jobs "
                f"SET status='scheduled', prompt_id=NULL, dispatched_at=NULL, "
                f"    started_at=NULL, scheduled_at=? "
                f"WHERE id IN ({placeholders}) AND status='dispatched'",
                (time.time(), *snapshot_ids),
            )
    return cur.rowcount


def _cancel_comfyui_queue(
    in_flight, comfyui_url, *, fetcher=None, queue_delete_timeout=_QUEUE_DELETE_TIMEOUT,
):
    """Cancel every (job_id, prompt_id) in *in_flight* against ComfyUI.

    *in_flight* is a list of dicts from
    ``ScheduledQueueDB.list_in_flight_with_prompt_id()`` — each row
    carries at least ``id``, ``prompt_id`` and ``status`` (``'dispatched'``
    or ``'running'``).

    Behaviour:
      * ``dispatched`` rows are bundled into a single
        ``POST /queue {delete: [...]}`` call (ComfyUI accepts a list).
        This endpoint is a fast in-memory mutation that normally
        returns in <100ms, so ``queue_delete_timeout`` defaults to 5s.
        A stuck ComfyUI MUST NOT block the aiohttp worker for the full
        30s default — pause-all must return promptly so the sidebar
        doesn't appear frozen.
      * ``running`` rows are interrupted one at a time via
        ``POST /interrupt {prompt_id: pid}`` (ComfyUI's interrupt
        endpoint only takes one id at a time). ``/interrupt`` waits
        for the currently-running sampling step to finish before
        returning, so it stays on the module-global 30s default; this
        is the case the long timeout was originally designed for.

    Returns ``(cancelled_count, error_count, error_messages)``:
      * ``cancelled_count`` — rows whose prompt_id ComfyUI acknowledged
        (deleted from queue_pending OR successfully interrupted from
        queue_running). Per-prompt idempotency: a missing / already-done
        prompt is counted as cancelled because reconcile() will not find
        it in either queue slot either.
      * ``error_count`` — HTTP 5xx / network errors that prevented us
        from even reaching ComfyUI. The caller decides whether to
        still reclaim these rows (we currently DO NOT reclaim on error,
        so a retry of ``/pause-all`` will try again with a fresh HTTP
        call).
      * ``error_messages`` — short strings for logging / surfacing in
        the HTTP response (so operators can tell "ComfyUI was down" from
        "the cancel landed but ComfyUI returned 500").

    The function never raises. ``comfyui_url`` should be a fully-qualified
    base URL with no trailing slash.
    """
    if not in_flight:
        return 0, 0, []

    pending_ids = [r["prompt_id"] for r in in_flight if r["status"] == "dispatched" and r.get("prompt_id")]
    running_ids = [r["prompt_id"] for r in in_flight if r["status"] == "running" and r.get("prompt_id")]
    # Map pid -> job_id so the caller can clear the prompt_id off our
    # rows after a successful cancel (so reconcile doesn't fetch a
    # /history record for a prompt ComfyUI no longer has).
    pid_to_job = {r["prompt_id"]: r["id"] for r in in_flight if r.get("prompt_id")}

    cancelled = 0
    errors: list[str] = []
    base = comfyui_url.rstrip("/")

    if pending_ids:
        # /queue {delete: [...]} is a fast in-memory mutation — the
        # caller (e.g. /pause-all) MUST NOT block the aiohttp worker
        # for the global 30s default. Override per-call.
        ok, info = _comfyui_post_json(
            base + "/queue",
            {"delete": pending_ids},
            fetcher=fetcher,
            timeout=queue_delete_timeout,
        )
        if ok:
            # ComfyUI returns 200 with an empty body and no per-id
            # acknowledgement; we treat the whole batch as cancelled on
            # any 2xx because there is no public per-id status code and
            # missing prompt_ids are silent no-ops on ComfyUI's side.
            cancelled += len(pending_ids)
        else:
            errors.append(f"queue delete: {info}")

    for pid in running_ids:
        # /interrupt waits for the current sampling step — keep the
        # module global 30s ceiling here.
        ok, info = _comfyui_post_json(
            base + "/interrupt", {"prompt_id": pid}, fetcher=fetcher,
        )
        if ok:
            cancelled += 1
        else:
            errors.append(f"interrupt {pid[:8]}: {info}")

    return cancelled, len(errors), errors


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

    Lightweight fields like ``workflow_title`` (the ComfyUI workflow filename
    captured when the job was queued, v0.3.10+) ARE returned here so the
    sidebar can label each row without a per-row detail fetch.
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
    Optional: priority (int), note (str), auto_retry (int), client_id (str),
              workflow_title (str).
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

    # workflow_title: optional. The ComfyUI frontend sends the active
    # workflow's filename so the sidebar can label each row. Empty / missing
    # values are accepted (the DB layer normalises them to NULL).
    workflow_title = body.get("workflow_title")
    if workflow_title is not None and not isinstance(workflow_title, str):
        return _bad_request("workflow_title must be a string")

    try:
        job_id = db.add_job(
            payload=payload,
            scheduled_at=float(scheduled_at),
            priority=int(priority),
            note=note,
            client_id=client_id,
            auto_retry=int(auto_retry),
            workflow_title=workflow_title,
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


async def requeue_dispatched_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/requeue-dispatched/{id}.

    Remove one prompt from ComfyUI's pending queue, then return the local row
    to ``scheduled``.  The state transition is deliberately performed only
    after the native queue delete succeeds, so a transient ComfyUI error
    cannot create a duplicate prompt.

    The response uses status 409 for a live row that is no longer
    ``dispatched`` (for example, it became running while the request was in
    flight).
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        row = db.get_job(job_id)
    except Exception:
        _log.exception("requeue_dispatched: get_job failed")
        return _server_error("database error")
    if row is None:
        return _not_found("job not found")
    if row.get("status") != "dispatched":
        return _json_response(
            {"error": "job is no longer dispatched; it cannot be requeued"},
            status=409,
        )

    prompt_id = row.get("prompt_id")
    if prompt_id:
        try:
            ok, info = _comfyui_post_json(
                _comfyui_url(request) + "/queue",
                {"delete": [prompt_id]},
            )
        except Exception:
            _log.exception("requeue_dispatched: ComfyUI queue delete failed")
            return _server_error("ComfyUI queue delete failed")
        if not ok:
            return _json_response(
                {"error": f"ComfyUI queue delete failed: {info}"}, status=502
            )

    try:
        if not db.requeue_dispatched_job(job_id):
            return _json_response(
                {"error": "job is no longer dispatched; it cannot be requeued"},
                status=409,
            )
    except Exception:
        _log.exception("requeue_dispatched: database transition failed")
        return _server_error("database error")

    return _json_response({"id": job_id, "status": "scheduled", "requeued": True})


# Short alias used by older clients/sidebars.  Keep the explicit name above
# as the canonical API; both paths intentionally share the same handler.
async def requeue_handler(request) -> "web.Response":  # type: ignore[name-defined]
    return await requeue_dispatched_handler(request)


async def cancel_running_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/cancel-running/{id}.

    Interrupt exactly one live prompt, then move its local row to the failed
    history.  ComfyUI's /interrupt acknowledgement is the commit point:
    failures leave the row in ``running`` so the operator can retry safely.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    job_id = request.match_info.get("job_id", "").strip()
    if not job_id:
        return _bad_request("job_id is required")

    try:
        row = db.get_job(job_id)
    except Exception:
        _log.exception("cancel_running: get_job failed")
        return _server_error("database error")
    if row is None:
        return _not_found("job not found")
    if row.get("status") != "running":
        return _json_response(
            {"error": "job is not running; it cannot be interrupted"},
            status=409,
        )

    prompt_id = row.get("prompt_id")
    if not prompt_id:
        return _json_response(
            {"error": "running job has no prompt_id; it cannot be interrupted"},
            status=409,
        )

    try:
        ok, info = _comfyui_post_json(
            _comfyui_url(request) + "/interrupt", {"prompt_id": prompt_id}
        )
    except Exception:
        _log.exception("cancel_running: ComfyUI interrupt failed")
        return _server_error("ComfyUI interrupt failed")
    if not ok:
        return _json_response(
            {"error": f"ComfyUI interrupt failed: {info}"}, status=502
        )

    try:
        if not db.mark_failed(job_id, "cancelled by user"):
            return _server_error("database error")
    except Exception:
        _log.exception("cancel_running: failed to archive job")
        return _server_error("database error")

    return _json_response({"id": job_id, "status": "failed"})


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

    if "workflow_title" in body:
        v = body["workflow_title"]
        # Allow None to clear the title; otherwise require a string. Empty
        # string is accepted and normalised to NULL by the DB layer so legacy
        # rows that had the field passed as "" still treat it as "no title".
        if v is not None and not isinstance(v, str):
            return _bad_request("workflow_title must be a string")
        fields["workflow_title"] = v

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
        for status in ("scheduled", "dispatched", "running", "interrupted", "cancelled"):
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

class _PauseRequestContext:
    def __init__(self, db, comfyui_url):
        self.app = {"sq_db": db, "sq_comfyui_url": comfyui_url}

    def get(self, key, default=None):
        return self.app.get(key, default)


def _pause_all_blocking(db, comfyui_url) -> "web.Response":  # type: ignore[name-defined]
    """Run pause-all's synchronous DB and ComfyUI work off the event loop."""
    request = _PauseRequestContext(db, comfyui_url)
    """POST /api/schedule/pause-all

    Body: empty.
    Response: 200 ``{"paused": true, "reclaimed_count": N,
    "cancelled_count": M, "error_count": K, "errors": [...]}``.

    Side effects (in order):

      1. ``db.set_state("paused", "1")`` — the scheduler thread stops
         dispatching new jobs at the next tick.
      2. ``db.list_in_flight_with_prompt_id()`` — enumerate every row
         still sitting in ComfyUI's native queue. **Only ``dispatched``
         rows are reclaimed**: the user separates pause into two
         buttons, so ``/pause-all`` returns queued prompts back to the
         local queue and never interrupts a running prompt.
      3. Hit ComfyUI to actually pull the dispatched prompts out:

         * ``POST /queue {delete: [...]}`` with every dispatched
           prompt_id bundled into a single batch (ComfyUI accepts a
           list). This endpoint is a fast in-memory mutation that
           returns in <100ms, so it gets a 5s timeout (via
           ``_cancel_comfyui_queue``'s ``queue_delete_timeout``).
           A stuck / unresponsive ComfyUI MUST NOT block the aiohttp
           worker for the full 30s default — that previously froze
           the sidebar for half a minute.

      4. Reclaim ONLY the rows that were ``dispatched`` at the moment
         of the snapshot AND are still ``dispatched`` at the moment
         of the reclaim. This is a deliberate race-condition fix:
         between step 2 and step 4 a row may have transitioned
         ``dispatched`` → ``running`` (ComfyUI picked it up off the
         pending queue while we were waiting on its HTTP ack).
         ``reclaim_dispatched()`` would naively flip such a row back
         to ``scheduled`` even though ComfyUI is already running it,
         creating a duplicate prompt on resume. We avoid that by
         scoping the reclaim SQL to ``id IN (<snapshot ids>) AND
         status='dispatched'``, wrapped in a single transaction.

         **Skipped when any cancel call failed** — if ComfyUI didn't
         ack the cancel, leaving the row at 'dispatched' preserves
         the prompt_id so a subsequent ``/pause-all`` retry can
         target it again. The handler also calls
         ``db.clear_prompt_id()`` for the successfully cancelled
         ids so reconcile() doesn't fetch a /history record for a
         prompt that no longer exists.

    Running rows are untouched. Cancel a running prompt via
    ``/cancel-running/{id}`` (single) or ``/pause-running-all``
    (global). Re-claim a single dispatched prompt via
    ``/requeue-dispatched/{id}``.

    Failure modes:

      * If ComfyUI is unreachable / returns 5xx for the queue delete,
        the in-ComfyUI pending row keeps existing; our DB row stays
        at 'dispatched' and a subsequent ``/pause-all`` retry will
        try again with a fresh HTTP request. ``paused=True`` is still
        returned because the scheduler is correctly paused — the
        operator just has to retry the cancel piece.
      * If the reclaim SQL raises, we still return 200 with whatever
        the HTTP layer accomplished so a partial outage doesn't leave
        the scheduler running. The exception is logged.

    Latency budget: ≤5s for the ComfyUI cancel + ≤100ms for the
    reclaim SQL. The total request must finish well under the 30s
    default that previously froze the aiohttp worker.
    """
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")

    # The ComfyUI URL is overridable from app state so a deployment that
    # talks to a non-default host (cluster peer, second ComfyUI process)
    # can wire it in without editing this file. Default matches the
    # scheduler.py default so the two stay in lockstep.
    comfyui_url = request.app.get("sq_comfyui_url") or _DEFAULT_COMFYUI_URL

    try:
        db.set_state("paused", "1")
        all_in_flight = db.list_in_flight_with_prompt_id()
    except Exception:
        _log.exception("pause_all: db read failed")
        return _server_error("database error")

    # Only touch 'dispatched' rows; the user wants a separate button
    # to interrupt running prompts. See /pause-running-all.
    dispatched_in_flight = [
        r for r in (all_in_flight or []) if r.get("status") == "dispatched"
    ]
    # Snapshot the IDs we observed as dispatched. The reclaim step
    # MUST scope its UPDATE to this exact set (intersected with rows
    # still in status='dispatched') so a row that flipped to
    # 'running' between this snapshot and the reclaim is NOT
    # touched. See the race-condition note in the docstring.
    dispatched_snapshot_ids = [
        r["id"] for r in dispatched_in_flight if r.get("id")
    ]

    cancelled_count = 0
    error_count = 0
    errors: list[str] = []
    if dispatched_in_flight:
        cancelled_count, error_count, errors = _cancel_comfyui_queue(
            dispatched_in_flight, comfyui_url,
        )
        if error_count:
            _log.warning(
                "pause_all: %d/%d dispatched cancel calls failed: %s",
                error_count, len(dispatched_in_flight), errors,
            )
            try:
                db.set_state("last_error", f"pause_all cancel: {errors[0]}")
            except Exception:
                pass
        # For every row we *think* ComfyUI accepted, clear the prompt_id
        # so reconcile() doesn't fetch a /history record for a prompt
        # that no longer exists. Scope to rows STILL in
        # status='dispatched' so a row that flipped to 'running'
        # while we were waiting on /queue keeps its prompt_id
        # (ComfyUI is now running it; we don't want to lose the
        # /history pointer).
        if cancelled_count:
            try:
                _clear_prompt_id_dispatched_snapshot(
                    db, dispatched_snapshot_ids,
                )
            except Exception:
                _log.exception("pause_all: clear_prompt_id failed (continuing)")

    # Only reclaim 'dispatched' rows when the cancel calls actually
    # landed (or there were no in-flight rows to begin with). If the
    # cancel failed, leaving the row at 'dispatched' preserves the
    # prompt_id so a subsequent ``/pause-all`` retry can target it
    # again; reclaiming would NULL the prompt_id and break reconcile.
    #
    # Atomicity: scope the UPDATE to the snapshot ids AND the still-
    # dispatched status in a single SQL statement. If B was
    # 'dispatched' when we read list_in_flight_with_prompt_id() but
    # ComfyUI promoted B to 'running' while we were POSTing the
    # /queue {delete: [...]} batch, the WHERE clause skips B — we
    # don't NULL out B's prompt_id and don't requeue B. The user
    # sees B continue running normally; the /pause-running-all
    # button handles it separately.
    reclaimed_count = 0
    if error_count == 0 and dispatched_snapshot_ids:
        try:
            reclaimed_count = _reclaim_dispatched_snapshot(
                db, dispatched_snapshot_ids,
            )
        except Exception:
            _log.exception("pause_all: reclaim_dispatched failed (continuing)")
    elif error_count == 0 and not dispatched_snapshot_ids:
        # No in-flight rows — the helper isn't worth calling; mirror
        # the legacy semantics so the response shape stays stable.
        try:
            reclaimed_count = db.reclaim_dispatched()
        except Exception:
            _log.exception("pause_all: reclaim_dispatched failed (continuing)")

    return _json_response({
        "paused": True,
        "reclaimed_count": int(reclaimed_count),
        "cancelled_count": int(cancelled_count),
        "error_count": int(error_count),
        "errors": errors,
    })


async def pause_all_handler(request) -> "web.Response":  # type: ignore[name-defined]
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")
    comfyui_url = request.app.get("sq_comfyui_url") or _DEFAULT_COMFYUI_URL
    return await asyncio.to_thread(_pause_all_blocking, db, comfyui_url)


def _pause_running_all_blocking(db, comfyui_url) -> "web.Response":  # type: ignore[name-defined]
    """Run running-only pause work off the aiohttp event loop."""
    try:
        db.set_state("paused", "1")
        all_in_flight = db.list_in_flight_with_prompt_id()
    except Exception:
        _log.exception("pause_running_all: db read failed")
        return _server_error("database error")

    running_in_flight = [
        r for r in (all_in_flight or []) if r.get("status") == "running"
    ]

    cancelled_count = 0
    error_count = 0
    errors: list[str] = []
    if running_in_flight:
        cancelled_count, error_count, errors = _cancel_comfyui_queue(
            running_in_flight, comfyui_url,
        )
        if error_count:
            _log.warning(
                "pause_running_all: %d/%d running cancel calls failed: %s",
                error_count, len(running_in_flight), errors,
            )
            try:
                db.set_state("last_error", f"pause_running_all cancel: {errors[0]}")
            except Exception:
                pass
        if cancelled_count:
            try:
                cancelled_pids = {r["prompt_id"] for r in running_in_flight}
                job_ids_to_clear = [
                    r["id"] for r in running_in_flight
                    if r.get("prompt_id") and r["prompt_id"] in cancelled_pids
                ][:cancelled_count]
                db.clear_prompt_id(job_ids_to_clear)
            except Exception:
                _log.exception(
                    "pause_running_all: clear_prompt_id failed (continuing)"
                )

    return _json_response({
        "paused": True,
        "reclaimed_count": 0,
        "cancelled_count": int(cancelled_count),
        "interrupted_count": int(cancelled_count),
        "error_count": int(error_count),
        "errors": errors,
    })


async def pause_running_all_handler(request) -> "web.Response":
    db = request.app.get("sq_db")
    if db is None:
        return _server_error("db not initialized")
    comfyui_url = request.app.get("sq_comfyui_url") or _DEFAULT_COMFYUI_URL
    return await asyncio.to_thread(_pause_running_all_blocking, db, comfyui_url)


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

    if "workflow_title" in item:
        wt = item["workflow_title"]
        if wt is not None and not isinstance(wt, str):
            return {}, "workflow_title must be a string"
        args["workflow_title"] = wt

    return args, None


async def add_batch_handler(request) -> "web.Response":  # type: ignore[name-defined]
    """POST /api/schedule/add-batch

    Body: ``{"items": [<single-add body>, ...]}`` (max 50 items per request).
    Each item accepts the same optional fields as /add (priority, note,
    auto_retry, client_id, workflow_title).

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
    app.router.add_post("/api/schedule/requeue-dispatched/{job_id}", requeue_dispatched_handler)
    app.router.add_post("/api/schedule/cancel-running/{job_id}", cancel_running_handler)
    app.router.add_post("/api/schedule/update/{job_id}", update_handler)
    app.router.add_post("/api/schedule/reorder/{job_id}", reorder_handler)
    app.router.add_get("/api/schedule/status", status_handler)

    # Stage 2 additions (spec section 3) — appended, never reorder stage-1.
    app.router.add_post("/api/schedule/pause-all", pause_all_handler)
    app.router.add_post("/api/schedule/pause-running-all", pause_running_all_handler)
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
    # Internal helpers — exported so tests can patch them.
    "_comfyui_post_json",
    "_cancel_comfyui_queue",
    "_DEFAULT_COMFYUI_URL",
]
