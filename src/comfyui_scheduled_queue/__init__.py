"""ComfyUI-ScheduledQueue - backend + CLI wrapper.

Install by copying this directory to ``<ComfyUI>/custom_nodes/ComfyUI-ScheduledQueue/``
or symlinking it there. The package name (directory) **must** be one ComfyUI
recognises: lowercase, hyphen or underscore OK, no spaces.

The ComfyUI loader imports ``<plugin>/__init__.py``. ComfyUI guarantees:

* ``server.PromptServer`` is created **after** all custom_node imports.
* Custom_node imports must not block startup on missing optional deps
  (e.g. ``aiohttp``, ``requests``).

This module therefore defers all ComfyUI / aiohttp dependent work until
``try_install()`` is called, which happens via the explicit bootstrap hook
the spec mandates.

Public surface:

* ``WEB_DIRECTORY``   -- web/ folder, served under /extensions/<plugin>/...
* ``NODE_CLASS_MAPPINGS`` -- no nodes; this extension only adds HTTP routes
* ``try_install()``   -- call from ComfyUI startup; safe to call multiple
                          times; raises only on hard, unrecoverable errors.
"""
from __future__ import annotations

import atexit
import importlib.util
import logging
import os
import sys
import traceback

WEB_DIRECTORY = os.path.join(os.path.dirname(__file__), "web")
NODE_CLASS_MAPPINGS: dict = {}

__all__ = ["WEB_DIRECTORY", "NODE_CLASS_MAPPINGS", "try_install"]

_PKG = "ComfyUI-ScheduledQueue"
_MODULES = ("database", "routes", "prompt_interceptor", "scheduler")

_installed = False
_scheduler_thread = None


def _load_sibling(name: str):
    """Load a sibling module via importlib (so we never rely on sys.path
    containing this directory).
    """
    here = os.path.dirname(__file__)
    full = f"{_PKG}.{name}"
    if full in sys.modules:
        return sys.modules[full]
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(here, f"{name}.py")
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"[ScheduledQueue] cannot load sibling: {name}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[full] = mod
    spec.loader.exec_module(mod)
    globals()[name] = mod
    return mod


def try_install() -> bool:
    """Idempotent install entry point.

    Returns True on success (or already-installed), False on soft failure
    (e.g. PromptServer not ready yet, aiohttp missing). Never raises on
    expected startup-time failures -- ComfyUI is still booting.
    """
    global _installed, _scheduler_thread
    if _installed:
        return True

    # 1) Load siblings (no ComfyUI imports yet).
    log = logging.getLogger(__name__)
    try:
        for name in _MODULES:
            _load_sibling(name)
    except Exception:
        log.warning(
            "[ScheduledQueue] sibling module load failed:\n%s",
            traceback.format_exc(),
        )
        return False

    database = globals()["database"]
    routes = globals()["routes"]
    prompt_interceptor = globals()["prompt_interceptor"]
    scheduler = globals()["scheduler"]

    # 2) Open the SQLite database. Defer folder_paths lookup until inside
    #    the DB ctor so import smoke works without ComfyUI.
    try:
        db = database.ScheduledQueueDB()
    except Exception:
        log.warning(
            "[ScheduledQueue] database init failed:\n%s",
            traceback.format_exc(),
        )
        return False

    # 3) Recover orphans from any previous ComfyUI run.
    try:
        recovered = db.recover_orphans()
        if recovered:
            print(
                f"[ScheduledQueue] Recovered {recovered} orphan job(s); "
                f"status=interrupted, manual resume required"
            )
    except Exception:
        log.warning(
            "[ScheduledQueue] orphan recovery failed:\n%s",
            traceback.format_exc(),
        )

    if db.get_state("paused") is None:
        db.set_state("paused", "1")

    # 4) Wire HTTP routes. Lazy import inside setup_routes handles missing
    #    aiohttp / server.PromptServer gracefully.
    interceptor = prompt_interceptor.PromptInterceptor(db)
    routes.setup_routes(db, interceptor)

    # 5) Background scheduler thread (daemon, dies with ComfyUI).
    _scheduler_thread = scheduler.SchedulerThread(db)
    _scheduler_thread.start()

    atexit.register(_scheduler_thread.stop)

    print(f"[ScheduledQueue] Stage 3 initialised. db={db.db_path}")
    _installed = True
    return True


# ------------------------------------------------------------------
# ComfyUI import-time hook.
#
# When custom_nodes are imported, ComfyUI has not yet built PromptServer.
# We try once; on failure we silently exit. The plugin self-registers via
# the loader's known sentinel ``try_install`` function so tests and
# external bootstrap can call it later. We do NOT print noisy warnings on
# the first import -- that's the expected path.
# ------------------------------------------------------------------
try_install()
