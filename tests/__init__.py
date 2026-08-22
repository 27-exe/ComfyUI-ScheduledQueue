"""Force-import conftest so the per-process database path is overridden
before any test module imports ``database``.
"""
import os
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# Importing conftest has the side-effect of redirecting the default DB
# path to a tempfile. Both unittest discover and pytest will pick this
# file up via ``tests/__init__.py``.
from tests import conftest  # noqa: F401
