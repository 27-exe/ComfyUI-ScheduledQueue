from __future__ import annotations

import asyncio
import json
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(os.path.dirname(_HERE), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from comfyui_scheduled_queue import routes  # noqa: E402


class _App:
    def __init__(self, db, url="http://comfyui.test"):
        self.values = {"sq_db": db, "sq_comfyui_url": url}

    def get(self, key, default=None):
        return self.values.get(key, default)


class _Request:
    def __init__(self, db):
        self.app = _App(db)


class TestPauseAsync(unittest.TestCase):
    def test_pause_all_uses_blocking_worker_and_preserves_schema(self):
        db = object()
        worker_result = {
            "paused": True,
            "reclaimed_count": 2,
            "cancelled_count": 2,
            "error_count": 0,
            "errors": [],
        }
        to_thread = AsyncMock(return_value=routes._json_response(worker_result))
        with patch.object(routes.asyncio, "to_thread", to_thread), patch.object(
            routes, "_pause_all_blocking", return_value=worker_result
        ) as blocking:
            response = asyncio.run(routes.pause_all_handler(_Request(db)))

        blocking.assert_not_called()
        to_thread.assert_awaited_once()
        self.assertEqual(to_thread.await_args.args[1:], (db, "http://comfyui.test"))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), worker_result)

    def test_pause_running_all_uses_blocking_worker_and_preserves_schema(self):
        db = object()
        worker_result = {
            "paused": True,
            "reclaimed_count": 0,
            "cancelled_count": 1,
            "interrupted_count": 1,
            "error_count": 0,
            "errors": [],
        }
        to_thread = AsyncMock(return_value=routes._json_response(worker_result))
        with patch.object(routes.asyncio, "to_thread", to_thread), patch.object(
            routes, "_pause_running_all_blocking", return_value=worker_result
        ) as blocking:
            response = asyncio.run(routes.pause_running_all_handler(_Request(db)))

        blocking.assert_not_called()
        to_thread.assert_awaited_once()
        self.assertEqual(to_thread.await_args.args[1:], (db, "http://comfyui.test"))
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(response.body), worker_result)


if __name__ == "__main__":
    unittest.main()
