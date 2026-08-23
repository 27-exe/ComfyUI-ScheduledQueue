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

    Fallback policy: if the payload carries a seed field but NO
    ``control_after_generate`` directive, treat it as ``"randomize"``.

    Why: ComfyUI's execution cache keys on the serialized prompt, so a
    repeated dispatch with the same seed hits the cache and returns the
    previous outputs in milliseconds — the user sees "instant" runs that
    didn't actually run. Cache reuse only matters when the user expects a
    fresh draw (randomize); fixed/increment flows already change the seed
    each round-trip. Defaulting to randomize for the no-directive case is
    therefore always the safe choice: if the user actually wanted fixed,
    they would have stored the directive, and the existing node only has a
    seed field if it's a sampler-style node where randomize is appropriate.
    """
    mode = inputs.get("control_after_generate")
    if not isinstance(mode, str):
        # No directive present. If the node carries a seed-like field,
        # default to randomize so cache reuse can't silently swallow the
        # dispatch. Nodes with neither seed nor directive are left alone.
        if any(f in inputs for f in _SEED_FIELDS):
            mode = "randomize"
        else:
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
    # --- Self-adaptive polling intervals ----------------------------------
    # The scheduler used to wake every 1s and reconcile every 5s. That works
    # fine while jobs are flying, but for a queue that sits idle for hours
    # it's pure CPU burn. Instead, we pick the next sleep based on what we
    # actually saw in the current tick:
    #
    #   * we just POSTed a prompt this tick ............ IDLE_DISPATCH = 1s
    #     (we want fast feedback so reconcile picks it up while the prompt
    #      is still warm in ComfyUI's queue and not yet running)
    #   * there's a `running` job .......................... RUNNING = 2s
    #     (running jobs can finish any second; poll hot but not greedy)
    #   * there's a `dispatched` job but nothing running .. QUEUED   = 3s
    #     (queued jobs wait their turn; polling hot enough to notice
    #      promotion to running within a few seconds)
    #   * nothing in flight, nothing due ................... IDLE   = 5s
    #     (the original 5s reconcile cadence was already fine for the
    #      long-tail idle case; preserve it)
    IDLE_INTERVAL = 5.0
    DISPATCH_INTERVAL = 1.0
    RUNNING_INTERVAL = 2.0
    QUEUED_INTERVAL = 3.0
    # --- Other constants --------------------------------------------------
    MAX_RETRIES = 3
    # How often (at minimum) we must sweep /history when something is in
    # flight. We only call reconcile() when there are dispatched/running
    # jobs — see `_should_reconcile` — but the *interval* between those
    # sweeps is still bounded so a long-running prompt doesn't go
    # un-reconciled forever.
    RECONCILE_INTERVAL = 5.0
    HISTORY_TIMEOUT = 4.0
    # Exponential backoff: on consecutive ComfyUI 5xx errors we double
    # the sleep interval up to BACKOFF_MAX. The very first 5xx jumps to
    # 2 * base, the second to 4 * base, etc.
    BACKOFF_BASE = IDLE_INTERVAL  # start doubling from the idle base
    BACKOFF_MAX = 30.0

    def __init__(self, db, comfyui_url="http://127.0.0.1:8188"):
        self.db = db
        self.comfyui_url = comfyui_url.rstrip('/')
        self._stop = threading.Event()
        self._thread = None
        # Number of consecutive ComfyUI calls (tick POST or reconcile
        # /history) that returned 5xx. Reset on any 2xx. Drives
        # exponential backoff.
        self._consecutive_5xx = 0

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
            # Track what happened this iteration so we can pick the right
            # next-sleep interval. The four signals we care about:
            #   dispatched_this_tick -- tick() actually POSTed a prompt
            #   has_running           -- something is in ComfyUI's executor
            #   has_dispatched        -- something is queued in ComfyUI
            #   (idle)                -- none of the above
            dispatched_this_tick = False
            try:
                dispatched_this_tick = self.tick()
            except Exception:
                log.exception('scheduler tick failed')

            now = time.monotonic()
            # Lazy reconcile: only sweep /history if there are jobs that
            # might have transitioned to a terminal state in ComfyUI.
            # This saves an HTTP call + DB write per iteration when the
            # queue is empty, which is the steady state for most of the
            # day in a low-traffic setup.
            if (
                now - last_reconcile >= self.RECONCILE_INTERVAL
                and self._has_in_flight_jobs()
            ):
                last_reconcile = now
                try:
                    self.reconcile()
                except Exception:
                    log.exception('scheduler reconcile failed')

            interval = self._compute_next_interval(dispatched_this_tick)
            self._stop.wait(interval)

    def _has_in_flight_jobs(self):
        """Cheap probe: is there any job that reconcile() could finalize?

        Reconcile only does useful work when ComfyUI might have finished
        something. We ask the DB for at most one row in
        ``(dispatched, running)``; if the answer is empty, skip the
        /history sweep entirely. This keeps idle costs at zero HTTP
        calls per loop iteration.
        """
        try:
            jobs = self.db.list_jobs(('dispatched', 'running'), 1)
        except Exception:
            # If the probe itself errors, fall through and reconcile
            # anyway — better to over-poll than to miss a finishing job.
            log.exception('in-flight probe failed; will reconcile anyway')
            return True
        return bool(jobs)

    def _compute_next_interval(self, dispatched_this_tick):
        """Pick the next sleep length based on current queue state.

        Priority (highest urgency first):
          1. just dispatched this tick ............. DISPATCH_INTERVAL
          2. running jobs present .................. RUNNING_INTERVAL
          3. dispatched jobs waiting in ComfyUI .... QUEUED_INTERVAL
          4. idle .................................. IDLE_INTERVAL

        Backoff (on consecutive ComfyUI 5xx) is applied as an UPPER
        cap — it can only *slow down* the cadence, never speed it up.
        Once ComfyUI is healthy again the cap is cleared by
        ``_record_comfyui_success`` and we naturally snap back to the
        right cadence for whatever state we are in.
        """
        # 1. What's the "natural" cadence for the current queue state?
        if dispatched_this_tick:
            natural = self.DISPATCH_INTERVAL
        else:
            try:
                inflight = self.db.list_jobs(('dispatched', 'running'), 10)
            except Exception:
                inflight = []
            has_running = any(j['status'] == 'running' for j in inflight)
            has_queued = any(j['status'] == 'dispatched' for j in inflight)
            if has_running:
                natural = self.RUNNING_INTERVAL
            elif has_queued:
                natural = self.QUEUED_INTERVAL
            else:
                natural = self.IDLE_INTERVAL

        # 2. If we're in a 5xx streak, the next sleep is the *max* of the
        #    natural cadence and the backoff-doubled value (capped at
        #    BACKOFF_MAX). Backoff is an upper bound, never a floor.
        if self._consecutive_5xx > 0:
            # Cap exponent so a very long outage cannot overflow;
            # BACKOFF_MAX clamps the final answer anyway.
            exponent = min(self._consecutive_5xx, 20)
            backed = self.BACKOFF_BASE * (2 ** exponent)
            backed = min(backed, self.BACKOFF_MAX)
            natural = max(natural, backed)

        # 3. Final guard: never exceed BACKOFF_MAX.
        return min(natural, self.BACKOFF_MAX)

    def _record_comfyui_success(self):
        """Called whenever a ComfyUI round-trip (POST /prompt, GET /history)
        returned 2xx. Resets the backoff counter."""
        if self._consecutive_5xx:
            log.info(
                'comfyui recovered after %d consecutive 5xx; clearing backoff',
                self._consecutive_5xx,
            )
            self._consecutive_5xx = 0

    def _record_comfyui_5xx(self, where):
        """Called on ComfyUI HTTP 5xx. Bumps the backoff counter."""
        self._consecutive_5xx += 1
        # Cap the exponent so we never overflow even after very long
        # outages; BACKOFF_MAX on `min()` will clamp the result anyway.
        log.warning(
            'comfyui 5xx in %s; backoff streak=%d (next sleep up to %.1fs)',
            where, self._consecutive_5xx,
            min(self.BACKOFF_BASE * (2 ** min(self._consecutive_5xx, 20)),
                self.BACKOFF_MAX),
        )

    def tick(self):
        """Dispatch at most one due job. Returns ``True`` if a job was
        actually POSTed to ComfyUI (and thus the scheduler should poll
        again quickly), ``False`` otherwise.

        Side effects on ``self._consecutive_5xx``:
          * +1 on any HTTP 5xx from ComfyUI
          * cleared on a successful 2xx response
        Network-level errors (URLError, timeouts, refused connections)
        do NOT count toward backoff — those are usually transient
        restarts, and we want to retry promptly.
        """
        if self.db.get_state('paused') != '0':
            log.debug('tick skipped: paused')
            return False
        job = self.db.claim_next_due_job()
        if not job:
            return False

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
            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    status_code = response.status
                    if 500 <= status_code < 600:
                        # Server-side error: bump backoff and refuse to
                        # mark the job running. The retry handler below
                        # will reschedule it on the standard backoff
                        # ladder.
                        self._record_comfyui_5xx('tick POST')
                        raise RuntimeError(f'HTTP {status_code}')
                    if status_code < 200 or status_code >= 300:
                        # 4xx — client's fault, retrying won't help, but
                        # it's not a server outage either, so don't touch
                        # the backoff streak.
                        raise RuntimeError(f'HTTP {status_code}')
                    result = json.loads(response.read().decode() or '{}')
                self._record_comfyui_success()
            except urllib.error.HTTPError as exc:
                # urllib raises HTTPError for non-2xx status codes from
                # urlopen when used in some flows; treat 5xx as backoff
                # signal but leave 4xx as a plain client error.
                if 500 <= getattr(exc, 'code', 0) < 600:
                    self._record_comfyui_5xx('tick POST')
                raise RuntimeError(f'HTTP {exc.code}') from exc
            prompt_id = result.get('prompt_id')
            log.info('[SQ-DEBUG] POST /prompt response prompt_id=%s full=%s', prompt_id, result)
            if not prompt_id:
                raise RuntimeError('ComfyUI response did not include prompt_id')
            # POST succeeded: the prompt now lives inside ComfyUI's native
            # queue (possibly behind another job). We mark it 'dispatched'
            # rather than 'running' — promote-to-running is reconcile's job,
            # which observes ComfyUI's /queue to decide when execution
            # actually starts. See reconcile() below.
            self.db.mark_dispatched(job['id'], prompt_id)
            self.db.set_state('last_dispatch_at', str(time.time()))
            log.info('dispatched %s as %s', job['id'], prompt_id)
            return True
        except Exception as exc:
            log.exception('[SQ-DEBUG] dispatch failed: %s', exc)
            self._dispatch_failure(job['id'], str(exc))
            return False

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

    def reconcile(self, history_fetcher=None, queue_fetcher=None):
        """Reconcile 'dispatched' and 'running' jobs against ComfyUI's state.

        ComfyUI's two endpoints tell us everything we need:

          * ``/queue`` returns ``{queue_running: [[..., prompt_id, ...], ...],
            queue_pending: [[..., prompt_id, ...], ...]}`` — the live
            state of ComfyUI's native queue.
          * ``/history/<prompt_id>`` returns the terminal record once
            execution finishes (see below for the nested-dict shape).

        State machine the reconcile pass enforces:

          * ``dispatched`` jobs whose prompt_id shows up in
            ``queue_running`` get promoted to ``running``.
          * ``dispatched`` jobs whose prompt_id shows up in
            ``queue_pending`` stay ``dispatched`` (they're queued behind
            another job in ComfyUI's executor — not yet running).
          * ``dispatched`` / ``running`` jobs whose prompt_id shows up in
            ``/history`` are finalised: success → ``done``, error →
            ``failed``. (The job migrates to ``job_history``.)
          * Anything else stays where it is; the next reconcile pass will
            try again.

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

        ``queue_fetcher`` (optional) is a callable ``() -> {'queue_running':
        [...], 'queue_pending': [...]}`` for testability; defaults to
        ``self._queue``. ``history_fetcher`` (optional) is the existing
        ``(prompt_id) -> record`` callable.
        """
        # Snapshot /queue once per reconcile cycle — every job in the loop
        # below asks the same question, and we don't want N HTTP calls.
        try:
            queue = (
                queue_fetcher()
                if queue_fetcher
                else self._queue()
            )
        except Exception:
            log.exception('reconcile: /queue fetch failed; skipping queue promotion')
            queue = None

        running_pids: set[str] = set()
        pending_pids: set[str] = set()
        if isinstance(queue, dict):
            for slot in ('queue_running', 'queue_pending'):
                items = queue.get(slot) or []
                if not isinstance(items, list):
                    continue
                for entry in items:
                    # Each entry is a 5-tuple
                    #   [job_number, prompt_id, workflow_json, metadata, output_node_ids]
                    # — ComfyUI's openapi spec puts prompt_id at index 1.
                    if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                        pid = entry[1]
                        if isinstance(pid, str):
                            (running_pids if slot == 'queue_running' else pending_pids).add(pid)

        # One pass covers both 'dispatched' and 'running' rows. Status
        # transitions inside the loop are no-ops for rows that don't
        # qualify (e.g. a 'running' row that is also in queue_running
        # simply has its prompt_id verified and status kept).
        for job in self.db.list_jobs(('dispatched', 'running'), 10000):
            try:
                pid = job.get('prompt_id')
                if not pid:
                    # No prompt_id yet — claim_next_due_job never finished,
                    # or the live row predates the dispatch-state split.
                    # Skip; we'll pick it up once mark_dispatched runs.
                    continue

                # 1. /queue membership decides dispatched -> running promotion.
                if (
                    job['status'] == 'dispatched'
                    and pid in running_pids
                ):
                    self.db.mark_running(job['id'], pid)
                    continue  # fresh state; don't re-finalise this tick

                # 2. /history decides running/dispatched -> done/failed.
                #    A prompt_id that vanished from both queue slots AND
                #    has no history record yet is treated as 'still
                #    running' — ComfyUI may have just taken it off the
                #    queue for execution. Be patient.
                record = (
                    history_fetcher(pid)
                    if history_fetcher
                    else self._history(pid)
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
                    self.db.mark_done(job['id'], pid, outputs)
                elif status_str in ('error', 'failed', 'failure'):
                    self.db.mark_failed(
                        job['id'],
                        str(error_msg or 'ComfyUI reported error'),
                    )
                elif outputs is not None or completed:
                    self.db.mark_done(job['id'], pid, outputs)
            except Exception:
                log.exception('reconcile failed for %s', job['id'])

    def _history(self, prompt_id):
        """Fetch /history/<prompt_id>; never raises on 5xx.

        On a 2xx response: clear the backoff streak.
        On a 5xx response: bump the backoff streak and return ``None`` so
        the caller leaves the job running. Network errors (URLError,
        timeout, connection refused) are also swallowed and return
        ``None`` without touching the streak — ComfyUI restarts shouldn't
        trigger backoff.
        """
        req = urllib.request.Request(
            self.comfyui_url + '/history/' + urllib.parse.quote(prompt_id)
        )
        try:
            with urllib.request.urlopen(req, timeout=self.HISTORY_TIMEOUT) as r:
                data = json.loads(r.read().decode() or '{}')
            self._record_comfyui_success()
            return data.get(prompt_id) or data
        except urllib.error.HTTPError as exc:
            if 500 <= getattr(exc, 'code', 0) < 600:
                self._record_comfyui_5xx(f'reconcile /history/{prompt_id[:8]}')
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return None

    def _queue(self):
        """Fetch ``/queue``; return a normalised dict or ``None``.

        Normalised shape::

            {"queue_running": [<entry>, ...],
             "queue_pending": [<entry>, ...]}

        where each ``<entry>`` is the raw 5-tuple ComfyUI emits — the
        reconcile pass reads ``entry[1]`` for the prompt_id.

        Failure modes mirror ``_history``: HTTP 5xx and network errors
        both return ``None`` so the caller can skip queue promotion
        this cycle rather than guess.
        """
        req = urllib.request.Request(self.comfyui_url + '/queue')
        try:
            with urllib.request.urlopen(req, timeout=self.HISTORY_TIMEOUT) as r:
                data = json.loads(r.read().decode() or '{}')
            self._record_comfyui_success()
            if not isinstance(data, dict):
                return None
            # Be defensive about missing slots — older ComfyUI versions
            # may omit one of them.
            return {
                'queue_running': list(data.get('queue_running') or []),
                'queue_pending': list(data.get('queue_pending') or []),
            }
        except urllib.error.HTTPError as exc:
            if 500 <= getattr(exc, 'code', 0) < 600:
                self._record_comfyui_5xx('reconcile /queue')
            return None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            return None
