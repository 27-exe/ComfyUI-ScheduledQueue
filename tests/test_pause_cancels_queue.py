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

    Responses can be 2-tuples ``(status, text)`` (legacy / convenience)
    or 3-tuples ``(status, text, kind)`` to exercise the new timeout
    branch in ``_comfyui_post_json``.
    """

    def __init__(
        self,
        default_response: Tuple[int, str, str] = (200, "", "success"),
    ):
        self.calls: List[Dict[str, Any]] = []
        self._queue: List[Tuple[int, str, str]] = []
        self.default_response = default_response

    def expect(self, response) -> "_RecordingFetcher":
        if len(response) == 2:
            response = (response[0], response[1], "success")
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
        fetcher = _RecordingFetcher(default_response=(0, "", "refused"))
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
        # After the pause-all split, /pause-all only touches the
        # dispatched rows. The running row is left alone (see
        # /pause-running-all).
        self.db.update_job(a, status="dispatched", prompt_id="p-a")
        self.db.update_job(b, status="dispatched", prompt_id="p-b")
        self.db.update_job(c, status="running", prompt_id="p-c")

        # One HTTP response for the /queue delete batch.
        self.fetcher.expect((200, ""))

        captured = self._patch_fetcher()
        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["paused"], True)
        self.assertEqual(body["cancelled_count"], 2)
        self.assertEqual(body["error_count"], 0)
        self.assertEqual(body["errors"], [])

        # The cancel helper was called with the right in_flight set
        # and the default ComfyUI URL.
        self.assertEqual(captured["url"], routes._DEFAULT_COMFYUI_URL)
        in_flight_ids = {r["id"] for r in captured["in_flight"]}
        # Only the dispatched rows are passed to /pause-all; the running
        # row is left for /pause-running-all.
        self.assertEqual(in_flight_ids, {a, b})

        # And the HTTP fetcher saw the right URLs / bodies.
        self.assertEqual(len(self.fetcher.calls), 1)
        first = self.fetcher.calls[0]
        self.assertEqual(first["url"], routes._DEFAULT_COMFYUI_URL + "/queue")
        # Order is DB-driven (sorted by id), so compare as sets.
        self.assertEqual(set(first["body"]["delete"]), {"p-a", "p-b"})

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
        # After the pause-all split, /pause-all only touches
        # 'dispatched' rows. The running row is left alone (it is
        # handled by /pause-running-all instead).
        self.db.update_job(a, status="dispatched", prompt_id="p-a",
                            dispatched_at=1.0)
        self.db.update_job(b, status="dispatched", prompt_id="p-b",
                            dispatched_at=2.0)

        # /queue delete accepts a list of prompt_ids; we expect a
        # single batched call.
        self.fetcher.expect((200, ""))
        self._patch_fetcher()

        _run(routes.pause_all_handler(_StubRequest(self.app)))

        # Both 'dispatched' rows are reclaimed and have their
        # prompt_id cleared.
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
        # Network down — every call returns 0. After the pause-all
        # split, /pause-all only touches dispatched rows, so we test
        # with just one dispatched row.
        a = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p1")
        self.fetcher.default_response = (0, "", "refused")
        self._patch_fetcher()

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["paused"], True)
        self.assertEqual(body["cancelled_count"], 0)
        self.assertEqual(body["error_count"], 1)
        # Paused flag still set even though ComfyUI is unreachable —
        # scheduler should not start dispatching again.
        self.assertEqual(self.db.get_state("paused"), "1")
        # No prompt_id was cleared because nothing ack'd.
        self.assertEqual(self.db.get_job(a)["prompt_id"], "p1")

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

    def test_pause_timeout_is_treated_as_success(self):
        """A read timeout (status=0, kind='timeout') on the /interrupt call
        means ComfyUI was busy finishing the current sampling step. We
        treat the cancel as successful so the row gets reclaimed; if
        ComfyUI really didn't process it, reconcile() will pick up
        the orphan on the next poll and mark it interrupted."""
        a = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-busy")
        self.fetcher.expect((0, "", "timeout"))
        self._patch_fetcher()

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["cancelled_count"], 1)
        self.assertEqual(body["error_count"], 0)
        self.assertEqual(self.db.get_job(a)["status"], "scheduled")

    def test_pause_running_all_timeout_is_treated_as_success(self):
        """Same timeout-as-success rule applies to /pause-running-all:
        a timeout on /interrupt is treated as success because ComfyUI
        was busy finishing the current step. prompt_id is cleared so
        reconcile doesn't keep tracking the orphan."""
        a = _add(self.db)
        self.db.update_job(a, status="running", prompt_id="p-busy")
        self.fetcher.expect((0, "", "timeout"))
        self._patch_fetcher()

        resp = _run(routes.pause_running_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["cancelled_count"], 1)
        self.assertEqual(body["error_count"], 0)
        row = self.db.get_job(a)
        self.assertIsNone(row.get("prompt_id"))
        self.assertEqual(row["status"], "running")

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


# ---------------------------------------------------------------------
# Regression coverage for the pause-all race + 30s blocking bugs.
#
# Bug 1: pause-all used a global 30s HTTP timeout for the fast
# ``/queue {delete: [...]}`` call, which could freeze the aiohttp
# worker for 30s and make the sidebar appear unresponsive.
#
# Bug 2: the reclaim step (``db.reclaim_dispatched()``) used a
# blanket ``WHERE status='dispatched'`` UPDATE, so a row that
# flipped from ``dispatched`` → ``running`` while pause-all was
# waiting for ComfyUI's /queue ack would be naively flipped back
# to ``scheduled``. On resume the scheduler would re-dispatch that
# row, creating a duplicate prompt in ComfyUI (the running copy
# would still finish normally, but the user would see two of
# everything).
# ---------------------------------------------------------------------


class _RacingFetcher:
    """A fetcher that flips a row's DB status from 'dispatched' to
    'running' AFTER the handler captured its snapshot but BEFORE the
    reclaim step runs. This emulates the production race where
    ComfyUI promotes a pending prompt to running while pause-all
    is waiting on /queue.

    The flip is triggered the first time the fetcher is invoked —
    i.e. the instant the handler calls ``_cancel_comfyui_queue``,
    which is exactly the window in which the production race
    happens. The same fetcher still records every call so the test
    can assert the right HTTP traffic happened.
    """

    def __init__(self, db, flip_id, target_status="running"):
        self._db = db
        self._flip_id = flip_id
        self._flipped = False
        self.target_status = target_status
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, timeout: float):
        try:
            decoded = body.decode("utf-8")
            json_body = json.loads(decoded) if decoded else None
        except Exception:
            json_body = None
        self.calls.append(
            {"url": url, "body": json_body, "raw_body": body, "timeout": timeout}
        )
        if not self._flipped:
            self._flipped = True
            # Atomic-ish: the handler's snapshot has already been
            # taken (it ran ``list_in_flight_with_prompt_id()``
            # before calling _cancel_comfyui_queue), so flipping
            # now means the reclaim step sees status='running' for
            # this row.
            try:
                # NOTE: started_at is intentionally omitted because
                # db.update_job whitelists which columns can change;
                # status alone is enough for the WHERE clause in
                # _reclaim_dispatched_snapshot to skip this row.
                self._db.update_job(
                    self._flip_id,
                    status=self.target_status,
                )
            except Exception:
                pass
        # Pretend ComfyUI acked the cancel. The race fix is on our
        # side, not ComfyUI's.
        return 200, "", "success"


class _SlowFetcher:
    """A fetcher that simulates a slow ComfyUI by sleeping for
    ``min(delay, timeout)`` seconds, then returns ``kind='timeout'``
    if ``delay > timeout`` (ComfyUI didn't ack in time) or
    success if ``delay <= timeout``.

    The test wants to verify two things:
      1. The /queue call gets a short per-call timeout (≤5s), so a
         6s slow ComfyUI triggers the timeout path.
      2. The handler doesn't block longer than that timeout.

    We honour the ``timeout`` argument explicitly so the assertion
    ``slow.calls[0]["timeout"] <= 5.0`` proves the per-call timeout
    is wired correctly. If the timeout value ever grows back to
    the global 30s default, the sleep below grows too — the test
    would then either pass-through (≤30s) and fail on the
    ``wall_elapsed`` check, OR block for 30s and waste CI time.
    Either failure mode catches the regression.
    """

    def __init__(self, delay: float):
        self._delay = float(delay)
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, url: str, body: bytes, timeout: float):
        try:
            decoded = body.decode("utf-8")
            json_body = json.loads(decoded) if decoded else None
        except Exception:
            json_body = None
        self.calls.append(
            {"url": url, "body": json_body, "raw_body": body, "timeout": timeout}
        )
        # Honour the timeout: sleep up to ``min(delay, timeout)``
        # seconds. If delay > timeout, return kind='timeout' so the
        # helper applies timeout-as-success. If delay <= timeout,
        # return success.
        import time as _time
        slept = min(self._delay, float(timeout))
        if slept > 0:
            _time.sleep(slept)
        if self._delay > float(timeout):
            return 0, "", "timeout"
        return 200, "", "success"


class TestPauseAllRaceCondition(unittest.TestCase):
    """Regression tests for the pause-all 'B flips to running' race."""

    def setUp(self):
        self.db, self.path = _fresh_db()
        self.app = _FakeApp(self.db)
        # Capture the real _cancel_comfyui_queue BEFORE any test
        # patches it, so the regression tests can drive it with a
        # custom fetcher without recursing into themselves.
        self._real_cancel = routes._cancel_comfyui_queue

    def tearDown(self):
        # Make sure the module is back to its original state even
        # if the test failed mid-way (addCleanup is the primary
        # restoration mechanism; this is the safety net).
        routes._cancel_comfyui_queue = self._real_cancel
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _install_fetcher(self, fetcher):
        """Replace ``routes._cancel_comfyui_queue`` with a thin
        shim that delegates to the real implementation but
        injects *fetcher* into the call. Cleanup is wired
        automatically.
        """
        real = self._real_cancel
        chosen_fetcher = fetcher

        def shim(in_flight, comfyui_url, *, fetcher=None, **kw):
            # ``chosen_fetcher`` is bound via closure so the test's
            # custom fetcher is used regardless of what the handler
            # passes in (it currently passes nothing).
            return real(in_flight, comfyui_url, fetcher=chosen_fetcher)

        routes._cancel_comfyui_queue = shim
        self.addCleanup(setattr, routes, "_cancel_comfyui_queue", real)

    def test_pause_does_not_requeue_race_lost_to_running(self):
        """Regression: while pause-all is waiting for the /queue
        ack, a row that was 'dispatched' at snapshot time gets
        promoted to 'running' by ComfyUI. The race fix: the
        reclaim step scopes its UPDATE to ``id IN (<snapshot>) AND
        status='dispatched'`` so the row that flipped is NOT
        requeued. The still-dispatched rows ARE requeued.

        Without the fix, B (which is now running) would be flipped
        back to 'scheduled', losing its prompt_id and creating a
        duplicate prompt on resume.
        """
        a = _add(self.db)
        b = _add(self.db)
        c = _add(self.db)
        # All three start as dispatched with a prompt_id.
        self.db.update_job(a, status="dispatched", prompt_id="p-a",
                            dispatched_at=1.0)
        self.db.update_job(b, status="dispatched", prompt_id="p-b",
                            dispatched_at=2.0)
        self.db.update_job(c, status="dispatched", prompt_id="p-c",
                            dispatched_at=3.0)

        # Race injector: the first time the fetcher is called
        # (i.e. inside _cancel_comfyui_queue, AFTER the handler's
        # snapshot, BEFORE the reclaim), flip B to 'running'.
        racing = _RacingFetcher(self.db, flip_id=b, target_status="running")
        self._install_fetcher(racing)

        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["paused"], True)
        # /queue delete was acked for all three (racing fetcher
        # returns 200), so cancelled_count = 3.
        self.assertEqual(body["cancelled_count"], 3)
        self.assertEqual(body["error_count"], 0)
        # But only A and C are reclaimed. B is left alone because
        # its status is now 'running' (the race was lost).
        self.assertEqual(body["reclaimed_count"], 2)

        # A and C are back to 'scheduled' with prompt_id cleared.
        self.assertEqual(self.db.get_job(a)["status"], "scheduled")
        self.assertIsNone(self.db.get_job(a)["prompt_id"])
        self.assertEqual(self.db.get_job(c)["status"], "scheduled")
        self.assertIsNone(self.db.get_job(c)["prompt_id"])

        # B is the smoking gun: it must remain 'running' with its
        # original prompt_id intact. We do NOT want a duplicate
        # prompt on resume.
        b_row = self.db.get_job(b)
        self.assertEqual(b_row["status"], "running")
        self.assertEqual(b_row["prompt_id"], "p-b")

        # Sanity: the /queue delete was indeed sent to ComfyUI.
        self.assertEqual(len(racing.calls), 1)
        self.assertEqual(racing.calls[0]["url"], routes._DEFAULT_COMFYUI_URL + "/queue")


class TestPauseAllLatency(unittest.TestCase):
    """Regression tests for the pause-all 30s blocking bug."""

    def setUp(self):
        self.db, self.path = _fresh_db()
        self.app = _FakeApp(self.db)
        self._real_cancel = routes._cancel_comfyui_queue

    def tearDown(self):
        routes._cancel_comfyui_queue = self._real_cancel
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _install_fetcher(self, fetcher):
        real = self._real_cancel
        chosen_fetcher = fetcher

        def shim(in_flight, comfyui_url, *, fetcher=None, **kw):
            return real(in_flight, comfyui_url, fetcher=chosen_fetcher)

        routes._cancel_comfyui_queue = shim
        self.addCleanup(setattr, routes, "_cancel_comfyui_queue", real)

    def test_pause_returns_quickly_even_with_slow_comfyui(self):
        """Regression: a slow ComfyUI (e.g. stalled on /queue
        delete) must NOT block the aiohttp worker for the global
        30s default. The fix: the per-call ``queue_delete_timeout``
        is 5s. After the timeout, the handler returns 200 with
        ``error_count >= 1`` and the user can retry.

        We verify two things:
          1. The handler returns well within 30s when /queue takes
             >5s to ack (the timeout-as-success rule treats the
             slow ack as success, so the row is reclaimed).
          2. The ``timeout`` value the fetcher receives is the
             short 5s one, not the global 30s default.
        """
        a = _add(self.db)
        self.db.update_job(a, status="dispatched", prompt_id="p-slow")

        # /queue takes 6s to ack. With a 5s per-call timeout the
        # underlying urllib raises after 5s and the fetcher
        # returns ``kind='timeout'`` — which the helper treats as
        # success.
        slow = _SlowFetcher(delay=6.0)
        self._install_fetcher(slow)

        wall_start = __import__("time").monotonic()
        resp = _run(routes.pause_all_handler(_StubRequest(self.app)))
        wall_elapsed = __import__("time").monotonic() - wall_start

        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        # The slow ack is treated as success (timeout-as-success).
        self.assertEqual(body["cancelled_count"], 1)
        self.assertEqual(body["error_count"], 0)
        # Row reclaimed (timeout-as-success means we proceed with
        # the reclaim step).
        self.assertEqual(body["reclaimed_count"], 1)
        self.assertEqual(self.db.get_job(a)["status"], "scheduled")

        # The smoking gun: we MUST NOT have waited 6s. The 5s
        # timeout kicked in first. Allow generous slack for slow
        # CI but assert the call is well under the 6s sleep the
        # fetcher would have caused.
        self.assertLess(
            wall_elapsed, 5.5,
            f"pause-all took {wall_elapsed:.2f}s; the per-call "
            f"timeout (5s) failed to fire — the handler is "
            f"blocking on the global 30s default again.",
        )

        # And the per-call timeout must be the short 5s one, not
        # the global 30s default. If the helper regressed, this
        # assertion catches it.
        self.assertEqual(len(slow.calls), 1)
        self.assertLessEqual(
            slow.calls[0]["timeout"], 5.0,
            f"/queue delete was given timeout={slow.calls[0]['timeout']}; "
            f"expected ≤5s for the fast in-memory endpoint.",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)