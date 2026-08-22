"""Non-blocking (to ComfyUI's event loop) scheduler worker."""
from __future__ import annotations

import copy
import importlib.util as _il_util
import json
import logging
import os as _os
import secrets
import sys as _sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# ComfyUI loads custom_nodes via ``importlib.util.spec_from_file_location``
# using the parent package name ``ComfyUI-ScheduledQueue`` as the module's
# dotted parent. A relative ``from .workflow_format import ...`` then resolves
# to ``ComfyUI-ScheduledQueue.workflow_format`` -- but workflow_format was
# never registered in ``sys.modules`` (only ``scheduler`` was), so the import
# raises ``ModuleNotFoundError`` and ComfyUI's ``_load_sibling`` swallows the
# failure, never installing our routes.
#
# Mirror the loader pattern used by ``__init__._load_sibling`` and explicitly
# self-load ``workflow_format`` into ``sys.modules`` under the same dotted name
# before importing its symbols. This keeps scheduler.py import-safe both when
# executed as a sibling file (production / ComfyUI) and as part of a real
# package (tests with ``sys.path.insert(0, 'src')``).
_wf_spec = _il_util.spec_from_file_location(
    "ComfyUI-ScheduledQueue.workflow_format",
    _os.path.join(_os.path.dirname(__file__), "workflow_format.py"),
)
if _wf_spec is None or _wf_spec.loader is None:
    raise ImportError(
        "[ScheduledQueue] cannot self-load workflow_format.py"
    )
_wf_mod = _il_util.module_from_spec(_wf_spec)
_sys.modules.setdefault(
    "ComfyUI-ScheduledQueue.workflow_format", _wf_mod
)
_wf_spec.loader.exec_module(_wf_mod)
convert_ui_to_api = _wf_mod.convert_ui_to_api
is_api_format = _wf_mod.is_api_format
del _il_util, _os, _sys, _wf_spec, _wf_mod

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pre-dispatch hook: emulate the ComfyUI frontend's `control_after_generate`
# processing that normally happens during graphToPrompt + queuePrompt.
#
# Why this is needed
# ------------------
# The ComfyUI web UI ships workflows whose node `inputs` dict contains both a
# `seed` (or `noise_seed`) and a sibling `control_after_generate` value
# ("fixed" | "randomize" | "increment" | "decrement"). When a user clicks
# "Queue Prompt", the frontend's widget machinery mutates the seed according
# to the chosen mode and then strips `control_after_generate` from the
# serialized payload before POSTing to /prompt.
#
# Our scheduler just POSTs the stored payload verbatim, so the same seed
# arrives at ComfyUI each run. ComfyUI then takes the cheap path through its
# execution cache (`execution_cached` / output cache reuse) and we get
# identical results every dispatch.
#
# Source references in this repo's ComfyUI checkout:
#   * comfy/comfy_types/node_typing.py:165      -- control_after_generate is a
#                                                  frontend-only metadata
#                                                  flag on input specs.
#   * comfyui_frontend_package/.../settingStore-CwkLtSKP.js
#         function applyWidgetControl           -- queued hook entry point
#         function nextValueForLinkedTarget     -- picks the right widget
#         function computeNextControlledValue   -- dispatches on mode
#         function computeNextNumberValue       -- INT/FLOAT (seed, noise_seed)
#         function computeNextComboValue        -- COMBO inputs
#   * comfyui_frontend_package/.../core-BqDAGg28.js
#         `Oe` Set                             -- strip-list for
#                                                  re-serialization
#                                                  (includes
#                                                  "control_after_generate").
#
# The frontend also has a `control_before_generate` user setting; when true
# the mutation happens before POST, when false it happens after. We always
# run it before POST because we have no widget state to update post-POST.
# ---------------------------------------------------------------------------

_SEED_FIELDS = ("seed", "noise_seed")
_COMBO_FIELDS = ()  # nothing we auto-handle today; reserved for future use.
# The frontend uses a JS-safe integer range of ±2**53 for INT widgets.
# We mirror that so `randomize` lands in the same band a user would see in the UI.
_INT53_MIN = -(2 ** 53)
_INT53_MAX = (2 ** 53) - 1
# ComfyUI's KSampler / RandomNoise seed bounds: 0 .. 2**64 - 1.
# Default `min`/`max` on `seed` widgets is exactly this (see nodes.py:1602).
_SEED_MIN = 0
_SEED_MAX = 0xFFFFFFFFFFFFFFFF  # 2**64 - 1


def _apply_control_after_generate(inputs: dict) -> None:
    """Mutate *inputs* in place, emulating the frontend widget hook.

    Only operates on the two seed-like fields. Any other input that happens
    to carry a `control_after_generate` flag is left alone — the frontend
    only wires up widgets for `seed` and `noise_seed`, and we don't have
    a node spec table to know which other COMBO widgets might want this
    treatment.
    """
    mode = inputs.get("control_after_generate")
    if not isinstance(mode, str):
        return
    for field in _SEED_FIELDS:
        if field not in inputs:
            continue
        try:
            current = int(inputs[field])
        except (TypeError, ValueError):
            # Non-numeric seed (e.g. still a string from a broken save):
            # leave it alone and let ComfyUI surface the error at execution.
            continue
        new_value = _next_number_value(current, mode)
        if new_value is not None:
            inputs[field] = new_value
    # Mirror the frontend: strip the directive after applying it so the
    # payload we POST looks exactly like one produced by Queue Prompt.
    inputs.pop("control_after_generate", None)


def _next_number_value(current: int, mode: str):
    """Reimplementation of computeNextNumberValue from the frontend.

    Returns the next value, or None for modes the frontend treats as
    "no-op" (e.g. ``fixed``).
    """
    if mode == "fixed":
        return None
    if mode == "increment":
        return current + 1
    if mode == "decrement":
        return current - 1
    if mode == "randomize":
        # Frontend draws uniformly from [min, max] at integer step size.
        # We draw via SystemRandom so two near-simultaneous dispatches
        # don't get the same PRNG state, then clamp to the same bounds
        # the widget UI would show.
        span = _SEED_MAX - _SEED_MIN + 1
        return _SEED_MIN + secrets.randbelow(span)
    if mode == "increment-wrap":
        # Treat wrap as "increment but loop around" -- not used by any
        # built-in widget today, but defined in the frontend source.
        return current + 1
    # Unknown mode: be conservative, don't touch the seed.
    log.warning("unknown control_after_generate mode %r; leaving seed unchanged", mode)
    return None


def _apply_pre_dispatch_hooks(prompt: dict) -> dict:
    """Run every frontend pre-POST mutation we know about on *prompt*.

    Today this is just the ``control_after_generate`` pass. New hooks
    discovered while reading the ComfyUI frontend source should be
    added here so the scheduler keeps emitting payloads indistinguishable
    from those produced by the Queue Prompt button.

    The returned dict is a fresh object; the caller can safely keep a
    reference to the input *prompt* for logging or comparison.
    """
    if not isinstance(prompt, dict):
        return prompt
    # Accept UI-format workflows pasted from the editor: ComfyUI's /prompt
    # endpoint rejects them, but the rest of this hook only knows how to
    # mutate API-format dicts (with ``inputs.seed``). Convert first, then
    # run the existing per-input hooks unchanged.
    if not is_api_format(prompt):
        prompt = convert_ui_to_api(prompt)
    if not isinstance(prompt, dict):
        return prompt
    out = copy.deepcopy(prompt)
    for _node_id, node in out.items():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if isinstance(inputs, dict):
            _apply_control_after_generate(inputs)
    return out


class SchedulerThread:
    POLL_INTERVAL = 1.0
    MAX_RETRIES = 3
    RECONCILE_INTERVAL = 5.0
    HISTORY_TIMEOUT = 4.0

    def __init__(self, db, comfyui_url="http://127.0.0.1:8188"):
        self.db = db
        self.comfyui_url = comfyui_url.rstrip('/')
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name='ScheduledQueue-Scheduler',
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout=5):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout)

    def _run(self):
        last_reconcile = 0.0
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                log.exception('scheduler tick failed')

            now = time.monotonic()
            if now - last_reconcile >= self.RECONCILE_INTERVAL:
                last_reconcile = now
                try:
                    self.reconcile()
                except Exception:
                    log.exception('scheduler reconcile failed')
            self._stop.wait(self.POLL_INTERVAL)

    def tick(self):
        if self.db.get_state('paused') != '0':
            log.debug('tick skipped: paused')
            return
        job = self.db.claim_next_due_job()
        if not job:
            return

        # === DEBUG: log raw payload seed values (BEFORE hook) ===
        try:
            raw_payload = json.loads(job['payload'])
            for node_id, node in (raw_payload.items() if isinstance(raw_payload, dict) else []):
                if not isinstance(node, dict):
                    continue
                inputs = node.get('inputs', {})
                if not isinstance(inputs, dict):
                    continue
                if 'seed' in inputs or 'control_after_generate' in inputs:
                    log.info(
                        '[SQ-DEBUG] job=%s node=%s class=%s seed=%s noise_seed=%s cag=%s',
                        job['id'][:8], node_id, node.get('class_type'),
                        inputs.get('seed'),
                        inputs.get('noise_seed'),
                        inputs.get('control_after_generate'),
                    )
        except Exception as exc:
            log.warning('[SQ-DEBUG] raw payload inspection failed: %s', exc)

        try:
            prompt = _apply_pre_dispatch_hooks(json.loads(job['payload']))

            # === DEBUG: log payload seed values (AFTER hook, BEFORE POST) ===
            try:
                for node_id, node in (prompt.items() if isinstance(prompt, dict) else []):
                    if not isinstance(node, dict):
                        continue
                    inputs = node.get('inputs', {})
                    if not isinstance(inputs, dict):
                        continue
                    if 'seed' in inputs:
                        log.info(
                            '[SQ-DEBUG] AFTER_HOOK job=%s node=%s seed=%s noise_seed=%s cag_present=%s',
                            job['id'][:8], node_id,
                            inputs.get('seed'),
                            inputs.get('noise_seed'),
                            'control_after_generate' in inputs,
                        )
            except Exception as exc:
                log.warning('[SQ-DEBUG] post-hook payload inspection failed: %s', exc)

            body = {
                'prompt': prompt,
                'client_id': job.get('client_id') or 'scheduled_queue',
            }
            log.info(
                '[SQ-DEBUG] POST /prompt job=%s client_id=%s prompt_node_count=%s',
                job['id'][:8], body['client_id'], len(prompt) if isinstance(prompt, dict) else '?',
            )
            req = urllib.request.Request(
                self.comfyui_url + '/prompt',
                data=json.dumps(body).encode(),
                headers={'Content-Type': 'application/json'},
                method='POST',
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                if response.status < 200 or response.status >= 300:
                    raise RuntimeError(f'HTTP {response.status}')
                result = json.loads(response.read().decode() or '{}')
            prompt_id = result.get('prompt_id')
            log.info('[SQ-DEBUG] POST /prompt response prompt_id=%s full=%s', prompt_id, result)
            if not prompt_id:
                raise RuntimeError('ComfyUI response did not include prompt_id')
            self.db.mark_running(job['id'], prompt_id)
            self.db.set_state('last_dispatch_at', str(time.time()))
            log.info('dispatched %s as %s', job['id'], prompt_id)
        except Exception as exc:
            log.exception('[SQ-DEBUG] dispatch failed: %s', exc)
            self._dispatch_failure(job['id'], str(exc))

    def _dispatch_failure(self, job_id, error):
        n = self.db.increment_retry(job_id)
        if n > self.MAX_RETRIES:
            self.db.mark_failed(
                job_id,
                f'dispatch failed after {self.MAX_RETRIES} retries: {error}',
            )
        else:
            self.db.update_job(
                job_id,
                status='scheduled',
                scheduled_at=time.time() + min(60, 2**n),
                error=f'dispatch retry {n}/{self.MAX_RETRIES}: {error}',
            )
        self.db.set_state('last_error', error)

    def reconcile(self, history_fetcher=None):
        """Finalize running jobs using ComfyUI's nested history record.

        ComfyUI 0.33.0 returns ``/history/{prompt_id}`` as::

            {prompt_id: {
                "prompt": [...],
                "outputs": {...},  # omitted or empty on failure
                "status": {
                    "status_str": "success" | "error",
                    "completed": bool,
                    "messages": [...],
                },
                "meta": {...},
            }}

        A missing or empty history record is not evidence of completion, so
        the job remains ``running`` for a later reconcile pass.  An explicit
        ``error`` status wins over generic completion signals; otherwise
        success, an ``outputs`` value, or ``completed`` marks the job done.
        """
        for job in self.db.list_jobs(['running'], 10000):
            try:
                record = (
                    history_fetcher(job['prompt_id'])
                    if history_fetcher
                    else self._history(job['prompt_id'])
                )
                if not record:
                    continue

                status_field = record.get('status')
                if isinstance(status_field, dict):
                    status_str = str(status_field.get('status_str', '')).lower()
                    completed = status_field.get('completed') is True
                    error_msg = status_field.get('error') or record.get('error')
                else:
                    # Keep accepting the older bare-string test/API shape.
                    status_str = str(status_field or '').lower()
                    completed = False
                    error_msg = record.get('error')

                outputs = record.get('outputs')
                if status_str == 'success':
                    self.db.mark_done(job['id'], job['prompt_id'], outputs)
                elif status_str in ('error', 'failed', 'failure'):
                    self.db.mark_failed(
                        job['id'],
                        str(error_msg or 'ComfyUI reported error'),
                    )
                elif outputs is not None or completed:
                    self.db.mark_done(job['id'], job['prompt_id'], outputs)
            except Exception:
                log.exception('reconcile failed for %s', job['id'])

    def _history(self, prompt_id):
        req = urllib.request.Request(
            self.comfyui_url + '/history/' + urllib.parse.quote(prompt_id)
        )
        try:
            with urllib.request.urlopen(req, timeout=self.HISTORY_TIMEOUT) as r:
                data = json.loads(r.read().decode() or '{}')
            return data.get(prompt_id) or data
        except (urllib.error.HTTPError, urllib.error.URLError):
            return None
