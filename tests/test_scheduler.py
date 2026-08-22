"""Scheduler worker tests. We mock urllib so no real ComfyUI is needed, and
fake a `urlopen` that returns canned /history and /prompt responses.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from comfyui_scheduled_queue import database, scheduler  # noqa: E402


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    return database.ScheduledQueueDB(db_path=tmp.name), tmp.name


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _make_worker(self, fake_post=None, fake_history=None):
        return scheduler.SchedulerThread(self.db, comfyui_url="http://fake-comfyui/")

    def test_paused_blocks_dispatch(self):
        self.db.set_state("paused", "1")
        self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db)
        # Even if POST succeeded, no row should be claimed.
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            u.return_value = _FakeResponse(200, json.dumps({"prompt_id": "p1"}))
            w.tick()
            self.assertEqual(u.call_count, 0)
        w.stop()

    def test_successful_dispatch_marks_running(self):
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0, note="runme")
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake-comfyui/")
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            u.return_value = _FakeResponse(200, json.dumps({"prompt_id": "p1"}))
            w.tick()
            self.assertGreaterEqual(u.call_count, 1)
            row = self.db.get_job(jid)
            self.assertEqual(row["status"], "running")
            self.assertEqual(row["prompt_id"], "p1")
        w.stop()

    def test_failed_dispatch_increments_retry(self):
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake-comfyui/")
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            # 5xx -> raise inside tick -> _dispatch_failure
            u.return_value = _FakeResponse(500, "boom")
            w.tick()
        row = self.db.get_job(jid)
        # Back to scheduled with retry_count=1
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["retry_count"], 1)
        w.stop()

    def test_reconcile_marks_done_when_history_says_success(self):
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-done")
        w = scheduler.SchedulerThread(self.db)

        def fake_history(prompt_id):
            return {"status": "success", "outputs": {"images": ["x.png"]}}

        w.reconcile(history_fetcher=fake_history)
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "done")
        self.assertEqual(hist[0]["prompt_id"], "p-done")
        self.assertIsNone(self.db.get_job(jid))
        w.stop()

    def test_reconcile_handles_real_comfyui_history_shape(self):
        # Regression: ComfyUI 1.49.6 /history/<id> returns a nested dict:
        #   {<prompt_id>: {status: {status_str: "success", completed: True},
        #                  outputs: {...}, ...}}
        # The old code did `record.get('status', '')` and then a str() .lower()
        # match against ("success", "error") — which never fired because
        # str(dict) is never equal to "success". Running jobs would loop
        # forever and never reach the done/failed terminal states.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="7afd-real-shape")
        w = scheduler.SchedulerThread(self.db)

        real_history_shape = {
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [],
            },
            "outputs": {
                "9": {"images": [{"filename": "out.png"}]},
            },
            "meta": {},
        }

        history_fetcher = MagicMock(return_value=real_history_shape)
        w.reconcile(history_fetcher=history_fetcher)

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1, "reconcile must finalize the running job")
        self.assertEqual(hist[0]["status"], "done")
        self.assertEqual(hist[0]["prompt_id"], "7afd-real-shape")
        self.assertIsNone(self.db.get_job(jid))
        history_fetcher.assert_called_once_with("7afd-real-shape")
        w.stop()

    def test_reconcile_marks_error_failed_with_empty_outputs(self):
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-error-empty-outputs")
        w = scheduler.SchedulerThread(self.db)
        record = {
            "status": {
                "status_str": "error",
                "completed": True,
                "messages": [],
            },
            "outputs": {},
        }
        history_fetcher = MagicMock(return_value=record)

        w.reconcile(history_fetcher=history_fetcher)

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "failed")
        self.assertEqual(
            hist[0]["error"],
            "ComfyUI reported error",
        )
        self.assertIsNone(self.db.get_job(jid))
        history_fetcher.assert_called_once_with("p-error-empty-outputs")
        w.stop()

    def test_reconcile_leaves_none_and_empty_record_as_running(self):
        ids = []
        for prompt_id in ("p-none", "p-empty"):
            jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
            self.db.update_job(jid, status="running", prompt_id=prompt_id)
            ids.append(jid)
        w = scheduler.SchedulerThread(self.db)
        history_fetcher = MagicMock(side_effect=[None, {}])

        w.reconcile(history_fetcher=history_fetcher)

        for jid in ids:
            self.assertEqual(self.db.get_job(jid)["status"], "running")
        self.assertEqual(self.db.list_history(), [])
        self.assertEqual(history_fetcher.call_args_list[0].args, ("p-none",))
        self.assertEqual(history_fetcher.call_args_list[1].args, ("p-empty",))
        w.stop()

    def test_reconcile_marks_failed_when_history_says_error(self):
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-err")
        w = scheduler.SchedulerThread(self.db)

        def fake_history(prompt_id):
            return {"status": "error", "error": "sampler crashed"}

        w.reconcile(history_fetcher=fake_history)
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "failed")
        self.assertEqual(hist[0]["error"], "sampler crashed")
        w.stop()

    def test_reconcile_leaves_unknown_as_running(self):
        # Honest behaviour: when history doesn't have a record, we keep the
        # job as running and let the next reconcile cycle try again.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-pending")
        w = scheduler.SchedulerThread(self.db)

        def fake_history(prompt_id):
            return None

        w.reconcile(history_fetcher=fake_history)
        self.assertEqual(self.db.get_job(jid)["status"], "running")
        self.assertEqual(self.db.list_history(), [])
        w.stop()

    def test_background_thread_runs(self):
        # Smoke-test that the daemon thread is alive + auto-stops via daemon=True.
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake/")
        w.start()
        time.sleep(0.1)
        self.assertTrue(w._thread.is_alive())
        w.stop()
        time.sleep(0.1)
        self.assertFalse(w._thread.is_alive())


import os  # noqa: E402


if __name__ == "__main__":
    unittest.main(verbosity=2)
