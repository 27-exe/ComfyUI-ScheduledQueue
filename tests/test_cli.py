"""CLI wrapper tests for ``comfy-schedule``.

We spin up a tiny in-process HTTP server that mimics ComfyUI's
``/api/schedule/*`` endpoints. The CLI is then invoked as a subprocess so we
exercise argparse + urllib + JSON parsing for real.
"""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "scripts" / "comfy-schedule"
PORT = 18713


class _Handler(http.server.BaseHTTPRequestHandler):
    """Fake ComfyUI /api/schedule router backed by in-memory dict."""

    store = {
        "jobs": [],
        "paused": True,
        "last_dispatch_at": None,
        "last_error": None,
    }

    def log_message(self, *_args, **_kwargs):
        return

    def do_GET(self):
        if self.path == "/api/schedule/status":
            counts = {s: 0 for s in (
                "scheduled", "running", "interrupted", "done", "failed", "cancelled"
            )}
            for j in self.store["jobs"]:
                counts[j["status"]] = counts.get(j["status"], 0) + 1
            body = {
                "paused": self.store["paused"],
                "last_dispatch_at": self.store["last_dispatch_at"],
                "last_error": self.store["last_error"],
                "counts": counts,
            }
            self._json(200, body)
        elif self.path.startswith("/api/schedule/list"):
            self._json(200, {"jobs": list(self.store["jobs"]), "total": len(self.store["jobs"]), "filter": []})
        elif self.path == "/api/schedule/orphan-status":
            ids = [j["id"] for j in self.store["jobs"] if j["status"] == "interrupted"]
            self._json(200, {"interrupted_count": len(ids), "interrupted_ids": ids, "auto_retry_count": 0})
        else:
            self._json(404, {"error": "no"})

    def do_POST(self):
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8") if length else "{}"
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self._json(400, {"error": "bad json"})
            return
        if self.path == "/api/schedule/add":
            jid = f"j-{len(self.store['jobs']) + 1}"
            self.store["jobs"].append({
                "id": jid,
                "scheduled_at": payload.get("scheduled_at", 0),
                "status": "scheduled",
                "note": payload.get("note", ""),
                "priority": payload.get("priority", 100),
            })
            self._json(201, {"id": jid, "scheduled_at": payload.get("scheduled_at", 0),
                             "status": "scheduled"})
        elif self.path == "/api/schedule/pause-all":
            self.store["paused"] = True
            self._json(200, {"paused": True})
        elif self.path == "/api/schedule/resume-all":
            self.store["paused"] = False
            self._json(200, {"paused": False, "resumed_count": 0})
        elif self.path.startswith("/api/schedule/cancel/"):
            jid = self.path.rsplit("/", 1)[-1]
            for j in self.store["jobs"]:
                if j["id"] == jid:
                    j["status"] = "cancelled"
                    self._json(200, {"id": jid, "status": "cancelled"})
                    return
            self._json(404, {"error": "job not found"})
        else:
            self._json(404, {"error": "no"})

    def _json(self, status, body):
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def _run_cli(*args, stdin_data=None, env=None):
    """Run the CLI with the given args; return (rc, stdout, stderr)."""
    env = env or os.environ.copy()
    env["COMFYUI_URL"] = f"http://127.0.0.1:{PORT}"
    proc = subprocess.run(
        [sys.executable, str(CLI), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=10,
        input=stdin_data,
    )
    return proc.returncode, proc.stdout, proc.stderr


class TestCli(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.HTTPServer(("127.0.0.1", PORT), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # wait for socket ready
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.store = {
            "jobs": [],
            "paused": True,
            "last_dispatch_at": None,
            "last_error": None,
        }

    def test_status(self):
        rc, out, err = _run_cli("status")
        self.assertEqual(rc, 0, err)
        body = json.loads(out)
        self.assertTrue(body["paused"])

    def test_add_then_list_via_status(self):
        rc, out, _ = _run_cli(
            "add", "-", "--in", "60s", "--note", "cli test",
            stdin_data='{"3": {"class_type": "KSampler"}}',
        )
        self.assertEqual(rc, 0, out)
        body = json.loads(out)
        self.assertIn("id", body)

        rc, out, _ = _run_cli("status")
        self.assertEqual(rc, 0, out)
        body = json.loads(out)
        self.assertEqual(body["counts"]["scheduled"], 1)

    def test_pause_then_resume(self):
        rc, out, _ = _run_cli("pause")
        self.assertEqual(rc, 0, out)
        rc, out, _ = _run_cli("resume")
        self.assertEqual(rc, 0, out)

    def test_ids_only(self):
        for n in ("a", "b", "c"):
            _run_cli(
                "add", "-", "--in", "60s", "--note", n,
                stdin_data='{"3": {"class_type": "KSampler"}}',
            )
        rc, out, _ = _run_cli("list", "--ids-only")
        self.assertEqual(rc, 0, out)
        ids = [line for line in out.splitlines() if line]
        self.assertEqual(len(ids), 3)
        self.assertTrue(ids[0].startswith("j-"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
