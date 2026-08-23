"""Tests for the "pause-all actually cancels the in-ComfyUI queue" feature.

Two layers under test:

* ``database.list_in_flight_with_prompt_id`` + ``database.clear_prompt_id``
  — the bookkeeping helpers added so the routes layer can enumerate the
  jobs still sitting in ComfyUI's native queue and then NULL their
  prompt_id once ComfyUI has acknowledged the cancel.

* ``routes._cancel_comfyui_queue`` + ``routes.pause_all_handler`` — the
  HTTP plumbing: builds the right request bodies for ComfyUI's
  ``/queue {delete:[...]}`` and ``/interrupt {prompt_id:...}`` endpoints,
  counts successes vs failures, and threads the result through the
  handler response.

Tests inject a fake fetcher so no real ComfyUI is contacted.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from typing import Any, Dict, List, Optional, Tuple

# Make ``src/`` importable when pytest isn't available (this project
# ships an unittest suite, not pytest).
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
_SRC = os.path.join(_ROOT, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from comfyui_scheduled_queue import database, routes  # noqa: E402


# ---------------------------------------------------------------------
# Tiny fakes — re-used across tests so the suite stays readable.
# ---------------------------------------------------------------------


class _RecordingFetcher:
    """A fetcher that records every call and returns a programmable
    response. Each ``expect`` call is consumed FIFO; if the test makes
    more calls than expected the default ``default_response`` kicks in.
    """

    def __init__(self, default_response: Tuple[int, str] = (200, "")):
        self.calls: List[Dict[str, Any]] = []
        self._queue: List[Tuple[int, str]] = []
        self.default_response = default_response

    def expect(self, response: Tuple[int, str]) -> "_RecordingFetcher":
        self._queue.append(response)
        return self

    def __call__(self, url: str, body: bytes, timeout: float):
        # Decode body lazily — body may be JSON or just bytes.
        try:
            decoded = body.decode("utf-8")
            json_body = json.loads(decoded) if decoded else None
        except Exception:
            json_body = None
        self.calls.append(
            {"url": url, "body": json_body, "raw_body": body, "timeout": timeout}
        )
        if self._queue:
            return self._queue.pop(0)
        return self.default_response


class _FakeApp:
    """Minimal aiohttp-app stand-in. Tests stash ``sq_db`` and
    optionally ``sq_comfyui_url`` here, then pass it as
    ``request.app``."""

    def __init__(self, db, comfyui_url: Optional[str] = None):
        self._db = db
        self._store: Dict[str, Any] = {"sq_db": db}
        if comfyui_url is not None:
            self._store["sq_comfyui_url"] = comfyui_url

    def get(self, key: str, default: Any = None) -> Any:
        return self._store.get(key, default)


class _StubRequest:
    """Drop-in for an aiohttp Request — handlers only touch .app and
    sometimes .json_body (the pause-all handler uses neither beyond
    ``app.get``)."""

    def __init__(self, app: _FakeApp, json_body: Any = None):
        self.app = app
        self._json_body = json_body

    async def json(self) -> Any:
        return self._json_body


def _run(coro):
    """Drive an async handler to completion from synchronous test code.
    Uses ``asyncio.run`` so the test is independent of any outer event
    loop (Python 3.14 made ``get_event_loop`` strict)."""
    return asyncio.run(coro)


def _fresh_db():
    """Create a temp DB, route the package default through the temp
    file (so any helper that calls ``_default_db_path`` lands in our
    scratch space), and return ``(db, path)``."""
    tmp = tempfile.NamedTemporaryFile(
        prefix="sq-pause-queue-", suffix=".sqlite3", delete=False,
    )
    tmp.close()
    db = database.ScheduledQueueDB(db_path=tmp.name)
    return db, tmp.name


def _add(db, scheduled_at: float = 0.0, priority: int = 100) -> str:
    return db.add_job(payload={"x": 1}, scheduled_at=scheduled_at, priority=priority)


def _dispatch(db, job_id: str, prompt_id: str = "p-default") -> None:
    db.claim_next_due_job.__self__  # type: ignore[attr-defined]  # noqa
    # claim_next_due_job is the canonical "scheduled -> dispatched"
    # path but it requires a due row. Use update_job for a deterministic
    # transition that's independent of wall-clock time.
    db.update_job(job_id, status="dispatched", prompt_id=prompt_id,
                  dispatched_at=__import__("time").time())


def _mark_running(db, job_id: str, prompt_id: str) -> None:
    db.update_job(job_id, status="running", prompt_id=prompt_id,
                  started_at=__import__("time").time())


# ---------------------------------------------------------------------
# database.py — list_in_flight_with_prompt_id / clear_prompt_id
# ---------------------------------------------------------------------


class TestListInFlight(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_empty_db_returns_empty_list(self):
        self.assertEqual(self.db.list_in_flight_with_prompt_id(), [])

    def test_returns_only_dispatched_and_running_with_prompt_id(self):
        a = _add(self.db)
        b = _add(self.db)
        c = _add(self.db)
        d = _add(self.db)

        # a: dispatched with prompt_id  → must be returned
        self.db.update_job(a, status="dispatched", prompt_id="p-a")
        # b: running with prompt_id     → must be returned
        self.db.update_job(b, status="running", prompt_id="p-b")
        # c: dispatched but NULL prompt_id  → must be skipped
        self.db.update_job(c, status="dispatched", prompt_id=None)
        # d: scheduled (not in flight)  → must be skipped
        _ = d

        rows = self.db.list_in_flight_with_prompt_id()
        ids = {r["id"] for r in rows}
        pids = {r["prompt_id"] for r in rows}
        self.assertEqual(ids, {a, b})
        self.assertEqual(pids, {"p-a", "p-b"})

    def test_sort_is_dispatched_before_running(self):
        # Insertion order is reversed: running first, dispatched second.
        a = _add(self.db)
        b = _add(self.db)
        self.db.update_job(a, status="running", prompt_id="p-run")
        self.db.update_job(b, status="dispatched", prompt_id="p-pend")

        rows = self.db.list_in_flight_with_prompt_id()
        self.assertEqual([r["status"] for r in rows], ["dispatched", "running"])


class TestClearPromptId(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_clears_prompt_id_and_dispatched_at(self):
        a = _add(self.db)
        b = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-a",
                            dispatched_at=1.0)
        self.db.update_job(b, status="running", prompt_id="p-b",
                            dispatched_at=2.0)

        cleared = self.db.clear_prompt_id([a, b])
        self.assertEqual(cleared, 2)
        self.assertIsNone(self.db.get_job(a)["prompt_id"])
        self.assertIsNone(self.db.get_job(b)["prompt_id"])
        self.assertIsNone(self.db.get_job(a)["dispatched_at"])
        self.assertIsNone(self.db.get_job(b)["dispatched_at"])

    def test_empty_input_is_noop(self):
        self.assertEqual(self.db.clear_prompt_id([]), 0)

    def test_unknown_ids_are_tolerated(self):
        a = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-a")
        # `not-a-real-id` doesn't match any row; rowcount must reflect
        # the actual update count.
        cleared = self.db.clear_prompt_id(["not-a-real-id"])
        self.assertEqual(cleared, 0)
        # `a` is untouched.
        self.assertEqual(self.db.get_job(a)["prompt_id"], "p-a")


# ---------------------------------------------------------------------
# routes._cancel_comfyui_queue — pure helper, fully unit-tested.
# ---------------------------------------------------------------------


class TestCancelComfyuiQueue(unittest.TestCase):
    BASE = "http://127.0.0.1:8188"

    def test_empty_in_flight_is_noop(self):
        fetcher = _RecordingFetcher()
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            [], self.BASE, fetcher=fetcher,
        )
        self.assertEqual((cancelled, errors, msgs), (0, 0, []))
        self.assertEqual(fetcher.calls, [])

    def test_dispatched_rows_post_single_batch_delete(self):
        fetcher = _RecordingFetcher().expect((200, ""))
        in_flight = [
            {"id": "j1", "prompt_id": "p1", "status": "dispatched"},
            {"id": "j2", "prompt_id": "p2", "status": "dispatched"},
            {"id": "j3", "prompt_id": "p3", "status": "dispatched"},
        ]
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 3)
        self.assertEqual(errors, 0)
        self.assertEqual(msgs, [])
        self.assertEqual(len(fetcher.calls), 1)
        call = fetcher.calls[0]
        self.assertEqual(call["url"], self.BASE + "/queue")
        self.assertEqual(call["body"], {"delete": ["p1", "p2", "p3"]})

    def test_running_rows_post_one_interrupt_each(self):
        fetcher = _RecordingFetcher()
        for _ in range(3):
            fetcher.expect((200, ""))
        in_flight = [
            {"id": "r1", "prompt_id": "rpid1", "status": "running"},
            {"id": "r2", "prompt_id": "rpid2", "status": "running"},
        ]
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 2)
        self.assertEqual(errors, 0)
        self.assertEqual(msgs, [])
        self.assertEqual(len(fetcher.calls), 2)
        urls = [c["url"] for c in fetcher.calls]
        self.assertEqual(urls, [self.BASE + "/interrupt", self.BASE + "/interrupt"])
        bodies = [c["body"] for c in fetcher.calls]
        self.assertEqual(bodies, [{"prompt_id": "rpid1"}, {"prompt_id": "rpid2"}])

    def test_mixed_dispatched_and_running(self):
        # Three calls total: one batched /queue delete, two /interrupt
        # calls.
        fetcher = _RecordingFetcher()
        for _ in range(3):
            fetcher.expect((200, ""))
        in_flight = [
            {"id": "p1", "prompt_id": "pp1", "status": "dispatched"},
            {"id": "p2", "prompt_id": "pp2", "status": "dispatched"},
            {"id": "r1", "prompt_id": "rr1", "status": "running"},
            {"id": "r2", "prompt_id": "rr2", "status": "running"},
        ]
        cancelled, errors, _ = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 4)
        self.assertEqual(errors, 0)
        self.assertEqual(len(fetcher.calls), 3)
        # First call must be the batched delete.
        self.assertEqual(fetcher.calls[0]["url"], self.BASE + "/queue")
        self.assertEqual(fetcher.calls[0]["body"], {"delete": ["pp1", "pp2"]})
        # Remaining calls are interrupts.
        for c in fetcher.calls[1:]:
            self.assertEqual(c["url"], self.BASE + "/interrupt")

    def test_dispatched_5xx_counted_as_error(self):
        fetcher = _RecordingFetcher().expect((500, "boom"))
        in_flight = [
            {"id": "x", "prompt_id": "px", "status": "dispatched"},
        ]
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 0)
        self.assertEqual(errors, 1)
        self.assertEqual(len(msgs), 1)
        self.assertIn("HTTP 500", msgs[0])

    def test_network_failure_counted_as_error(self):
        # Fetcher returns status=0 — emulates URL open failure / DNS.
        fetcher = _RecordingFetcher(default_response=(0, ""))
        in_flight = [
            {"id": "x", "prompt_id": "px", "status": "dispatched"},
        ]
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 0)
        self.assertEqual(errors, 1)
        self.assertEqual(len(msgs), 1)
        self.assertIn("HTTP 0", msgs[0])

    def test_partial_failure_continues_with_remaining(self):
        # First call (the batched delete) fails; the two interrupts
        # succeed. Total cancelled = 2, errors = 1.
        fetcher = _RecordingFetcher()
        fetcher.expect((502, "bad gateway"))
        fetcher.expect((200, ""))
        fetcher.expect((200, ""))
        in_flight = [
            {"id": "p1", "prompt_id": "pp1", "status": "dispatched"},
            {"id": "r1", "prompt_id": "rr1", "status": "running"},
            {"id": "r2", "prompt_id": "rr2", "status": "running"},
        ]
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 2)
        self.assertEqual(errors, 1)
        self.assertEqual(len(msgs), 1)
        # All three calls happened despite the first one failing.
        self.assertEqual(len(fetcher.calls), 3)

    def test_fetcher_exception_is_treated_as_error(self):
        def boom(*_args, **_kwargs):
            raise OSError("nope")

        in_flight = [
            {"id": "x", "prompt_id": "px", "status": "dispatched"},
        ]
        cancelled, errors, msgs = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=boom,
        )
        self.assertEqual(cancelled, 0)
        self.assertEqual(errors, 1)
        self.assertEqual(len(msgs), 1)
        self.assertIn("fetcher raised", msgs[0])

    def test_rows_without_prompt_id_are_skipped(self):
        # A row with prompt_id=None should not be POSTed — the DB
        # helper already filters them, but defensive at the helper
        # level too.
        fetcher = _RecordingFetcher()
        in_flight = [
            {"id": "p1", "prompt_id": "pp1", "status": "dispatched"},
            {"id": "missing", "prompt_id": None, "status": "dispatched"},
        ]
        cancelled, errors, _ = routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        self.assertEqual(cancelled, 1)
        self.assertEqual(errors, 0)
        self.assertEqual(fetcher.calls[0]["body"], {"delete": ["pp1"]})

    def test_running_interrupt_uses_individual_calls(self):
        # Defensive: the batched-delete contract applies ONLY to
        # dispatched rows. Running rows always go to /interrupt one
        # at a time, never bundled.
        fetcher = _RecordingFetcher()
        for _ in range(5):
            fetcher.expect((200, ""))
        in_flight = [
            {"id": f"r{i}", "prompt_id": f"rrpid{i}", "status": "running"}
            for i in range(5)
        ]
        routes._cancel_comfyui_queue(
            in_flight, self.BASE, fetcher=fetcher,
        )
        urls = [c["url"] for c in fetcher.calls]
        self.assertEqual(
            urls, [self.BASE + "/interrupt"] * 5,
            f"running rows must hit /interrupt one at a time, got {urls}",
        )
        # No batched call to /queue when there are no dispatched rows.
        self.assertNotIn(self.BASE + "/queue", urls)


# ---------------------------------------------------------------------
# routes._comfyui_post_json — lower-level transport.
# ---------------------------------------------------------------------


class TestComfyuiPostJson(unittest.TestCase):
    def test_serialises_payload_as_json(self):
        fetcher = _RecordingFetcher().expect((200, '{"ok":1}'))
        ok, body = routes._comfyui_post_json(
            "http://x/y", {"delete": ["a"]}, fetcher=fetcher,
        )
        self.assertTrue(ok)
        self.assertEqual(body, '{"ok":1}')
        self.assertEqual(fetcher.calls[0]["body"], {"delete": ["a"]})

    def test_returns_false_on_5xx(self):
        fetcher = _RecordingFetcher().expect((503, ""))
        ok, body = routes._comfyui_post_json(
            "http://x/y", {"x": 1}, fetcher=fetcher,
        )
        self.assertFalse(ok)
        self.assertIn("HTTP 503", body)

    def test_returns_false_when_fetcher_raises(self):
        def kaboom(*_a, **_k):
            raise RuntimeError("socket died")

        ok, body = routes._comfyui_post_json(
            "http://x/y", {"x": 1}, fetcher=kaboom,
        )
        self.assertFalse(ok)
        self.assertIn("fetcher raised", body)


# ---------------------------------------------------------------------
# routes.pause_all_handler — the user-facing endpoint.
# ---------------------------------------------------------------------


class TestPauseAllCancelsQueue(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()
        # The handler reads the ComfyUI URL from app state; we don't
        # set it so the default kicks in. We still inject a fetcher
        # via a monkey-patch — see TestPauseAll._patch below.
        self.app = _FakeApp(self.db)
        self.fetcher = _RecordingFetcher()

    def tearDown(self):
        # Restore the default fetcher so the next test starts clean.
        routes._comfyui_post_json  # touch for noqa
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _patch_fetcher(self):
        """Patch ``_cancel_comfyui_queue`` (which the handler calls)
        so it uses our recording fetcher instead of the production
        urllib path. Returns a context manager."""
        original = routes._cancel_comfyui_queue
        captured = {}

        def fake_cancel(in_flight, comfyui_url, *, fetcher=None):
            cancelled, errors, msgs = original(
                in_flight, comfyui_url, fetcher=self.fetcher,
            )
            captured["url"] = comfyui_url
            captured["in_flight"] = in_flight
            return cancelled, errors, msgs

        routes._cancel_comfyui_queue = fake_cancel
        self.addCleanup(setattr, routes, "_cancel_comfyui_queue", original)
        return captured

    # -- actual scenarios ----------------------------------------

    def test_pause_with_no_inflight_is_clean(self):
        # No jobs → list_in_flight_with_prompt_id returns [], the
        # cancel layer is a no-op, and the response carries zeros.
        captured = self._patch_fetcher()
        self.fetcher.expect((200, ""))  # belt-and-braces; should not be hit

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["paused"], True)
        self.assertEqual(body["reclaimed_count"], 0)
        self.assertEqual(body["cancelled_count"], 0)
        self.assertEqual(body["error_count"], 0)
        self.assertEqual(body["errors"], [])
        # The cancel helper wasn't called at all (no in-flight rows).
        self.assertEqual(captured, {})
        self.assertEqual(self.fetcher.calls, [])
        # The pause flag is set.
        self.assertEqual(self.db.get_state("paused"), "1")

    def test_pause_calls_comfyui_with_correct_urls_and_bodies(self):
        a = _add(self.db)
        b = _add(self.db)
        c = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-a")
        self.db.update_job(b, status="dispatched", prompt_id="p-b")
        self.db.update_job(c, status="running", prompt_id="p-c")

        # Two HTTP responses: one for the /queue delete batch, one for
        # the single /interrupt call.
        self.fetcher.expect((200, ""))
        self.fetcher.expect((200, ""))

        captured = self._patch_fetcher()
        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["paused"], True)
        self.assertEqual(body["cancelled_count"], 3)
        self.assertEqual(body["error_count"], 0)
        self.assertEqual(body["errors"], [])

        # The cancel helper was called with the right in_flight set
        # and the default ComfyUI URL.
        self.assertEqual(captured["url"], routes._DEFAULT_COMFYUI_URL)
        in_flight_ids = {r["id"] for r in captured["in_flight"]}
        self.assertEqual(in_flight_ids, {a, b, c})

        # And the HTTP fetcher saw the right URLs / bodies.
        self.assertEqual(len(self.fetcher.calls), 2)
        first, second = self.fetcher.calls
        self.assertEqual(first["url"], routes._DEFAULT_COMFYUI_URL + "/queue")
        # Order is DB-driven (sorted by id), so compare as sets.
        self.assertEqual(set(first["body"]["delete"]), {"p-a", "p-b"})
        self.assertEqual(second["url"], routes._DEFAULT_COMFYUI_URL + "/interrupt")
        self.assertEqual(second["body"], {"prompt_id": "p-c"})

    def test_pause_uses_app_overridden_comfyui_url(self):
        """If app state carries a custom sq_comfyui_url, the handler
        honours it (cluster / multi-host deployments)."""
        custom_app = _FakeApp(self.db, comfyui_url="http://comfy-2:9000")
        self.db.update_job(_add(self.db), status="dispatched", prompt_id="p1")

        self.fetcher.expect((200, ""))
        captured = self._patch_fetcher()

        resp = _run(routes.pause_all_handler(_StubRequest(custom_app)))
        self.assertEqual(resp.status, 200)
        self.assertEqual(captured["url"], "http://comfy-2:9000")
        self.assertEqual(
            self.fetcher.calls[0]["url"], "http://comfy-2:9000/queue",
        )

    def test_pause_clears_prompt_id_after_successful_cancel(self):
        a = _add(self.db)
        b = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-a",
                            dispatched_at=1.0)
        self.db.update_job(b, status="running", prompt_id="p-b",
                            dispatched_at=2.0)

        self.fetcher.expect((200, ""))
        self.fetcher.expect((200, ""))
        self._patch_fetcher()

        _run(routes.pause_all_handler(_StubRequest(self.app)))

        # Reclaim flips 'dispatched' rows back to 'scheduled' but
        # leaves 'running' rows at 'running'. Either way, the
        # prompt_id (and dispatched_at) should be NULL after the
        # cancel calls ack'd.
        for jid in (a, b):
            row = self.db.get_job(jid)
            self.assertIsNone(row["prompt_id"], jid)
            self.assertIsNone(row["dispatched_at"], jid)

    def test_pause_records_last_error_on_http_failure(self):
        a = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-fail")
        # ComfyUI returns 500.
        self.fetcher.expect((500, "boom"))
        self._patch_fetcher()

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["cancelled_count"], 0)
        self.assertEqual(body["error_count"], 1)
        self.assertTrue(body["errors"])
        # The DB has the pause flag set AND last_error carrying the
        # reason.
        self.assertEqual(self.db.get_state("paused"), "1")
        self.assertIn("pause_all cancel", self.db.get_state("last_error") or "")
        # The dispatched row is NOT reclaimed — operator can retry.
        self.assertEqual(self.db.get_job(a)["status"], "dispatched")

    def test_pause_handles_comfyui_down_gracefully(self):
        # Network down — every call returns 0.
        a = _add(self.db)
        b = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p1")
        self.db.update_job(b, status="running", prompt_id="p2")
        self.fetcher.default_response = (0, "")
        self._patch_fetcher()

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["paused"], True)
        self.assertEqual(body["cancelled_count"], 0)
        # error_count covers every HTTP call (1 batched + 1 interrupt
        # = 2 attempts).
        self.assertEqual(body["error_count"], 2)
        # Paused flag still set even though ComfyUI is unreachable —
        # scheduler should not start dispatching again.
        self.assertEqual(self.db.get_state("paused"), "1")
        # No prompt_id was cleared because nothing ack'd.
        self.assertEqual(self.db.get_job(a)["prompt_id"], "p1")
        self.assertEqual(self.db.get_job(b)["prompt_id"], "p2")

    def test_pause_reclaims_dispatched_after_cancel(self):
        """End-to-end: after a successful pause-all, the previously
        'dispatched' rows should be back to 'scheduled' (so the
        scheduler picks them up again on resume), while 'running'
        rows keep their status (reconcile will finalise them once
        ComfyUI writes a history record)."""
        a = _add(self.db)
        b = _add(self.db)
        c = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-a")
        self.db.update_job(b, status="dispatched", prompt_id="p-b")
        self.db.update_job(c, status="running", prompt_id="p-c")

        self.fetcher.expect((200, ""))
        self.fetcher.expect((200, ""))
        self._patch_fetcher()

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        body = json.loads(resp.body)
        self.assertEqual(body["reclaimed_count"], 2)

        self.assertEqual(self.db.get_job(a)["status"], "scheduled")
        self.assertEqual(self.db.get_job(b)["status"], "scheduled")
        self.assertEqual(self.db.get_job(c)["status"], "running")

    def test_pause_without_db_returns_500(self):
        broken_app = _FakeApp(db=None)  # type: ignore[arg-type]
        resp = _run(routes.pause_all_handler(_StubRequest(broken_app)))
        self.assertEqual(resp.status, 500)

    def test_pause_response_shape_is_backward_compatible(self):
        """Existing clients parse ``{paused, reclaimed_count}``. Make
        sure those keys are still present, plus the new ones."""
        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        body = json.loads(resp.body)
        for k in ("paused", "reclaimed_count"):
            self.assertIn(k, body)
        for k in ("cancelled_count", "error_count", "errors"):
            self.assertIn(k, body)


if __name__ == "__main__":
    unittest.main(verbosity=2)