"""Routes handler tests -- exercise the reorder, pause/resume and update
endpoints. We bypass aiohttp by calling the handler coroutines directly
with a minimal stub Request that records what the handler reads.
"""
import asyncio
import json
import os
import sys
import tempfile
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
    return asyncio.get_event_loop().run_until_complete(coro)


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

    def test_pause_then_status_reflects_state(self):
        # status should report True before pause, and reflect the DB state
        # after a toggle (not the previously hard-coded True).
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertEqual(resp.status, 200)
        body = json.loads(resp.body)
        self.assertTrue(body["paused"], "default state should be paused")

        _run(self._routes.pause_all_handler(_StubRequest(app=self.app)))
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        self.assertTrue(json.loads(resp.body)["paused"])

        _run(self._routes.resume_all_handler(_StubRequest(app=self.app)))
        resp = _run(self._routes.status_handler(_StubRequest(app=self.app)))
        body = json.loads(resp.body)
        self.assertFalse(body["paused"], "status must reflect DB after resume")

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
        items = [
            {"payload": {"i": 0}, "scheduled_at": 100.0, "priority": 5},
            {"payload": {"i": 1}, "scheduled_at": 200.0, "priority": 7},
            {"payload": {"i": 2}, "scheduled_at": 300.0, "priority": 9},
        ]
        resp = _run(self._routes.add_batch_handler(_StubRequest(
            app=self.app, json_body={"items": items},
        )))
        self.assertEqual(resp.status, 201)
        body = json.loads(resp.body)
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["added"]), 3)
        self.assertEqual({row["scheduled_at"] for row in body["added"]},
                         {100.0, 200.0, 300.0})
        # rows are real — fetch by id
        for row in body["added"]:
            self.assertIsNotNone(self.db.get_job(row["id"]))

    def test_add_batch_rejects_over_50_items(self):
        items = [
            {"payload": {"i": i}, "scheduled_at": 100.0 + i}
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


if __name__ == "__main__":
    unittest.main()
