"""The scheduled pause must fire under ComfyUI's flat module loading.

ComfyUI mounts custom_node files with spec_from_file_location, so there is
no ``comfyui_scheduled_queue`` package. The trigger used to do a bare
absolute import, raised ModuleNotFoundError, and swallowed it -- the pause
simply never happened, with one log line as the only evidence.

This test executes the REAL loader body under a forcibly withheld package.
It fails on the pre-fix code, which is why it exists.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"


def _loader_body(source):
    """Slice out `def _load_routes_module(...)` by TOP-LEVEL boundaries.

    Brace counting is not usable here: the body contains dict literals and
    f-string braces, and the params line contains `(` `)` too. Indentation
    is the reliable signal -- a top-level statement is the one that starts in
    column 0, so the function ends just before the next such line.
    """
    lines = source.split("\n")
    start = next(i for i, l in enumerate(lines)
                 if l.startswith("def _load_routes_module("))
    end = next(i for i in range(start + 1, len(lines))
               if lines[i] and not lines[i][0].isspace()
               and not lines[i].startswith(("#", ")", "]", "}")))
    return "\n".join(lines[start:end])


class FlatLoadPauseTest(unittest.TestCase):
    def setUp(self):
        self.scheduler_src = (_SRC / "comfyui_scheduled_queue" / "scheduler.py").read_text()
        # Save and then withhold the package completely.
        self._saved = {k: v for k, v in sys.modules.items()
                       if k.startswith("comfyui_scheduled_queue")}
        for k in list(self._saved):
            sys.modules.pop(k)
        self._saved_path = list(sys.path)
        sys.path[:] = [p for p in sys.path
                       if "src" not in Path(p).parts]

    def tearDown(self):
        sys.path[:] = self._saved_path
        for k in [k for k in sys.modules
                  if k.startswith(("ComfyUI-Sched", "comfyui_scheduled_queue"))]:
            sys.modules.pop(k, None)
        sys.modules.update(self._saved)

    def test_bare_absolute_import_actually_fails_here(self):
        """Guards the premise: without the package the old code raised."""
        with self.assertRaises(ImportError):
            from comfyui_scheduled_queue import routes  # noqa: F401

    def test_loader_falls_back_to_self_load(self):
        ns = {"__file__": str(_SRC / "comfyui_scheduled_queue" / "scheduler.py"),
              "__name__": "ComfyUI-ScheduledQueue.scheduler"}
        exec(_loader_body(self.scheduler_src), ns)
        routes = ns["_load_routes_module"]()
        self.assertTrue(
            hasattr(routes, "_pause_all_blocking"),
            "loader returned a module without the pause worker")
        self.assertIn("ComfyUI-ScheduledQueue.routes", sys.modules,
                      "the module must be registered under the loader's name")


if __name__ == "__main__":
    unittest.main()
