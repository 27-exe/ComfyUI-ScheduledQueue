"""HTTP layer for the scheduled pause / resume rule.

These tests exercise ``routes.pause_schedule_handler`` directly with a
stub request, covering the contract the sidebar relies on:

  * GET returns the current pair as strings ("" when unarmed)
  * POST writes one field without clobbering the other
  * POST rejects malformed / past values with 400 and stores nothing
  * a cleared field yields ""
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from comfyui_scheduled_queue import database, routes  # noqa: E402


class _FakeApp:
    def __init__(self, db):
        self._values = {"sq_db": db, "sq_comfyui_url": "http://127.0.0.1:8188"}

    def get(self, key, default=None):
        return self._values.get(key, default)


class _StubRequest:
    """Minimal aiohttp Request stand-in: app, method and json body."""

    def __init__(self, app, method="GET", json_body=None):
        self.app = app
        self.method = method
        self._json_body = json_body

    async def json(self) -> Any:
        if self._json_body is None:
            raise ValueError("no json body")
        return self._json_body


def _run(coro):
    return asyncio.run(coro)


def _dtlocal(ts):
    return _dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M')


def _json_body(resp):
    """Both real aiohttp responses and routes' _StubResponse put JSON in
    .body (bytes)."""
    import json as _json
    raw = resp.body
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode()
    if isinstance(raw, str):
        return _json.loads(raw)
    return raw


class PauseScheduleHttpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = database.ScheduledQueueDB(
            db_path=str(Path(self.tmp.name) / 's.db')
        )
        self.app = _FakeApp(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def _get(self):
        return _run(routes.pause_schedule_handler(
            _StubRequest(self.app, method="GET")))

    def _post(self, body):
        return _run(routes.pause_schedule_handler(
            _StubRequest(self.app, method="POST", json_body=body)))

    # -- reads -----------------------------------------------------------

    def test_get_returns_empty_strings_when_unarmed(self):
        resp = self._get()
        self.assertEqual(resp.status, 200)
        body = _json_body(resp)
        self.assertEqual(body["pause_at"], "")
        self.assertEqual(body["resume_at"], "")

    def test_get_reflects_stored_values(self):
        self.db.set_pause_at("2030-01-01T08:30")
        self.db.set_resume_at("2030-01-01T09:45")
        body = _json_body(self._get())
        self.assertEqual(body["pause_at"], "2030-01-01T08:30")
        self.assertEqual(body["resume_at"], "2030-01-01T09:45")

    # -- writes ----------------------------------------------------------

    def test_post_stores_future_values(self):
        pause = _dtlocal(time.time() + 3600)
        resume = _dtlocal(time.time() + 7200)
        resp = self._post({"pause_at": pause, "resume_at": resume})
        self.assertEqual(resp.status, 200)
        self.assertEqual(self.db.get_pause_at(), pause)
        self.assertEqual(self.db.get_resume_at(), resume)

    def test_post_one_field_leaves_the_other_alone(self):
        """The sidebar edits each half independently, so a partial body
        must never clear the other rule."""
        pause = _dtlocal(time.time() + 3600)
        self.db.set_resume_at(_dtlocal(time.time() + 7200))
        self._post({"pause_at": pause})
        self.assertEqual(self.db.get_pause_at(), pause)
        self.assertTrue(self.db.get_resume_at())

    def test_post_clears_with_empty_string(self):
        self.db.set_pause_at(_dtlocal(time.time() + 3600))
        self._post({"pause_at": ""})
        self.assertFalse(self.db.get_pause_at())

    def test_post_clears_with_null(self):
        self.db.set_resume_at(_dtlocal(time.time() + 3600))
        self._post({"resume_at": None})
        self.assertFalse(self.db.get_resume_at())

    def test_post_empty_body_is_accepted(self):
        resp = self._post({})
        self.assertEqual(resp.status, 200)

    # -- rejections ------------------------------------------------------

    def test_post_rejects_past_value(self):
        resp = self._post({"pause_at": _dtlocal(time.time() - 60)})
        self.assertEqual(resp.status, 400)
        self.assertIsNone(self.db.get_pause_at())

    def test_post_rejects_garbage(self):
        resp = self._post({"pause_at": "not-a-date"})
        self.assertEqual(resp.status, 400)
        self.assertIsNone(self.db.get_pause_at())

    def test_post_rejects_non_string(self):
        resp = self._post({"pause_at": 12345})
        self.assertEqual(resp.status, 400)
        self.assertIsNone(self.db.get_pause_at())

    def test_post_rejects_non_object_body(self):
        resp = self._post(["2030-01-01T08:30"])
        self.assertEqual(resp.status, 400)

    def test_rejected_pair_stores_neither_field(self):
        """Validation happens before any write, so a bad resume value
        cannot leave a good pause value half-applied."""
        good_pause = _dtlocal(time.time() + 3600)
        resp = self._post({
            "pause_at": good_pause,
            "resume_at": "garbage",
        })
        self.assertEqual(resp.status, 400)
        self.assertIsNone(self.db.get_pause_at())
        self.assertIsNone(self.db.get_resume_at())

    def test_seconds_variant_is_accepted(self):
        future = _dt.datetime.fromtimestamp(time.time() + 3600)
        text = future.strftime('%Y-%m-%dT%H:%M:%S')
        self._post({"pause_at": text})
        self.assertEqual(self.db.get_pause_at(), text)


if __name__ == "__main__":
    unittest.main()
