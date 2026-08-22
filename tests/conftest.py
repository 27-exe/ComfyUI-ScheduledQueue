"""Global pytest-style hooks for the unittest test suite.

ComfyUI's custom-node loader uses a per-user folder for the SQLite
database. When this package is imported outside of a real ComfyUI
(e.g. by the unit-test runner), ``database._default_db_path()`` falls
back to ``../user/`` relative to the package -- which would otherwise
create an unwanted database file inside the source tree.

This conftest is auto-discovered by both ``unittest discover`` and
``pytest``. It forces every database call inside the test process to
point at a temporary file, leaving the source tree untouched.
"""
import os
import sys
import tempfile
from pathlib import Path

# Make ``src/`` importable for both runners.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

# Import AFTER sys.path tweak so the package sees our path first.
from comfyui_scheduled_queue import database  # noqa: E402


_tmpdir = tempfile.TemporaryDirectory(prefix="sq-test-")
_tmp_db = os.path.join(_tmpdir.name, "scheduled_queue.sqlite3")
database._default_db_path = lambda: _tmp_db  # type: ignore[assignment]


def pytest_unconfigure(config):  # pragma: no cover - pytest hook
    _tmpdir.cleanup()


def teardown_module(module):  # pragma: no cover - unittest safety net
    try:
        _tmpdir.cleanup()
    except Exception:
        pass
