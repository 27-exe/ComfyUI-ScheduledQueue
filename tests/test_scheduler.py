"""Scheduler worker tests. We mock urllib so no real ComfyUI is needed, and
fake a `urlopen` that returns canned /history and /prompt responses.
"""
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from comfyui_scheduled_queue import database, scheduler  # noqa: E402


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    return database.ScheduledQueueDB(db_path=tmp.name), tmp.name


class _FakeResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body.encode() if isinstance(body, str) else body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestScheduler(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _make_worker(self, fake_post=None, fake_history=None):
        return scheduler.SchedulerThread(self.db, comfyui_url="http://fake-comfyui/")

    def test_paused_blocks_dispatch(self):
        self.db.set_state("paused", "1")
        self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db)
        # Even if POST succeeded, no row should be claimed.
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            u.return_value = _FakeResponse(200, json.dumps({"prompt_id": "p1"}))
            w.tick()
            self.assertEqual(u.call_count, 0)
        w.stop()

    def test_successful_dispatch_marks_dispatched(self):
        # After a successful POST /prompt the scheduler must land the job
        # in 'dispatched' (NOT 'running') — promotion to 'running' is the
        # reconcile loop's job, once ComfyUI's /queue shows the prompt as
        # the currently-executing item.
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0, note="runme")
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake-comfyui/")
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            u.return_value = _FakeResponse(200, json.dumps({"prompt_id": "p1"}))
            w.tick()
            self.assertGreaterEqual(u.call_count, 1)
            row = self.db.get_job(jid)
            self.assertEqual(row["status"], "dispatched")
            self.assertEqual(row["prompt_id"], "p1")
        w.stop()

    def test_failed_dispatch_increments_retry(self):
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake-comfyui/")
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            # 5xx -> raise inside tick -> _dispatch_failure
            u.return_value = _FakeResponse(500, "boom")
            w.tick()
        row = self.db.get_job(jid)
        # Back to scheduled with retry_count=1
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["retry_count"], 1)
        w.stop()

    def test_reconcile_marks_done_when_history_says_success(self):
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-done")
        w = scheduler.SchedulerThread(self.db)

        def fake_history(prompt_id):
            return {"status": "success", "outputs": {"images": ["x.png"]}}

        w.reconcile(history_fetcher=fake_history)
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "done")
        self.assertEqual(hist[0]["prompt_id"], "p-done")
        self.assertIsNone(self.db.get_job(jid))
        w.stop()

    def test_reconcile_handles_real_comfyui_history_shape(self):
        # Regression: ComfyUI 1.49.6 /history/<id> returns a nested dict:
        #   {<prompt_id>: {status: {status_str: "success", completed: True},
        #                  outputs: {...}, ...}}
        # The old code did `record.get('status', '')` and then a str() .lower()
        # match against ("success", "error") — which never fired because
        # str(dict) is never equal to "success". Running jobs would loop
        # forever and never reach the done/failed terminal states.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="7afd-real-shape")
        w = scheduler.SchedulerThread(self.db)

        real_history_shape = {
            "status": {
                "status_str": "success",
                "completed": True,
                "messages": [],
            },
            "outputs": {
                "9": {"images": [{"filename": "out.png"}]},
            },
            "meta": {},
        }

        history_fetcher = MagicMock(return_value=real_history_shape)
        w.reconcile(history_fetcher=history_fetcher)

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1, "reconcile must finalize the running job")
        self.assertEqual(hist[0]["status"], "done")
        self.assertEqual(hist[0]["prompt_id"], "7afd-real-shape")
        self.assertIsNone(self.db.get_job(jid))
        history_fetcher.assert_called_once_with("7afd-real-shape")
        w.stop()

    def test_reconcile_marks_error_failed_with_empty_outputs(self):
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-error-empty-outputs")
        w = scheduler.SchedulerThread(self.db)
        record = {
            "status": {
                "status_str": "error",
                "completed": True,
                "messages": [],
            },
            "outputs": {},
        }
        history_fetcher = MagicMock(return_value=record)

        w.reconcile(history_fetcher=history_fetcher)

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "failed")
        self.assertEqual(
            hist[0]["error"],
            "ComfyUI reported error",
        )
        self.assertIsNone(self.db.get_job(jid))
        history_fetcher.assert_called_once_with("p-error-empty-outputs")
        w.stop()

    def test_reconcile_leaves_none_and_empty_record_as_running(self):
        ids = []
        for prompt_id in ("p-none", "p-empty"):
            jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
            self.db.update_job(jid, status="running", prompt_id=prompt_id)
            ids.append(jid)
        w = scheduler.SchedulerThread(self.db)
        history_fetcher = MagicMock(side_effect=[None, {}])

        w.reconcile(history_fetcher=history_fetcher)

        for jid in ids:
            row = self.db.get_job(jid)
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "running")
        self.assertEqual(self.db.list_history(), [])
        self.assertEqual(history_fetcher.call_args_list[0].args, ("p-none",))
        self.assertEqual(history_fetcher.call_args_list[1].args, ("p-empty",))
        w.stop()

    def test_reconcile_marks_failed_when_history_says_error(self):
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-err")
        w = scheduler.SchedulerThread(self.db)

        def fake_history(prompt_id):
            return {"status": "error", "error": "sampler crashed"}

        w.reconcile(history_fetcher=fake_history)
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "failed")
        self.assertEqual(hist[0]["error"], "sampler crashed")
        w.stop()

    def test_reconcile_leaves_unknown_as_running(self):
        # Honest behaviour: when history doesn't have a record, we keep the
        # job as running and let the next reconcile cycle try again.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-pending")
        w = scheduler.SchedulerThread(self.db)

        def fake_history(prompt_id):
            return None

        w.reconcile(history_fetcher=fake_history)
        self.assertEqual(self.db.get_job(jid)["status"], "running")
        self.assertEqual(self.db.list_history(), [])
        w.stop()

    def test_background_thread_runs(self):
        # Smoke-test that the daemon thread is alive + auto-stops via daemon=True.
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake/")
        w.start()
        time.sleep(0.1)
        self.assertTrue(w._thread.is_alive())
        w.stop()
        time.sleep(0.1)
        self.assertFalse(w._thread.is_alive())

    # ------------------------------------------------------------------
    # Dispatched / running state separation (v0.3.x state-machine split)
    # ------------------------------------------------------------------

    def test_mark_dispatched(self):
        """``mark_dispatched`` flips status to 'dispatched', stamps
        ``dispatched_at``, and stores the ComfyUI prompt_id returned by
        POST /prompt — distinct from the pre-split ``mark_running``
        helper, which now only runs when reconcile observes the prompt
        as the currently-executing item in ComfyUI's /queue.
        """
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0, note="go")
        # Claim flips to 'dispatched' but leaves prompt_id NULL —
        # exactly the state tick() should observe immediately before
        # POSTing the prompt.
        claimed = self.db.claim_next_due_job()
        assert claimed is not None
        self.assertEqual(claimed["status"], "dispatched")
        self.assertIsNone(claimed["prompt_id"])
        self.assertIsNotNone(claimed["dispatched_at"])

        # Scheduler.tick() would call this with the prompt_id returned
        # by ComfyUI's POST /prompt response.
        self.assertTrue(self.db.mark_dispatched(jid, "p-from-comfyui"))

        row = self.db.get_job(jid)
        assert row is not None
        self.assertEqual(row["status"], "dispatched",
                         "tick() must land on 'dispatched', not 'running'")
        self.assertEqual(row["prompt_id"], "p-from-comfyui")
        # dispatched_at may be re-stamped by mark_dispatched (claim time
        # vs POST time are not always identical); we only require it
        # remains a finite, recent timestamp.
        self.assertIsNotNone(row["dispatched_at"])

    def test_reconcile_marks_dispatched_to_running(self):
        """A 'dispatched' job whose prompt_id is in ComfyUI's
        ``queue_running`` slot gets promoted to 'running' on the next
        reconcile pass — this is what makes the new 'dispatched' state
        visible to the UI as a transient state between POST and
        execution start.
        """
        self.db.set_state("paused", "0")
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        # State after a successful POST /prompt:
        self.db.update_job(jid, status="dispatched", prompt_id="p-waiting")

        w = scheduler.SchedulerThread(self.db)

        def fake_queue():
            # ComfyUI's /queue returns a dict with two lists of 5-tuples.
            # prompt_id lives at index 1 of each entry.
            return {
                "queue_running": [
                    [0, "p-waiting", {}, {}, []],
                ],
                "queue_pending": [],
            }

        def fake_history(prompt_id):
            # No terminal record yet — we're still mid-run.
            return None

        w.reconcile(history_fetcher=fake_history, queue_fetcher=fake_queue)
        row = self.db.get_job(jid)
        assert row is not None
        self.assertEqual(
            row["status"], "running",
            "dispatched + in queue_running must promote to running",
        )
        self.assertEqual(row["prompt_id"], "p-waiting")
        # No terminal history row yet.
        self.assertEqual(self.db.list_history(), [])
        w.stop()

    def test_reconcile_marks_dispatched_to_done(self):
        """A 'dispatched' job whose prompt_id shows up in
        ``/history/<id>`` is finalised straight to 'done' (or 'failed'),
        without needing a separate 'running' leg first. This covers the
        race where ComfyUI finishes a prompt before reconcile gets a
        chance to promote it.
        """
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        # Same state tick() leaves behind.
        self.db.update_job(jid, status="dispatched", prompt_id="p-fast")

        w = scheduler.SchedulerThread(self.db)

        def fake_queue():
            # ComfyUI has already pulled the prompt off the queue; it
            # shows up in NEITHER running nor pending.
            return {"queue_running": [], "queue_pending": []}

        def fake_history(prompt_id):
            # /history/<id> returns the nested ComfyUI 1.49+ shape.
            return {
                "status": {"status_str": "success", "completed": True},
                "outputs": {"images": ["out.png"]},
            }

        w.reconcile(history_fetcher=fake_history, queue_fetcher=fake_queue)

        # The live row should be gone (it migrated to job_history).
        self.assertIsNone(self.db.get_job(jid))
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "done")
        self.assertEqual(hist[0]["prompt_id"], "p-fast")
        # And reconcile must NOT have spuriously promoted to running
        # before finalising (no /history would have shown that).
        w.stop()


import os  # noqa: E402


class TestPreDispatchHooks(unittest.TestCase):
    """Regression tests for the frontend-preprocessing emulation.

    The scheduler must mutate stored payloads exactly the way the ComfyUI
    web frontend does when the user clicks Queue Prompt. These tests cover
    the `control_after_generate` contract: each of the four known modes,
    the no-op cases (fixed / missing field), and the field-strip behaviour
    that the frontend always performs after applying a mode.
    """

    def setUp(self):
        # Same scratch-DB pattern used by TestScheduler above.
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    # ---- Pure function: _apply_control_after_generate ------------------

    def test_randomize_replaces_seed(self):
        inputs = {"seed": 42, "control_after_generate": "randomize"}
        scheduler._apply_control_after_generate(inputs)
        self.assertNotEqual(inputs["seed"], 42)
        self.assertNotIn("control_after_generate", inputs)

    def test_randomize_replaces_noise_seed_too(self):
        inputs = {
            "seed": 100,
            "noise_seed": 200,
            "control_after_generate": "randomize",
        }
        scheduler._apply_control_after_generate(inputs)
        self.assertNotEqual(inputs["seed"], 100)
        self.assertNotEqual(inputs["noise_seed"], 200)
        self.assertNotIn("control_after_generate", inputs)

    def test_increment_makes_seed_plus_one(self):
        inputs = {"seed": 42, "control_after_generate": "increment"}
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs["seed"], 43)
        self.assertNotIn("control_after_generate", inputs)

    def test_increment_makes_noise_seed_plus_one(self):
        inputs = {
            "noise_seed": 1_000_000,
            "control_after_generate": "increment",
        }
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs["noise_seed"], 1_000_001)
        self.assertNotIn("control_after_generate", inputs)

    def test_decrement_makes_seed_minus_one(self):
        inputs = {"seed": 42, "control_after_generate": "decrement"}
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs["seed"], 41)
        self.assertNotIn("control_after_generate", inputs)

    def test_fixed_leaves_seed_unchanged(self):
        inputs = {"seed": 42, "control_after_generate": "fixed"}
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs["seed"], 42)
        # Even when fixed, the frontend still strips the directive so
        # subsequent round-trips through graphToPrompt don't keep
        # re-adding it.
        self.assertNotIn("control_after_generate", inputs)

    def test_no_cag_randomizes_seed(self):
        # Regression: without a control_after_generate directive, the hook
        # now defaults to randomize so ComfyUI's execution cache cannot
        # silently reuse previous outputs on repeat dispatches. The user
        # sees a fresh draw each run; the directive is *not* stored in the
        # input dict, so we don't add it.
        inputs = {"seed": 42}
        scheduler._apply_control_after_generate(inputs)
        self.assertNotEqual(inputs["seed"], 42)
        self.assertNotIn("control_after_generate", inputs)
        # Stays in the seed widget's default min/max range.
        self.assertGreaterEqual(inputs["seed"], 0)
        self.assertLessEqual(inputs["seed"], 0xFFFFFFFFFFFFFFFF)

    def test_no_cag_randomizes_noise_seed(self):
        inputs = {"noise_seed": 999}
        scheduler._apply_control_after_generate(inputs)
        self.assertNotEqual(inputs["noise_seed"], 999)
        self.assertNotIn("control_after_generate", inputs)

    def test_no_seed_no_directive_is_untouched(self):
        # A node with neither seed nor directive has nothing for the hook
        # to do; leave the inputs alone.
        inputs = {"steps": 20, "model": ["1", 0]}
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs, {"steps": 20, "model": ["1", 0]})

    def test_missing_seed_with_control_field_is_safe(self):
        # A node that only carries the directive but no seed: just strip
        # the directive, don't crash.
        inputs = {"control_after_generate": "randomize"}
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs, {})

    def test_unknown_mode_warns_and_does_not_mutate_seed(self):
        inputs = {"seed": 42, "control_after_generate": "teleport"}
        with self.assertLogs(scheduler.log, level="WARNING"):
            scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs["seed"], 42)
        # We still strip the unknown directive; better than letting it
        # leak through and confuse ComfyUI's node validation.
        self.assertNotIn("control_after_generate", inputs)

    def test_non_string_mode_falls_through_to_randomize(self):
        # Defensive: a malformed directive (None / bool / int / list) is
        # not a recognized mode, so the hook falls through to the no-cag
        # fallback policy: if a seed field is present we randomize it.
        # Rationale: if the directive isn't a valid string, we can't trust
        # it — randomizing is always safer than risking cache reuse.
        for bogus in (None, True, 0, ["fixed"]):
            inputs = {"seed": 42, "control_after_generate": bogus}
            scheduler._apply_control_after_generate(inputs)
            self.assertNotEqual(inputs["seed"], 42, msg=f"bogus={bogus!r}")

    def test_non_numeric_seed_is_left_alone(self):
        # Don't crash if a user-managed seed is still a string from a
        # botched import; let ComfyUI surface the error at execution.
        inputs = {"seed": "not-a-number", "control_after_generate": "increment"}
        scheduler._apply_control_after_generate(inputs)
        self.assertEqual(inputs["seed"], "not-a-number")

    def test_randomize_stays_in_seed_range(self):
        # The seed widget's default min/max is 0 .. 2**64 - 1.
        inputs = {"seed": 0, "control_after_generate": "randomize"}
        for _ in range(50):
            scheduler._apply_control_after_generate(inputs)
            self.assertGreaterEqual(inputs["seed"], 0)
            self.assertLessEqual(inputs["seed"], 0xFFFFFFFFFFFFFFFF)
            # restore for next iteration
            inputs["seed"] = 0

    # ---- Pure function: _apply_pre_dispatch_hooks -----------------------

    def test_hooks_walk_every_node(self):
        prompt = {
            "1": {
                "class_type": "KSampler",
                "inputs": {"seed": 10, "control_after_generate": "increment"},
            },
            "2": {
                "class_type": "KSampler",
                "inputs": {"noise_seed": 20, "control_after_generate": "increment"},
            },
        }
        out = scheduler._apply_pre_dispatch_hooks(prompt)
        self.assertEqual(out["1"]["inputs"]["seed"], 11)
        self.assertEqual(out["2"]["inputs"]["noise_seed"], 21)
        self.assertNotIn("control_after_generate", out["1"]["inputs"])
        self.assertNotIn("control_after_generate", out["2"]["inputs"])

    def test_hooks_do_not_mutate_caller_payload(self):
        # We must deepcopy; otherwise a later run would see the
        # already-mutated seed and behave non-idempotently.
        prompt = {
            "1": {
                "class_type": "KSampler",
                "inputs": {"seed": 10, "control_after_generate": "increment"},
            }
        }
        original_seed = prompt["1"]["inputs"]["seed"]
        scheduler._apply_pre_dispatch_hooks(prompt)
        self.assertEqual(prompt["1"]["inputs"]["seed"], original_seed)
        self.assertEqual(prompt["1"]["inputs"]["control_after_generate"], "increment")

    def test_hooks_skip_nodes_without_inputs(self):
        prompt = {
            "1": {"class_type": "CheckpointLoaderSimple"},
            "2": {
                "class_type": "KSampler",
                "inputs": {"seed": 10, "control_after_generate": "increment"},
            },
        }
        out = scheduler._apply_pre_dispatch_hooks(prompt)
        self.assertEqual(out["2"]["inputs"]["seed"], 11)

    def test_hooks_returns_input_for_non_dict(self):
        # Defensive: a corrupt DB row should not blow up the scheduler
        # thread; just pass it through to ComfyUI's own validation.
        self.assertIsNone(scheduler._apply_pre_dispatch_hooks(None))
        self.assertEqual(scheduler._apply_pre_dispatch_hooks("garbage"), "garbage")
        self.assertEqual(scheduler._apply_pre_dispatch_hooks(123), 123)

    # ---- Integration: tick() actually applies the hooks ---------------

    def test_tick_sends_mutated_payload(self):
        """The full tick() path must POST a payload whose seed has been
        mutated and whose control_after_generate field has been stripped."""
        self.db.set_state("paused", "0")
        # Realistic KSampler-shaped payload
        payload = {
            "1": {
                "class_type": "KSampler",
                "inputs": {
                    "seed": 42,
                    "noise_seed": 99,
                    "control_after_generate": "increment",
                    "steps": 20,
                },
            },
        }
        self.db.add_job(payload=payload, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake/")

        captured = {}
        fake_resp = _FakeResponse(200, json.dumps({"prompt_id": "p-hooks"}))

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return fake_resp

        with patch.object(scheduler.urllib.request, "urlopen", side_effect=fake_urlopen):
            w.tick()

        sent_prompt = captured["body"]["prompt"]
        sent_inputs = sent_prompt["1"]["inputs"]
        self.assertEqual(sent_inputs["seed"], 43)
        self.assertEqual(sent_inputs["noise_seed"], 100)
        self.assertNotIn("control_after_generate", sent_inputs)
        # Unrelated inputs survive untouched
        self.assertEqual(sent_inputs["steps"], 20)

    def test_tick_two_runs_increment_in_a_row(self):
        """Two consecutive dispatches must each apply the increment hook.

        The in-DB payload must stay at ``seed=100`` after every dispatch —
        we mutate a fresh deepcopy each tick and POST it, we never write
        back to the database. That guarantees a third, fourth, Nth run
        keeps producing a clean +1 progression instead of jumping ahead
        by however many runs the job has had.
        """
        self.db.set_state("paused", "0")
        payload = {
            "1": {
                "class_type": "KSampler",
                "inputs": {"seed": 100, "control_after_generate": "increment"},
            }
        }
        jid = self.db.add_job(payload=payload, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake/")

        seen_seeds = []

        def fake_urlopen(req, timeout=None):
            seen_seeds.append(json.loads(req.data.decode())["prompt"]["1"]["inputs"]["seed"])
            return _FakeResponse(200, json.dumps({"prompt_id": "p"}))

        with patch.object(scheduler.urllib.request, "urlopen", side_effect=fake_urlopen):
            w.tick()
            # Re-arm the same job so the scheduler will pick it up again
            # (after tick() it moved to status='running').
            self.db.update_job(jid, status="scheduled", scheduled_at=0.0)
            w.tick()

        # Each dispatch incremented from the stored 100 -> 101.
        self.assertEqual(
            seen_seeds, [101, 101],
            "increment must apply on each dispatch from the immutable DB "
            "payload, not advance monotonically across runs",
        )

        # The DB-stored payload must NOT have been mutated. Otherwise the
        # third run would skip 101 (the value already burned into the
        # stored payload) and jump straight to 102 — silently dropping
        # a frame.
        stored_row = self.db.get_job(jid)
        self.assertIsNotNone(stored_row, "job should still be present")
        stored = json.loads(stored_row["payload"])  # type: ignore[index]
        self.assertEqual(stored["1"]["inputs"]["seed"], 100)
        self.assertEqual(stored["1"]["inputs"]["control_after_generate"], "increment")

    def test_api_format_payload_no_cag_now_randomizes(self):
        """End-to-end: an API-format payload without ``control_after_generate``
        is randomized by the hook before it reaches ComfyUI.

        This is the bug-fix regression: before the fix, a stored payload
        missing the directive would ship with its original seed and hit
        ComfyUI's execution cache, producing "instant" runs that reused
        previous outputs. After the fix, the hook must produce a fresh
        seed so the cache key changes and ComfyUI actually re-executes.
        """
        self.db.set_state("paused", "0")
        # Mirrors the failing snippet from the bug report: an API-format
        # KSampler with seed/noise_seed and no control_after_generate.
        payload = {
            "3": {
                "class_type": "KSampler",
                "inputs": {"seed": 100, "noise_seed": 200},
            }
        }
        self.db.add_job(payload=payload, scheduled_at=0.0)
        w = scheduler.SchedulerThread(self.db, comfyui_url="http://fake/")

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["body"] = json.loads(req.data.decode())
            return _FakeResponse(200, json.dumps({"prompt_id": "p-nocag"}))

        with patch.object(scheduler.urllib.request, "urlopen", side_effect=fake_urlopen):
            w.tick()

        sent_inputs = captured["body"]["prompt"]["3"]["inputs"]
        # The seed/noise_seed must have been randomized away from the
        # stored values — otherwise ComfyUI would cache-hit and return
        # the previous outputs instead of running.
        self.assertNotEqual(sent_inputs["seed"], 100)
        self.assertNotEqual(sent_inputs["noise_seed"], 200)
        # And the stored payload itself stays at 100/200 — the scheduler
        # mutates a deepcopy on each dispatch, never writes back.
        # Post-v0.3.x state machine: after POST success the job is
        # 'dispatched', not 'running'. Reconcile is responsible for the
        # promotion to 'running' once ComfyUI's /queue reports the
        # prompt as the currently-executing item.
        jobs = self.db.list_jobs(["dispatched"], 1)
        self.assertEqual(len(jobs), 1)
        stored_row = self.db.get_job(jobs[0]["id"])
        self.assertIsNotNone(stored_row)
        assert stored_row is not None
        stored = json.loads(stored_row["payload"])  # type: ignore[index]
        self.assertEqual(stored["3"]["inputs"]["seed"], 100)
        self.assertEqual(stored["3"]["inputs"]["noise_seed"], 200)


    # ------------------------------------------------------------------
    # Self-adaptive polling + lazy reconcile + 5xx backoff (added 2026-08-23)
    # ------------------------------------------------------------------

    def test_smart_polling_increases_interval_when_idle(self):
        """When the queue is empty, ``_compute_next_interval`` should pick
        the long IDLE interval, not the 1s poll-interval the old code
        hard-coded.

        With a dispatched (queued in ComfyUI but not yet running) job we
        should poll on the QUEUED cadence (3s); once ComfyUI marks it
        running we drop to RUNNING (2s); after it finishes we go back to
        IDLE (5s).
        """
        self.db.set_state("paused", "0")
        w = scheduler.SchedulerThread(self.db)

        # 1. Idle start: nothing due, nothing in flight -> IDLE_INTERVAL.
        self.assertEqual(w._compute_next_interval(False),
                         scheduler.SchedulerThread.IDLE_INTERVAL)

        # 2. A due job was just dispatched -> DISPATCH_INTERVAL wins,
        #    even if there are no other in-flight jobs.
        self.assertEqual(w._compute_next_interval(True),
                         scheduler.SchedulerThread.DISPATCH_INTERVAL)

        # 3. A `dispatched` job waiting in ComfyUI -> QUEUED_INTERVAL.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="dispatched", prompt_id="p-q")
        self.assertEqual(w._compute_next_interval(False),
                         scheduler.SchedulerThread.QUEUED_INTERVAL)

        # 4. Same job now `running` -> RUNNING_INTERVAL (faster than
        #    QUEUED because ComfyUI could finish any second).
        self.db.update_job(jid, status="running", prompt_id="p-q")
        self.assertEqual(w._compute_next_interval(False),
                         scheduler.SchedulerThread.RUNNING_INTERVAL)

        # 5. Job finishes -> interval snaps back to IDLE. We simulate
        #    "finished" by removing the row (mark_done does that).
        self.db.mark_done(jid, "p-q", {})
        self.assertEqual(w._compute_next_interval(False),
                         scheduler.SchedulerThread.IDLE_INTERVAL)

    def test_lazy_reconcile_skips_when_no_dispatched(self):
        """``_has_in_flight_jobs`` should report False when there are no
        ``dispatched`` or ``running`` rows, and ``reconcile`` should
        therefore not be called from ``_run`` when there's nothing in
        flight.

        We don't spin up the real background thread; we drive ``_run``
        exactly one iteration by patching ``_stop.wait`` to flip the
        stop event as a side-effect (so the loop exits after the first
        pass without sleeping), and assert that the /history endpoint
        was NEVER hit.
        """
        self.db.set_state("paused", "0")
        w = scheduler.SchedulerThread(self.db)

        history_calls = []

        def fake_urlopen(req, *args, **kwargs):
            history_calls.append(req.full_url)
            # Should never be reached when the queue is empty.
            return _FakeResponse(200, json.dumps({}))

        # Exit the loop on the very first _stop.wait() call.
        def fake_wait(_interval):
            w._stop.set()
            return True

        with patch.object(scheduler.urllib.request, "urlopen",
                          side_effect=fake_urlopen), \
             patch.object(w._stop, "wait", side_effect=fake_wait):
            w._run()

        # No history calls because there were no in-flight jobs.
        self.assertEqual(history_calls, [],
                         "reconcile() must NOT call /history when there "
                         "are no dispatched/running jobs")

        # Sanity: _has_in_flight_jobs agrees.
        self.assertFalse(w._has_in_flight_jobs())

        # And: even after we ADD a running job, the probe flips True.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        self.db.update_job(jid, status="running", prompt_id="p-lazy")
        self.assertTrue(w._has_in_flight_jobs())

    def test_backoff_on_comfyui_error(self):
        """Consecutive 5xx responses must (a) bump the streak counter and
        (b) inflate the next-sleep interval by doubling each time, capped
        at BACKOFF_MAX. A single 2xx in between resets the streak.
        """
        self.db.set_state("paused", "0")
        w = scheduler.SchedulerThread(self.db)

        # Baseline: no streak, no in-flight jobs -> IDLE.
        self.assertEqual(w._consecutive_5xx, 0)
        self.assertEqual(w._compute_next_interval(False),
                         scheduler.SchedulerThread.IDLE_INTERVAL)

        # 1st 5xx: streak=1, interval doubles to IDLE*2 = 10s.
        w._record_comfyui_5xx("test")
        self.assertEqual(w._consecutive_5xx, 1)
        interval_1 = w._compute_next_interval(False)
        self.assertGreaterEqual(interval_1, 2 * scheduler.SchedulerThread.IDLE_INTERVAL)

        # 2nd 5xx: streak=2, interval quadruples to IDLE*4 = 20s.
        w._record_comfyui_5xx("test")
        self.assertEqual(w._consecutive_5xx, 2)
        interval_2 = w._compute_next_interval(False)
        self.assertGreaterEqual(interval_2, 4 * scheduler.SchedulerThread.IDLE_INTERVAL)

        # Many more 5xx -> streak keeps growing but interval caps at 30s.
        for _ in range(10):
            w._record_comfyui_5xx("test")
        self.assertGreaterEqual(w._consecutive_5xx, 5)
        capped = w._compute_next_interval(False)
        self.assertLessEqual(capped, scheduler.SchedulerThread.BACKOFF_MAX)
        self.assertAlmostEqual(capped,
                               scheduler.SchedulerThread.BACKOFF_MAX,
                               places=5)

        # A success resets the streak and brings us back to IDLE.
        w._record_comfyui_success()
        self.assertEqual(w._consecutive_5xx, 0)
        self.assertEqual(w._compute_next_interval(False),
                         scheduler.SchedulerThread.IDLE_INTERVAL)

        # End-to-end: a 5xx in the POST path actually bumps the streak.
        jid = self.db.add_job(payload={"x": 1}, scheduled_at=0.0)
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            u.return_value = _FakeResponse(503, "unavailable")
            w.tick()
        self.assertEqual(w._consecutive_5xx, 1,
                         "tick POST 5xx must bump the backoff counter")
        # The job itself got rescheduled (retry ladder), but the
        # scheduler's streak is independent of that.
        row = self.db.get_job(jid)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "scheduled")

        # ...and a subsequent 200 clears it.
        with patch.object(scheduler.urllib.request, "urlopen") as u:
            u.return_value = _FakeResponse(200,
                                          json.dumps({"prompt_id": "p-backoff"}))
            self.db.update_job(jid, status="scheduled", scheduled_at=0.0)
            w.tick()
        self.assertEqual(w._consecutive_5xx, 0,
                         "tick POST 2xx must clear the backoff counter")


if __name__ == "__main__":
    unittest.main(verbosity=2)
