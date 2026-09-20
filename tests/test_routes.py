"""Routes handler tests -- exercise the reorder, pause/resume and update
endpoints. We bypass aiohttp by calling the handler coroutines directly
with a minimal stub Request that records what the handler reads.
"""
import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

# Ensure src/ is on the path even when the test runner is invoked from
# outside the project root.
_ROOT = Path(__file__).resolve().parent
_SRC = _ROOT.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# Force the DB path to a per-test temp file before the package is imported.
_tmpdir = tempfile.mkdtemp(prefix="sq-test-routes-")
os.environ.setdefault("COMFYUI_USER_DIR", _tmpdir)


class _StubRequest:
    """Minimal aiohttp Request lookalike sufficient for our handlers."""

    def __init__(self, *, app=None, json_body=None, match_info=None, query=None):
        self._app = app
        self._json_body = json_body
        self.match_info = match_info or {}
        # aiohttp exposes MultiDict, but our handlers only call .get(...) on it.
        # A plain dict is enough for the unit tests.
        self.query = query or {}

    async def json(self):
        if self._json_body is None:
            raise json.JSONDecodeError("empty", "", 0)
        return self._json_body

    @property
    def app(self):
        return self._app


def _run(coro):
    # asyncio.run() (vs the deprecated asyncio.get_event_loop() +
    # run_until_complete() that used to be here) creates and tears down a
    # private loop per call, which keeps the test runner -- and other test
    # modules in the suite -- from getting poisoned by leftover events.
    # On Python 3.12+ get_event_loop() raises "There is no current event
    # loop" in the main thread, which used to cause ~33 cross-file
    # failures here.
    return asyncio.run(coro)


# Fixture helper: every HTTP-layer test should use a *future* scheduled_at
# so the production `_check_future_scheduled_at` guard accepts it. We use
# `time.time()` per call rather than a module constant so a slow test that
# spans a minute boundary still passes the >= now+5 check.
def _future_scheduled_at(offset_seconds=3600):
    """Return a unix-seconds timestamp safely in the future for HTTP tests."""
    return time.time() + offset_seconds


class TestRoutes(unittest.TestCase):
    def setUp(self):
        # Fresh DB per test.
        self.db_path = os.path.join(_tmpdir, "test.sqlite3")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        from comfyui_scheduled_queue import database as db_mod
        from comfyui_scheduled_queue import routes
        self._db_mod = db_mod
        self._routes = routes
        self.db = db_mod.ScheduledQueueDB(db_path=self.db_path)
        self.app = {"sq_db": self.db}
        # These tests cover the HTTP contract, not payload validation; stub
        # the preflight seam so they stay hermetic (no live ComfyUI). The
        # validator itself is exercised in test_preflight.py and
        # PreflightIntegrationTests at the bottom of this file.
        from unittest.mock import patch as _patch
        _pf = _patch.object(routes, "_run_preflight", return_value=(True, []))
        _pf.start()
        self.addCleanup(_pf.stop)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _add(self, **kw):
        import time
        defaults = dict(payload={"x": 1}, scheduled_at=time.time() + 60, priority=100, note=None)
        defaults.update(kw)
        return self.db.add_job(**defaults)

    def test_reorder_swaps_neighbours(self):
        a = self._add(note="a")
        b = self._add(note="b")
        c = self._add(note="c")
        # move b up -> swap with a
        resp = _run(self._routes.reorder_handler(_StubRequest(
            app=self.app, json_body={"direction": -1}, match_info={"job_id": b})))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["moved"])
        order = [j["note"] for j in self.db.list_jobs() if j["status"] == "scheduled"]
        self.assertEqual(order, ["b", "a", "c"])

    def test_reorder_first_up_is_noop(self):
        a = self._add(note="a")
        b = self._add(note="b")
        resp = _run(self._routes.reorder_handler(_StubRequest(
            app=self.app, json_body={"direction": -1}, match_info={"job_id": a})))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertFalse(body["moved"])

    def test_reorder_bad_direction(self):
        a = self._add()
        resp = _run(self._routes.reorder_handler(_StubRequest(
            app=self.app, json_body={"direction": 0}, match_info={"job_id": a})))
        self.assertEqual(resp.status, 400)

    def test_reorder_unknown_job(self):
        resp = _run(self._routes.reorder_handler(_StubRequest(
            app=self.app, json_body={"direction": -1},
            match_info={"job_id": "nonexistent"})))
        self.assertEqual(resp.status, 404)

    def test_reorder_bad_body(self):
        a = self._add()
        resp = _run(self._routes.reorder_handler(_StubRequest(
            app=self.app, json_body=None, match_info={"job_id": a})))
        self.assertEqual(resp.status, 400)

    def _pause_no_cancel(self):
        """Call ``pause_all_handler`` while stubbing out the
        ComfyUI-queue cancel layer.

        The pause-all feature now POSTs to ComfyUI's ``/queue`` and
        ``/interrupt`` endpoints to actually pull dispatched/running
        prompts out of the executor (instead of just flipping the
        scheduler's paused flag). Existing pause-tests in this file
        pre-date that change and assert purely on the DB state
        — they don't (and shouldn't) care about the HTTP layer.

        Tests that DO care about the cancel layer live in
        ``test_pause_cancels_queue.py`` and use their own fetcher
        injection.
        """
        from unittest.mock import patch
        # ``cancelled == len(in_flight)`` and no errors → the handler
        # will reclaim every 'dispatched' row, matching the
        # pre-feature behaviour these tests assert on.
        patcher = patch.object(
            self._routes, "_cancel_comfyui_queue",
            return_value=(99, 0, []),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        return _run(self._routes.pause_all_handler(_StubRequest(app=self.app)))

    def test_pause_then_status_reflects_state(self):
        # status should report True before pause, and reflect the DB state
        # after a toggle (not the previously hard-coded True).
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["paused"], "default state should be paused")

        self._pause_no_cancel()
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertTrue(json.loads(resp.body)["paused"])

        _run(self._routes.resume_all_handler(_StubRequest(app=self.app)))
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        body = json.loads(resp.body)
        self.assertFalse(body["paused"], "status must reflect DB after resume")

    def test_pause_reclaims_dispatched_and_resume_redelivers(self):
        dispatched = self._add()
        self.db.mark_dispatched(dispatched, "p-dispatched")
        resp = self._pause_no_cancel()
        self.assertEqual(json.loads(resp.body)["reclaimed_count"], 1)
        row = self.db.get_job(dispatched)
        self.assertEqual(row["status"], "scheduled")
        self.assertIsNone(row["prompt_id"])
        self.assertIsNone(row["dispatched_at"])
        _run(self._routes.resume_all_handler(_StubRequest(app=self.app)))
        claimed = self.db.claim_next_due_job()
        self.assertEqual(claimed["id"], dispatched)

    def test_pause_does_not_reclaim_running(self):
        jid = self._add()
        self.db.mark_dispatched(jid, "p-running")
        self.db.mark_running(jid, "p-running")
        self._pause_no_cancel()
        self.assertEqual(self.db.get_job(jid)["status"], "running")

    def test_status_db_state_change_is_observable(self):
        """Regression test for the bug where status always returned paused=True."""
        # DB starts paused = 1
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertTrue(json.loads(resp.body)["paused"])

        # Flip DB to "0"
        self.db.set_state("paused", "0")
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertFalse(
            json.loads(resp.body)["paused"],
            "status must reflect DB state, not a hard-coded value",
        )

        # Flip back to "1"
        self.db.set_state("paused", "1")
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertTrue(json.loads(resp.body)["paused"])

    # ------------------------------------------------------------------
    # Stage 3: /add-batch, paginated /list, /job/{id}, /clear, /repeat,
    # /export, and priority-aware claim_next_due_job.
    # ------------------------------------------------------------------

    def test_add_batch_with_multiple_items(self):
        # Freeze the timestamps once so the request body and the assertion
        # see byte-identical values (`time.time()` advances between calls).
        ts0, ts1, ts2 = _future_scheduled_at(100), _future_scheduled_at(200), _future_scheduled_at(300)
        items = [
            {"payload": {"i": 0}, "scheduled_at": ts0, "priority": 5},
            {"payload": {"i": 1}, "scheduled_at": ts1, "priority": 7},
            {"payload": {"i": 2}, "scheduled_at": ts2, "priority": 9},
        ]
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": items},
        )))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["added"]), 3)
        self.assertEqual({row["scheduled_at"] for row in body["added"]},
                         {ts0, ts1, ts2})
        # rows are real — fetch by id
        for row in body["added"]:
            self.assertIsNotNone(self.db.get_job(row["id"]))

    def test_add_batch_rejects_over_50_items(self):
        items = [
            {"payload": {"i": i}, "scheduled_at": _future_scheduled_at(100 + i)}
            for i in range(51)
        ]
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": items},
        )))
        self.assertEqual(resp.status, 400)
        body = json.loads(resp.body)
        self.assertIn("too many", body["error"].lower())
        # and nothing landed in the DB
        self.assertEqual(len(self.db.list_jobs()), 0)

    def test_list_pagination_and_status_filter(self):
        # 5 scheduled + 2 cancelled = 7 jobs total.
        for i in range(5):
            self._add(note=f"s-{i}")
        c1 = self._add(note="c-1"); self.db.update_job(c1, status="cancelled")
        c2 = self._add(note="c-2"); self.db.update_job(c2, status="cancelled")

        # page 1: status=scheduled, limit=2, offset=0
        resp = _run(self._routes.list_handler(_StubRequest(
            app=self.app, query={"status": "scheduled", "limit": "2", "offset": "0"},
        )))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["total"], 5)
        self.assertEqual(body["limit"], 2)
        self.assertEqual(body["offset"], 0)
        self.assertEqual(len(body["jobs"]), 2)
        self.assertTrue(body["has_more"])

        # page 2: offset=4, expect 1 row + has_more=False
        resp = _run(self._routes.list_handler(_StubRequest(
            app=self.app, query={"status": "scheduled", "limit": "2", "offset": "4"},
        )))
        body = json.loads(resp.body)
        self.assertEqual(len(body["jobs"]), 1)
        self.assertFalse(body["has_more"])

        # offset past the end -> empty + has_more=False
        resp = _run(self._routes.list_handler(_StubRequest(
            app=self.app, query={"status": "scheduled", "offset": "999"},
        )))
        body = json.loads(resp.body)
        self.assertEqual(body["jobs"], [])
        self.assertFalse(body["has_more"])

    def test_list_with_multiple_statuses_filter(self):
        for i in range(3):
            self._add(note=f"s-{i}")
        d = self._add(note="d-1"); self.db.update_job(d, status="dispatched")
        c = self._add(note="c-1"); self.db.update_job(c, status="cancelled")

        # comma-separated filter
        resp = _run(self._routes.list_handler(_StubRequest(
            app=self.app, query={"status": "dispatched,cancelled"},
        )))
        body = json.loads(resp.body)
        self.assertEqual(body["total"], 2)
        self.assertEqual({j["note"] for j in body["jobs"]}, {"d-1", "c-1"})

    def test_get_job_with_outputs(self):
        jid = self._add(note="with-outputs")
        self.db.update_job(jid, status="running", prompt_id="pid-x")
        # simulate reconcile finishing the job
        self.db.mark_done(jid, prompt_id="pid-x", outputs={"images": ["a.png"]})
        # the row is now only in job_history
        self.assertIsNone(self.db.get_job(jid))

        resp = _run(self._routes.job_detail_handler(_StubRequest(
            app=self.app, match_info={"job_id": jid},
        )))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["id"], jid)
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["outputs"], {"images": ["a.png"]})
        self.assertEqual(body["payload"], {"x": 1})

        # 404 for unknown
        resp = _run(self._routes.job_detail_handler(_StubRequest(
            app=self.app, match_info={"job_id": "missing"},
        )))
        self.assertEqual(resp.status, 404)

    def test_clear_by_status_default(self):
        # 1 scheduled + 1 cancelled + 1 done
        self._add(note="keep-scheduled")
        cancelled = self._add(note="to-clear-cancelled")
        self.db.update_job(cancelled, status="cancelled")

        live = self._add(note="live")
        self.db.update_job(live, status="running", prompt_id="p-live")
        self.db.mark_done(live, prompt_id="p-live", outputs={"x": 1})

        # default deletes done/failed/cancelled
        resp = _run(self._routes.clear_handler(_StubRequest(app=self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["cleared"], 2)  # cancelled + done

        # live scheduled row untouched
        rows = self.db.list_jobs()
        self.assertEqual([r["note"] for r in rows], ["keep-scheduled"])
        # history cleaned out
        self.assertEqual(self.db.list_history(), [])

    def test_clear_by_explicit_status_list(self):
        for i in range(3):
            jid = self._add(note=f"x-{i}")
            self.db.update_job(jid, status="cancelled")
        self._add(note="keep")  # scheduled, must survive

        # only wipe cancelled
        resp = _run(self._routes.clear_handler(_StubRequest(
            app=self.app, query={"statuses": "cancelled"},
        )))
        body = json.loads(resp.body)
        self.assertEqual(body["cleared"], 3)
        # one row left
        self.assertEqual(len(self.db.list_jobs()), 1)

        # bad status -> 400
        resp = _run(self._routes.clear_handler(_StubRequest(
            app=self.app, query={"statuses": "bogus"},
        )))
        self.assertEqual(resp.status, 400)

    def test_repeat_job_creates_new_with_same_payload(self):
        src = self._add(note="src", payload={"x": 99, "nested": {"a": 1}})
        self.db.update_job(src, status="running", prompt_id="pid-r")
        self.db.mark_done(src, prompt_id="pid-r", outputs={"i": ["out.png"]})

        resp = _run(self._routes.repeat_handler(_StubRequest(
            app=self.app, match_info={"job_id": src},
        )))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["source_id"], src)
        new_id = body["id"]
        self.assertNotEqual(new_id, src)

        new_row = self.db.get_job(new_id)
        self.assertIsNotNone(new_row)
        assert new_row is not None  # type narrowing for pyright
        self.assertEqual(new_row["status"], "scheduled")
        # get_job returns payload as the raw JSON string (consistent with
        # other DB methods). Decode to compare structurally.
        self.assertEqual(json.loads(new_row["payload"]), {"x": 99, "nested": {"a": 1}})
        self.assertEqual(new_row["priority"], 100)  # explicit default
        self.assertEqual(new_row["note"], "repeat of " + src[:8])

        # The /export endpoint should hand back the same payload as JSON.
        resp = _run(self._routes.export_handler(_StubRequest(
            app=self.app, match_info={"job_id": new_id},
        )))
        self.assertEqual(resp.status, 200)
        self.assertEqual(
            json.loads(resp.body)["payload"],
            {"x": 99, "nested": {"a": 1}},
        )

        # 404 on unknown source
        resp = _run(self._routes.repeat_handler(_StubRequest(
            app=self.app, match_info={"job_id": "no-such"},
        )))
        self.assertEqual(resp.status, 404)

    def test_export_returns_payload(self):
        jid = self._add(note="exp", payload={"data": [1, 2, 3]})
        resp = _run(self._routes.export_handler(_StubRequest(
            app=self.app, match_info={"job_id": jid},
        )))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["payload"], {"data": [1, 2, 3]})
        # attachment header present
        self.assertIn("attachment", resp.headers["Content-Disposition"])
        self.assertIn(".json", resp.headers["Content-Disposition"])

        # also works for finished jobs (history rows)
        self.db.update_job(jid, status="running", prompt_id="p")
        self.db.mark_done(jid, prompt_id="p", outputs={"x": 1})
        resp = _run(self._routes.export_handler(_StubRequest(
            app=self.app, match_info={"job_id": jid},
        )))
        self.assertEqual(resp.status, 200)
        self.assertEqual(json.loads(resp.body)["payload"], {"data": [1, 2, 3]})

    def test_priority_ordering_in_claim_next(self):
        # Three jobs with ascending scheduled_at AND ascending priority.
        # Force them all to share the same queue_order so priority is the
        # only thing that breaks the tie.
        a = self.db.add_job(payload={"name": "low"},  scheduled_at=10.0, priority=1)
        b = self.db.add_job(payload={"name": "mid"},  scheduled_at=20.0, priority=5)
        c = self.db.add_job(payload={"name": "high"}, scheduled_at=30.0, priority=10)
        for jid in (a, b, c):
            self.db._conn.execute(  # type: ignore[attr-defined]
                "UPDATE scheduled_jobs SET queue_order=? WHERE id=?",
                (5000, jid),
            )

        claimed: list[str] = []
        for _ in range(3):
            row = self.db.claim_next_due_job()
            self.assertIsNotNone(row, "claim_next_due_job returned None early")
            claimed.append(json.loads(row["payload"])["name"])
        self.assertEqual(claimed, ["high", "mid", "low"])

    # ------------------------------------------------------------------
    # v0.3.14: future-only guard on scheduled_at across /add, /add-batch
    # and /update. See _MIN_SCHEDULE_OFFSET_SECONDS in routes.py.
    # ------------------------------------------------------------------

    def _add_body(self, scheduled_at, **extra):
        body = {"payload": {"x": 1}, "scheduled_at": scheduled_at}
        body.update(extra)
        return body

    def test_add_rejects_past_scheduled_at(self):
        # Clearly in the past -- the year 2000 is well below `time.time()`
        # by 25+ years so there is no flakiness from clock skew.
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app, json_body=self._add_body(946684800.0),  # 2000-01-01
        )))
        self.assertEqual(resp.status, 400)
        self.assertIn("future", resp.body.decode("utf-8", "replace"))

    def test_add_rejects_subthreshold_scheduled_at(self):
        # now + 2s -- below the 5s minimum that the UI already enforces.
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app, json_body=self._add_body(time.time() + 2),
        )))
        self.assertEqual(resp.status, 400)
        self.assertIn("5 seconds", resp.body.decode("utf-8", "replace"))

    def test_add_accepts_just_above_threshold(self):
        # now + 6s -- one second above the 5s floor. A slow CI tick could
        # push this below the threshold mid-test, so pad to now+10.
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app, json_body=self._add_body(time.time() + 10),
        )))
        self.assertEqual(resp.status, 201)

    def test_add_batch_skips_past_items(self):
        # Per-item validation: a past-time item is silently dropped, the
        # future-time siblings still land.
        items = [
            {"payload": {"i": 0}, "scheduled_at": _future_scheduled_at(60)},
            {"payload": {"i": 1}, "scheduled_at": 946684800.0},  # year 2000
            {"payload": {"i": 2}, "scheduled_at": _future_scheduled_at(120)},
        ]
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": items},
        )))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["count"], 2)
        self.assertEqual(len(body["added"]), 2)

    def test_update_rejects_past_scheduled_at(self):
        # Set up a real scheduled job first (using the helper), then try
        # to reschedule it to the past.
        ts = _future_scheduled_at(3600)
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app, json_body=self._add_body(ts, note="resched-me"),
        )))
        self.assertEqual(resp.status, 201)
        job_id = json.loads(resp.body)["id"]

        # Reschedule to a past timestamp -- must be rejected.
        resp = _run(self._routes.update_handler(_StubRequest(
            app=self.app,
            match_info={"job_id": job_id},
            json_body={"scheduled_at": 946684800.0},
        )))
        self.assertEqual(resp.status, 400)
        self.assertIn("future", resp.body.decode("utf-8", "replace"))

        # And the job's scheduled_at must NOT have been changed by the
        # rejected request (defence-in-depth against partial writes).
        current = self.db.get_job(job_id)
        assert current is not None  # we just created it
        self.assertEqual(current["scheduled_at"], ts)


# ---------------------------------------------------------------------------
# v0.3.10: workflow_title exposed through the HTTP layer.
# ---------------------------------------------------------------------------

class TestWorkflowTitleRoutes(unittest.TestCase):
    """Sidecar suite for the optional ``workflow_title`` field.

    The DB layer is covered exhaustively in test_database.py. These tests
    focus on the HTTP contract: list/get/repeat expose the column, add /
    add-batch / update accept it (with sane type validation), and the field
    survives the strip-payload discipline on /list.
    """

    def setUp(self):
        # Fresh DB per test.
        self.db_path = os.path.join(_tmpdir, "test_wt.sqlite3")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        from comfyui_scheduled_queue import database as db_mod
        from comfyui_scheduled_queue import routes
        self._db_mod = db_mod
        self._routes = routes
        self.db = db_mod.ScheduledQueueDB(db_path=self.db_path)
        self.app = {"sq_db": self.db}
        # These tests cover the HTTP contract, not payload validation; stub
        # the preflight seam so they stay hermetic (no live ComfyUI). The
        # validator itself is exercised in test_preflight.py and
        # PreflightIntegrationTests at the bottom of this file.
        from unittest.mock import patch as _patch
        _pf = _patch.object(routes, "_run_preflight", return_value=(True, []))
        _pf.start()
        self.addCleanup(_pf.stop)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    # ---- /add -----------------------------------------------------------

    def test_add_accepts_workflow_title(self):
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"x": 1},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": "My Workflow",
            },
        )))
        self.assertEqual(resp.status, 201)
        jid = json.loads(resp.body)["id"]
        # Stored in DB and readable.
        self.assertEqual(self.db.get_job(jid)["workflow_title"], "My Workflow")

    def test_add_without_workflow_title_defaults_blank(self):
        # No workflow_title in body -> stored as blank, not crashing.
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={"payload": {"x": 1}, "scheduled_at": _future_scheduled_at(100)},
        )))
        self.assertEqual(resp.status, 201)
        jid = json.loads(resp.body)["id"]
        self.assertFalse(self.db.get_job(jid)["workflow_title"])

    def test_add_rejects_non_string_workflow_title(self):
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"x": 1},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": 12345,  # int, not str
            },
        )))
        self.assertEqual(resp.status, 400)
        self.assertIn("workflow_title", json.loads(resp.body)["error"])

    def test_add_allows_null_workflow_title(self):
        # Explicit null is acceptable; DB layer normalises to nil.
        resp = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"x": 1},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": None,
            },
        )))
        self.assertEqual(resp.status, 201)
        jid = json.loads(resp.body)["id"]
        self.assertFalse(self.db.get_job(jid)["workflow_title"])

    # ---- /list ----------------------------------------------------------

    def test_list_exposes_workflow_title_without_payload(self):
        # Add three jobs with different titles.
        a_body = {"payload": {"x": 1}, "scheduled_at": _future_scheduled_at(100),
                  "workflow_title": "Alpha"}
        b_body = {"payload": {"x": 2}, "scheduled_at": _future_scheduled_at(200)}
        c_body = {"payload": {"x": 3}, "scheduled_at": _future_scheduled_at(300),
                  "workflow_title": ""}
        for body in (a_body, b_body, c_body):
            r = _run(self._routes.add_handler(_StubRequest(
                app=self.app, json_body=body,
            )))
            self.assertEqual(r.status, 201)

        resp = _run(self._routes.list_handler(_StubRequest(app=self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        # Each row carries workflow_title (or blank) but never the heavy payload.
        titles = sorted(
            (j.get("workflow_title") or "") for j in body["jobs"]
        )
        self.assertEqual(titles, ["", "", "Alpha"])
        # payload is stripped on /list — proves no accidental leak.
        for j in body["jobs"]:
            self.assertNotIn("payload", j)

    def test_list_does_not_strip_workflow_title(self):
        # Defensive: confirm the strip helper does not touch the new column.
        self.db.add_job(payload={"x": 1}, scheduled_at=100.0,
                        workflow_title="survives-strip")
        resp = _run(self._routes.list_handler(_StubRequest(app=self.app)))
        self.assertEqual(resp.status, 200)
        job = json.loads(resp.body)["jobs"][0]
        self.assertEqual(job["workflow_title"], "survives-strip")

    # ---- /job/{id} ------------------------------------------------------

    def test_get_job_with_outputs_exposes_workflow_title(self):
        # Live row path.
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"k": "v"},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": "Detail Title",
            },
        )))
        jid = json.loads(r.body)["id"]

        resp = _run(self._routes.job_detail_handler(_StubRequest(
            app=self.app, match_info={"job_id": jid},
        )))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["workflow_title"], "Detail Title")

    def test_get_job_with_outputs_history_exposes_workflow_title(self):
        # History row path: after mark_done the live row moves to job_history
        # but the title must follow.
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"k": "v"},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": "History Title",
            },
        )))
        jid = json.loads(r.body)["id"]
        self.db.update_job(jid, status="running", prompt_id="p")
        self.db.mark_done(jid, prompt_id="p", outputs={"x": 1})

        resp = _run(self._routes.job_detail_handler(_StubRequest(
            app=self.app, match_info={"job_id": jid},
        )))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertEqual(body["workflow_title"], "History Title")

    # ---- /update -------------------------------------------------------

    def test_update_accepts_workflow_title(self):
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"x": 1},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": "Original",
            },
        )))
        jid = json.loads(r.body)["id"]

        resp = _run(self._routes.update_handler(_StubRequest(
            app=self.app,
            match_info={"job_id": jid},
            json_body={"workflow_title": "Renamed"},
        )))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertIn("workflow_title", body["updated_fields"])
        self.assertEqual(self.db.get_job(jid)["workflow_title"], "Renamed")

    def test_update_clears_workflow_title_with_null(self):
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"x": 1},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": "Will be cleared",
            },
        )))
        jid = json.loads(r.body)["id"]

        resp = _run(self._routes.update_handler(_StubRequest(
            app=self.app,
            match_info={"job_id": jid},
            json_body={"workflow_title": None},
        )))
        self.assertEqual(resp.status, 200)
        self.assertFalse(self.db.get_job(jid)["workflow_title"])

    def test_update_rejects_non_string_workflow_title(self):
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={"payload": {"x": 1}, "scheduled_at": _future_scheduled_at(100)},
        )))
        jid = json.loads(r.body)["id"]

        resp = _run(self._routes.update_handler(_StubRequest(
            app=self.app,
            match_info={"job_id": jid},
            json_body={"workflow_title": ["list", "not", "allowed"]},
        )))
        self.assertEqual(resp.status, 400)
        self.assertIn("workflow_title", json.loads(resp.body)["error"])

    def test_update_workflow_title_alongside_other_fields(self):
        # Sanity: workflow_title is whitelisted, so it should pass the
        # unknown-field guard together with note.
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={"payload": {"x": 1}, "scheduled_at": _future_scheduled_at(100)},
        )))
        jid = json.loads(r.body)["id"]

        resp = _run(self._routes.update_handler(_StubRequest(
            app=self.app,
            match_info={"job_id": jid},
            json_body={"workflow_title": "Combo", "note": "tag"},
        )))
        self.assertEqual(resp.status, 200)
        updated = json.loads(resp.body)["updated_fields"]
        self.assertIn("workflow_title", updated)
        self.assertIn("note", updated)

    # ---- /add-batch ----------------------------------------------------

    def test_add_batch_per_item_workflow_title(self):
        items = [
            {"payload": {"i": 0}, "scheduled_at": _future_scheduled_at(100), "workflow_title": "Zero"},
            {"payload": {"i": 1}, "scheduled_at": _future_scheduled_at(200), "workflow_title": "One"},
            {"payload": {"i": 2}, "scheduled_at": _future_scheduled_at(300)},  # no title
        ]
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": items},
        )))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["count"], 3)

        rows = self.db.list_jobs_paginated(["scheduled"], limit=10, offset=0)
        titles = sorted((r.get("workflow_title") or "") for r in rows)
        self.assertEqual(titles, ["", "One", "Zero"])

    def test_add_batch_skips_item_with_invalid_workflow_title(self):
        # Per-item validation: a bad workflow_title in one item must not
        # poison the rest of the batch.
        items = [
            {"payload": {"i": 0}, "scheduled_at": _future_scheduled_at(100), "workflow_title": 99},
            {"payload": {"i": 1}, "scheduled_at": _future_scheduled_at(200), "workflow_title": "OK"},
        ]
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": items},
        )))
        # Whole batch still succeeds; the bad item is silently skipped
        # (spec: a single bad item must not fail the batch).
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["count"], 1)
        # Only the valid item is in the DB.
        rows = self.db.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["workflow_title"], "OK")

    # ---- /repeat -------------------------------------------------------

    def test_repeat_carries_workflow_title_from_source(self):
        # History path: finish a job then repeat it.
        r = _run(self._routes.add_handler(_StubRequest(
            app=self.app,
            json_body={
                "payload": {"x": 1},
                "scheduled_at": _future_scheduled_at(100),
                "workflow_title": "Repeat Me",
            },
        )))
        src_id = json.loads(r.body)["id"]
        self.db.update_job(src_id, status="running", prompt_id="p")
        self.db.mark_done(src_id, prompt_id="p", outputs={"i": ["x.png"]})

        resp = _run(self._routes.repeat_handler(_StubRequest(
            app=self.app, match_info={"job_id": src_id},
        )))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        new_id = body["id"]
        self.assertEqual(self.db.get_job(new_id)["workflow_title"], "Repeat Me")


class PreflightIntegrationTests(unittest.TestCase):
    """End-to-end: the add paths run the REAL validator on a REAL payload
    shape, with only the ComfyUI index fetch stubbed (hermetic — no live
    ComfyUI dependency, no HTTP)."""

    _OBJECT_INFO = {
        "EmptyImage": {
            "input": {"required": {
                "width": ["INT", {"default": 512, "min": 16, "max": 8192}],
                "height": ["INT", {"default": 512, "min": 16, "max": 8192}],
                "batch_size": ["INT", {"default": 1, "min": 1, "max": 4096}],
                "color": ["INT", {"default": 0, "min": 0, "max": 16777215}],
            }, "optional": {}},
            "output": ["IMAGE"],
        },
        "SaveImage": {
            "input": {"required": {"images": ["IMAGE"]},
                      "optional": {"filename_prefix": ["STRING", {"default": "ComfyUI"}]}},
            "output": [],
            "output_node": True,
        },
    }
    _MODEL_INDEX = {"checkpoints": [], "loras": []}

    def setUp(self):
        self.db_path = os.path.join(_tmpdir, "test_preflight_integration.sqlite3")
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        from comfyui_scheduled_queue import database as db_mod
        from comfyui_scheduled_queue import routes
        self._routes = routes
        self.db = db_mod.ScheduledQueueDB(db_path=self.db_path)
        self.app = {"sq_db": self.db}
        # Stub ONLY the index fetch so the test stays hermetic; the real
        # validator + the real `_run_preflight` seam run for real.
        from unittest.mock import patch as _patch
        _fetch = _patch.object(
            routes, "_fetch_comfyui_indexes",
            return_value=(self._OBJECT_INFO, self._MODEL_INDEX),
        )
        _fetch.start()
        self.addCleanup(_fetch.stop)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def _payload(self, prefix="preflight-it"):
        return {
            "1": {"class_type": "EmptyImage", "inputs": {
                "width": 64, "height": 64, "batch_size": 1, "color": 0}},
            "2": {"class_type": "SaveImage", "inputs": {
                "images": ["1", 0], "filename_prefix": prefix}},
        }

    def test_valid_payload_accepted(self):
        resp = _run(self._routes.add_handler(_StubRequest(app=self.app, json_body={
            "payload": self._payload(),
            "scheduled_at": _future_scheduled_at(600),
        })))
        self.assertEqual(resp.status, 201)

    def test_unknown_class_type_rejected_at_add_time(self):
        payload = self._payload()
        payload["3"] = {"class_type": "NoSuchNode", "inputs": {}}
        resp = _run(self._routes.add_handler(_StubRequest(app=self.app, json_body={
            "payload": payload,
            "scheduled_at": _future_scheduled_at(600),
        })))
        self.assertEqual(resp.status, 400)
        body = resp.body.decode("utf-8", "replace")
        self.assertIn("preflight failed", body)
        self.assertIn("missing_node_type", body)
        # And nothing entered the DB.
        self.assertEqual(self.db.list_jobs(), [])

    def test_batch_skips_bad_item_keeps_good(self):
        good = {"payload": self._payload(), "scheduled_at": _future_scheduled_at(600)}
        bad = {"payload": {"9": {"class_type": "NoSuchNode", "inputs": {}}},
               "scheduled_at": _future_scheduled_at(700)}
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": [good, bad]})))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["count"], 1)
        self.assertEqual(len(body["added"]), 1)


if __name__ == "__main__":
    unittest.main()
