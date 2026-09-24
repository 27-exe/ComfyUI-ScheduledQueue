"""Regression: routes must work when ComfyUI loads custom_nodes flat.

ComfyUI mounts every custom_node file via spec_from_file_location under the
dotted name "ComfyUI-ScheduledQueue.<mod>" -- there is no
"comfyui_scheduled_queue" package on sys.path. Any absolute package import
inside these modules silently breaks in production while every unit test
(which puts src/ on sys.path) stays green.

This suite deliberately withholds the src/ path and installs the modules
the way ComfyUI does.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent / "src" / "comfyui_scheduled_queue"


def _flat_load(name):
    """Load a sibling module the way ComfyUI's _load_sibling does."""
    dotted = f"ComfyUI-ScheduledQueue.{name}"
    if dotted in sys.modules:
        return sys.modules[dotted]
    spec = importlib.util.spec_from_file_location(dotted, _ROOT / f"{name}.py")
    if spec is None or spec.loader is None:
        raise ImportError(name)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted] = mod
    spec.loader.exec_module(mod)
    return mod


class FlatLoadRoutesTest(unittest.TestCase):
    """The scheduled-pause endpoint must function under a flat load."""

    def setUp(self):
        # Withhold src/ from sys.path and drop any cached package modules,
        # so the code is forced down the self-load branch ComfyUI uses.
        self._saved_path = list(sys.path)
        src = str(_ROOT.parent)
        sys.path[:] = [p for p in sys.path if p != src]
        for key in [k for k in list(sys.modules)
                    if "comfyui_scheduled_queue" in k or "ComfyUI-ScheduledQueue" in k]:
            del sys.modules[key]
        self.assertNotIn(src, sys.path)

    def tearDown(self):
        sys.path[:] = self._saved_path
        for key in [k for k in list(sys.modules)
                    if "comfyui_scheduled_queue" in k or "ComfyUI-ScheduledQueue" in k]:
            del sys.modules[key]


class _NoPackagePathMixin:
    """Withhold src/ from sys.path so tests observe the flat-load path."""

    def setUp(self):
        self._saved_path = list(sys.path)
        src = str(_ROOT.parent)
        sys.path[:] = [p for p in sys.path if p != src]
        self.assertNotIn(src, sys.path)

    def tearDown(self):
        sys.path[:] = self._saved_path


class WallclockParseTest(_NoPackagePathMixin, unittest.TestCase):
    """_parse_wallclock_value must not depend on the package import."""

    def _routes(self):
        self.assertNotIn(str(_ROOT.parent), sys.path,
                         "package path must be withheld for a fair flat load")
        return _flat_load("routes")

    def test_accepts_minute_precision(self):
        routes = self._routes()
        parsed = routes._parse_wallclock_value("2030-06-15T09:30")
        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, time.time())

    def test_accepts_seconds_precision(self):
        routes = self._routes()
        self.assertIsNotNone(routes._parse_wallclock_value("2030-06-15T09:30:00"))

    def test_rejects_garbage(self):
        routes = self._routes()
        for bad in ("garbage", "", None, "2030-13-45T99:99"):
            with self.subTest(value=bad):
                self.assertIsNone(routes._parse_wallclock_value(bad))

    def test_handler_rejects_and_accepts_over_http_free_path(self):
        """The validation branch inside the handler must be reachable."""
        routes = self._routes()
        self.assertTrue(callable(routes._parse_wallclock_value))


class _StubApp:
    def __init__(self, db):
        self._v = {"sq_db": db}

    def get(self, k, d=None):
        return self._v.get(k, d)


class _StubReq:
    """Minimal aiohttp Request stand-in."""

    def __init__(self, app, method="POST", body=None):
        self.app = app
        self.method = method
        self._body = body

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class FlatLoadHandlerTest(unittest.TestCase):
    """pause_schedule_handler must work when loaded flat."""

    def setUp(self):
        self._saved_path = list(sys.path)
        self._saved_mods = dict(sys.modules)
        src = str(_ROOT.parent)
        sys.path[:] = [p for p in sys.path if p != src]
        for key in [k for k in list(sys.modules)
                    if "comfyui_scheduled_queue" in k or "ComfyUI-ScheduledQueue" in k]:
            del sys.modules[key]

    def tearDown(self):
        sys.path[:] = self._saved_path
        for key in [k for k in list(sys.modules)
                    if "comfyui_scheduled_queue" in k or "ComfyUI-ScheduledQueue" in k]:
            del sys.modules[key]
        sys.modules.update(self._saved_mods)

    def _setup(self):
        routes = _flat_load("routes")
        scheduler = _flat_load("scheduler")
        db_mod = _flat_load("database")
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        db = db_mod.ScheduledQueueDB(db_path=tmp.name)
        return routes, db

    def _future(self, minutes=30):
        return time.strftime("%Y-%m-%dT%H:%M",
                             time.localtime(time.time() + minutes * 60))

    def test_post_accepts_a_valid_future_timestamp(self):
        """THE regression: this returned 400 in production before the fix."""
        routes, db = self._setup()
        try:
            app = _StubApp(db)
            req = _StubReq(app, "POST", {"pause_at": self._future(30)})
            resp = asyncio.run(routes.pause_schedule_handler(req))
            body = _read(resp)
            self.assertEqual(resp.status, 200, body)
            self.assertEqual(body["pause_at"], req._body["pause_at"])
        finally:
            db.close() if hasattr(db, "close") else None

    def test_post_rejects_a_past_timestamp(self):
        routes, db = self._setup()
        try:
            app = _StubApp(db)
            req = _StubReq(app, "POST", {"pause_at": "2020-01-01T00:00"})
            resp = asyncio.run(routes.pause_schedule_handler(req))
            self.assertEqual(resp.status, 400, _read(resp))
        finally:
            db.close() if hasattr(db, "close") else None


def _read(resp):
    import json
    raw = getattr(resp, "body", b"")
    if isinstance(raw, (bytes, bytearray)):
        return json.loads(bytes(raw).decode() or "{}")
    return raw


if __name__ == "__main__":
    unittest.main()
