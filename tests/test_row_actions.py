"""Regression coverage for row-level queue actions and split global pause.

The database owns the guarded state transition.  The route layer owns the
ComfyUI side effect and deliberately leaves a state unchanged when ComfyUI
does not acknowledge the request.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from comfyui_scheduled_queue import database, routes  # noqa: E402


def _fresh_db():
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    handle.close()
    return database.ScheduledQueueDB(db_path=handle.name), handle.name


def _run(coro):
    return asyncio.run(coro)


class _App:
    def __init__(self, db, comfyui_url=None):
        self.values = {"sq_db": db}
        if comfyui_url is not None:
            self.values["sq_comfyui_url"] = comfyui_url

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Request:
    def __init__(self, app, job_id):
        self.app = app
        self.match_info = {"job_id": job_id}

    async def json(self):
        raise json.JSONDecodeError("empty", "", 0)


class TestRequeueDispatchedDatabase(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


    def test_requeue_dispatched_returns_job_to_immediately_due_queue(self):
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-dispatched")

        self.assertTrue(self.db.requeue_dispatched_job(jid))

        row = self.db.get_job(jid)
        self.assertEqual(row["status"], "scheduled")
        self.assertIsNone(row["prompt_id"])
        self.assertIsNone(row["dispatched_at"])
        self.assertIsNone(row["started_at"])
        self.assertLessEqual(row["scheduled_at"], time.time())

    def test_requeue_refuses_running_and_missing_jobs(self):
        running = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(running, "pid-running")
        self.db.mark_running(running, "pid-running")
        before = dict(self.db.get_job(running))

        self.assertFalse(self.db.requeue_dispatched_job(running))
        after = self.db.get_job(running)
        self.assertEqual(after["status"], "running")
        self.assertEqual(after["prompt_id"], "pid-running")
        self.assertIsNotNone(after["started_at"])
        self.assertEqual(after["dispatched_at"], before["dispatched_at"])

        self.assertFalse(self.db.requeue_dispatched_job("does-not-exist"))


class TestRowActionRoutes(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()
        self.app = _App(self.db, "http://comfy.test:8188")

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_requeue_dispatched_deletes_native_prompt_then_reschedules(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-q")

        with patch.object(routes, "_comfyui_post_json", return_value=(True, "ok")) as post:
            response = _run(routes.requeue_dispatched_handler(
                _Request(self.app, jid)
            ))

        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertEqual(body, {"id": jid, "status": "scheduled", "requeued": True})
        post.assert_called_once_with(
            "http://comfy.test:8188/queue", {"delete": ["pid-q"]},
        )
        row = self.db.get_job(jid)
        self.assertEqual(row["status"], "scheduled")
        self.assertIsNone(row["prompt_id"])

    def test_requeue_failure_does_not_change_dispatched_row(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-q")

        with patch.object(
            routes, "_comfyui_post_json", return_value=(False, "HTTP 503"),
        ):
            response = _run(routes.requeue_dispatched_handler(
                _Request(self.app, jid)
            ))

        self.assertEqual(response.status, 502)
        row = self.db.get_job(jid)
        self.assertEqual(row["status"], "dispatched")
        self.assertEqual(row["prompt_id"], "pid-q")

    def test_requeue_rejects_running_job_with_truthful_409(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-running")
        self.db.mark_running(jid, "pid-running")

        with patch.object(routes, "_comfyui_post_json") as post:
            response = _run(routes.requeue_dispatched_handler(
                _Request(self.app, jid)
            ))

        self.assertEqual(response.status, 409)
        self.assertIn("dispatched", json.loads(response.body)["error"].lower())
        post.assert_not_called()

    def test_cancel_running_interrupts_native_prompt_and_marks_failed(self):
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-run")
        self.db.mark_running(jid, "pid-run")

        with patch.object(routes, "_comfyui_post_json", return_value=(True, "ok")) as post:
            response = _run(routes.cancel_running_handler(
                _Request(self.app, jid)
            ))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["status"], "failed")
        post.assert_called_once_with(
            "http://comfy.test:8188/interrupt",
            {"prompt_id": "pid-run"},
        )
        self.assertIsNone(self.db.get_job(jid))
        self.assertEqual(self.db.list_history()[0]["status"], "failed")
        self.assertIn("cancelled", self.db.list_history()[0]["error"])

    def test_cancel_running_http_failure_keeps_job_running(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-run")
        self.db.mark_running(jid, "pid-run")

        with patch.object(
            routes, "_comfyui_post_json", return_value=(False, "HTTP 500"),
        ):
            response = _run(routes.cancel_running_handler(
                _Request(self.app, jid)
            ))

        self.assertEqual(response.status, 502)
        self.assertEqual(self.db.get_job(jid)["status"], "running")
        self.assertEqual(self.db.list_history(), [])

    def test_cancel_running_without_prompt_id_is_conflict(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, None)
        self.db.mark_running(jid, None)

        response = _run(routes.cancel_running_handler(_Request(self.app, jid)))

        self.assertEqual(response.status, 409)
        self.assertEqual(self.db.get_job(jid)["status"], "running")


class TestSplitGlobalPauseHandlers(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()
        self.app = _App(self.db, "http://comfy.test:8188")

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_pause_all_lists_only_dispatched_and_reclaims_only_dispatched(self):
        dispatched = self.db.add_job(payload={}, scheduled_at=1.0)
        running = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(dispatched, "pid-q")
        self.db.mark_dispatched(running, "pid-run")
        self.db.mark_running(running, "pid-run")

        with patch.object(
            routes, "_cancel_comfyui_queue", return_value=(1, 0, []),
        ) as cancel:
            response = _run(routes.pause_all_handler(_Request(self.app, "unused")))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["reclaimed_count"], 1)
        self.assertEqual(self.db.get_job(dispatched)["status"], "scheduled")
        self.assertEqual(self.db.get_job(running)["status"], "running")
        self.assertIsNotNone(self.db.get_job(running)["prompt_id"])
        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.args[0], [{"id": dispatched, "prompt_id": "pid-q", "status": "dispatched"}])

    def test_pause_running_all_interrupts_all_running_without_requeueing_them(self):
        first = self.db.add_job(payload={}, scheduled_at=1.0)
        second = self.db.add_job(payload={}, scheduled_at=1.0)
        dispatched = self.db.add_job(payload={}, scheduled_at=1.0)
        for jid, pid in ((first, "pid-1"), (second, "pid-2")):
            self.db.mark_dispatched(jid, pid)
            self.db.mark_running(jid, pid)
        self.db.mark_dispatched(dispatched, "pid-q")

        with patch.object(
            routes, "_cancel_comfyui_queue", return_value=(2, 0, []),
        ) as cancel:
            response = _run(routes.pause_running_all_handler(
                _Request(self.app, "unused")
            ))

        self.assertEqual(response.status, 200)
        body = json.loads(response.body)
        self.assertEqual(body["paused"], True)
        self.assertEqual(body["interrupted_count"], 2)
        self.assertEqual(body["reclaimed_count"], 0)
        self.assertEqual(self.db.get_job(first)["status"], "running")
        self.assertEqual(self.db.get_job(second)["status"], "running")
        self.assertEqual(self.db.get_job(dispatched)["status"], "dispatched")
        cancel.assert_called_once()
        self.assertEqual(
            {r["id"] for r in cancel.call_args.args[0]},
            {first, second},
        )

    def test_pause_running_all_preserves_status_when_comfyui_fails(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "pid-run")
        self.db.mark_running(jid, "pid-run")

        with patch.object(
            routes, "_cancel_comfyui_queue", return_value=(0, 1, ["HTTP 500"]),
        ):
            response = _run(routes.pause_running_all_handler(
                _Request(self.app, "unused")
            ))

        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body)["interrupted_count"], 0)
        self.assertEqual(json.loads(response.body)["error_count"], 1)
        self.assertEqual(self.db.get_job(jid)["status"], "running")
        self.assertEqual(self.db.get_state("paused"), "1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
