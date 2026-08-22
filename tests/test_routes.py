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

    def __init__(self, *, app=None, json_body=None, match_info=None):
        self._app = app
        self._json_body = json_body
        self.match_info = match_info or {}

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


if __name__ == "__main__":
    unittest.main()
