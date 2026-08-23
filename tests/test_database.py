"""ScheduledQueueDB tests covering the state machine, ordering and cancellation
rules. Pure stdlib + unittest; no third-party deps.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

# Make the standalone src/ tree importable without pip-installing.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from comfyui_scheduled_queue import database  # noqa: E402


def _fresh_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    return database.ScheduledQueueDB(db_path=tmp.name), tmp.name


class TestBasics(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_add_returns_uuid_and_default_status(self):
        jid = self.db.add_job(payload={"a": 1}, scheduled_at=1_000_000.0, priority=100, note="x")
        self.assertTrue(jid)
        row = self.db.get_job(jid)
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["note"], "x")
        self.assertEqual(row["priority"], 100)

    def test_add_assigns_increasing_queue_order(self):
        a = self.db.add_job(payload={}, scheduled_at=10.0)
        b = self.db.add_job(payload={}, scheduled_at=10.0)
        c = self.db.add_job(payload={}, scheduled_at=10.0)
        ra = self.db.get_job(a)["queue_order"]
        rb = self.db.get_job(b)["queue_order"]
        rc = self.db.get_job(c)["queue_order"]
        self.assertLess(ra, rb)
        self.assertLess(rb, rc)

    def test_paused_defaults_to_one(self):
        # Per spec: paused=1 on first boot so the scheduler does NOT dispatch
        # anything before the operator explicitly resumes.
        self.assertEqual(self.db.get_state("paused"), "1")


class TestOrdering(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _add(self, **kw):
        kw.setdefault("scheduled_at", 1_000_000.0)
        kw.setdefault("payload", {})
        return self.db.add_job(**kw)

    def test_reorder_up_swaps_with_previous_neighbour(self):
        a = self._add()
        b = self._add()
        c = self._add()
        # Currently order: a < b < c
        self.assertEqual(self.db.get_job(a)["queue_order"], 1000)
        self.assertEqual(self.db.get_job(b)["queue_order"], 2000)
        self.assertEqual(self.db.get_job(c)["queue_order"], 3000)

        self.db.reorder_job(c, -1)
        self.assertEqual(self.db.get_job(c)["queue_order"], 2000)
        self.assertEqual(self.db.get_job(b)["queue_order"], 3000)

    def test_reorder_down_swaps_with_next_neighbour(self):
        a = self._add()
        b = self._add()
        c = self._add()
        self.db.reorder_job(a, 1)
        self.assertEqual(self.db.get_job(a)["queue_order"], 2000)
        self.assertEqual(self.db.get_job(b)["queue_order"], 1000)

    def test_reorder_first_up_is_noop(self):
        a = self._add()
        b = self._add()
        before_a = self.db.get_job(a)["queue_order"]
        before_b = self.db.get_job(b)["queue_order"]
        self.assertFalse(self.db.reorder_job(a, -1))
        self.assertEqual(self.db.get_job(a)["queue_order"], before_a)
        self.assertEqual(self.db.get_job(b)["queue_order"], before_b)

    def test_reorder_invalid_direction_raises(self):
        a = self._add()
        self.assertRaises(ValueError, self.db.reorder_job, a, 0)
        self.assertRaises(ValueError, self.db.reorder_job, a, 2)

    def test_claim_returns_earliest_due_job(self):
        a = self._add(scheduled_at=1000.0)
        b = self._add(scheduled_at=2000.0)
        # Advance time so both are due; b has higher queue_order so a wins.
        claimed = self.db.claim_next_due_job.__self__  # noqa
        # Use a real time in the past:
        self.db.update_job(a, scheduled_at=0.0)
        self.db.update_job(b, scheduled_at=0.0)
        job = self.db.claim_next_due_job()
        self.assertIsNotNone(job)
        self.assertEqual(job["id"], a)
        self.assertEqual(job["status"], "dispatched")

    def test_claim_returns_none_when_paused_or_due_list_empty(self):
        self._add(scheduled_at=0.0)
        # First call: succeeds (paused gate is in scheduler, not here).
        job = self.db.claim_next_due_job()
        self.assertIsNotNone(job)
        self.assertIsNone(self.db.claim_next_due_job())


class TestCancellation(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_cancel_pending_job(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.assertTrue(self.db.cancel_job(jid))
        self.assertEqual(self.db.get_job(jid)["status"], "cancelled")

    def test_cancel_running_is_refused(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.claim_next_due_job()
        self.db.update_job(jid, status="running")
        self.assertFalse(self.db.cancel_job(jid))
        self.assertEqual(self.db.get_job(jid)["status"], "running")

    def test_cancel_unknown_job(self):
        self.assertFalse(self.db.cancel_job("does-not-exist"))


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_recover_orphans_sets_interrupted(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.update_job(jid, status="running")
        n = self.db.recover_orphans()
        self.assertEqual(n, 1)
        self.assertEqual(self.db.get_job(jid)["status"], "interrupted")

    def test_history_records_done_and_failed(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.update_job(jid, prompt_id="p-1")
        self.db.mark_done(jid, "p-1", outputs={"images": ["a.png"]})
        self.assertIsNone(self.db.get_job(jid))
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["status"], "done")
        self.assertIn("images", hist[0]["outputs"])

    def test_reset_all_interrupted_marks_them_due(self):
        jid_a = self.db.add_job(payload={}, scheduled_at=10.0)
        jid_b = self.db.add_job(payload={}, scheduled_at=10.0)
        self.db.update_job(jid_a, status="interrupted")
        self.db.update_job(jid_b, status="interrupted")
        n = self.db.reset_all_interrupted()
        self.assertEqual(n, 2)
        for jid in (jid_a, jid_b):
            self.assertEqual(self.db.get_job(jid)["status"], "scheduled")

    def test_running_and_history_expose_real_started_at_and_duration(self):
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(jid, "p-timed")
        self.db.mark_running(jid, "p-timed")
        running = self.db.get_job(jid)
        self.assertEqual(running["status"], "running")
        self.assertIsNotNone(running["started_at"])
        self.assertIsNotNone(running["duration"])
        self.assertGreaterEqual(running["duration"], 0)
        self.db.mark_done(jid, "p-timed", outputs={})
        history = self.db.list_history()[0]
        self.assertIsNotNone(history["started_at"])
        self.assertIsNotNone(history["duration"])
        self.assertGreaterEqual(history["duration"], 0)

    def test_pause_reclaims_dispatched_but_not_running(self):
        dispatched = self.db.add_job(payload={}, scheduled_at=1.0)
        running = self.db.add_job(payload={}, scheduled_at=1.0)
        self.db.mark_dispatched(dispatched, "p-dispatched")
        self.db.mark_dispatched(running, "p-running")
        self.db.mark_running(running, "p-running")
        self.assertEqual(self.db.reclaim_dispatched(), 1)
        reclaimed = self.db.get_job(dispatched)
        self.assertEqual(reclaimed["status"], "scheduled")
        self.assertIsNone(reclaimed["prompt_id"])
        self.assertIsNone(reclaimed["dispatched_at"])
        self.assertEqual(self.db.get_job(running)["status"], "running")


class TestUpdateGuards(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_update_payload_rejected(self):
        jid = self.db.add_job(payload={"a": 1}, scheduled_at=10.0)
        self.assertRaises(ValueError, self.db.update_job, jid, payload={"b": 2})

    def test_update_unknown_field_rejected(self):
        jid = self.db.add_job(payload={}, scheduled_at=10.0)
        self.assertRaises(ValueError, self.db.update_job, jid, garbage=1)


# ---------------------------------------------------------------------------
# Stage 3 (v0.3.8 backend flush): pagination/count/clear/repeat helpers.
# ---------------------------------------------------------------------------

class TestStage3Pagination(unittest.TestCase):
    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_count_jobs_multiple_statuses(self):
        for i in range(4):
            self.db.add_job(payload={"i": i}, scheduled_at=10.0 + i)
        c1 = self.db.add_job(payload={}, scheduled_at=20.0)
        self.db.update_job(c1, status="cancelled")
        # a done row lives in job_history, not scheduled_jobs
        c2 = self.db.add_job(payload={}, scheduled_at=30.0)
        self.db.update_job(c2, status="running", prompt_id="p")
        self.db.mark_done(c2, prompt_id="p", outputs={"x": 1})

        # 4 scheduled + 1 cancelled = 5 across the live table
        self.assertEqual(self.db.count_jobs(["scheduled"]), 4)
        self.assertEqual(self.db.count_jobs(["cancelled"]), 1)
        # 1 done in history
        self.assertEqual(self.db.count_jobs(["done"]), 1)
        # cross-store sum
        self.assertEqual(
            self.db.count_jobs(["scheduled", "cancelled", "done"]), 6,
        )
        # empty filter -> total of both tables
        self.assertEqual(self.db.count_jobs([]), 6)
        self.assertEqual(self.db.count_jobs(None), 6)

    def test_list_jobs_paginated_statuses_none_returns_all(self):
        # Regression: when statuses is None (or empty), the paginated helper
        # used to drop the history table entirely because the `elif not
        # statuses` branch only queried scheduled_jobs. Now both stores are
        # returned (live rows first, finished rows after).
        for i in range(3):
            self.db.add_job(payload={"live": i}, scheduled_at=10.0 + i)
        # Two finished jobs that should land in job_history.
        h1 = self.db.add_job(payload={"h": 1}, scheduled_at=99.0)
        self.db.update_job(h1, status="running", prompt_id="p1")
        self.db.mark_done(h1, prompt_id="p1", outputs={"x": 1})
        h2 = self.db.add_job(payload={"h": 2}, scheduled_at=99.5)
        self.db.update_job(h2, status="running", prompt_id="p2")
        self.db.mark_failed(h2, error="boom")

        # statuses=None (the default).
        rows_none = self.db.list_jobs_paginated(statuses=None, limit=50, offset=0)
        # statuses=() explicitly.
        rows_empty = self.db.list_jobs_paginated(statuses=(), limit=50, offset=0)

        for rows, label in ((rows_none, "None"), (rows_empty, "()")):
            self.assertEqual(
                len(rows), 5,
                f"statuses={label} should return 3 live + 2 history rows",
            )
            statuses = {r["status"] for r in rows}
            # Live rows present.
            self.assertIn("scheduled", statuses)
            # History rows present — this is the regression guard.
            self.assertIn("done", statuses, f"statuses={label} dropped history 'done'")
            self.assertIn("failed", statuses, f"statuses={label} dropped history 'failed'")
            # Total count must agree.
            self.assertEqual(len(rows), self.db.count_jobs(statuses=None))

    def test_list_jobs_paginated_statuses_done_returns_history_only(self):
        # statuses=("done") maps only to hist (not in _STATUS_IN_SCHEDULED).
        # Pre-fix this returned [] because the `if sched:` branch was skipped
        # AND the `elif not statuses:` branch was skipped (statuses was truthy).
        # Post-fix the `if hist:` branch fires.
        live = self.db.add_job(payload={"live": 1}, scheduled_at=10.0)
        done = self.db.add_job(payload={"d": 1}, scheduled_at=20.0)
        self.db.update_job(done, status="running", prompt_id="pd")
        self.db.mark_done(done, prompt_id="pd", outputs={"x": 1})

        rows = self.db.list_jobs_paginated(["done"], limit=50, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], done)
        self.assertEqual(rows[0]["status"], "done")
        # The live job must NOT appear when filtering by history-only status.
        self.assertNotIn(live, {r["id"] for r in rows})
        # And the count must agree.
        self.assertEqual(self.db.count_jobs(["done"]), 1)

    def test_list_jobs_paginated_statuses_scheduled_returns_live_only(self):
        # statuses=("scheduled") maps only to sched. The hist branch should
        # not contribute anything; history rows must not leak in.
        live = self.db.add_job(payload={"l": 1}, scheduled_at=10.0)
        finished = self.db.add_job(payload={"f": 1}, scheduled_at=20.0)
        self.db.update_job(finished, status="running", prompt_id="pf")
        self.db.mark_done(finished, prompt_id="pf", outputs={"x": 1})

        rows = self.db.list_jobs_paginated(["scheduled"], limit=50, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], live)
        self.assertEqual(rows[0]["status"], "scheduled")
        # History row must not appear.
        self.assertNotIn(finished, {r["id"] for r in rows})
        self.assertEqual(self.db.count_jobs(["scheduled"]), 1)

    def test_list_jobs_paginated_statuses_mixed_returns_both(self):
        # statuses=("scheduled", "done") spans both stores. Both branches fire.
        live = self.db.add_job(payload={"l": 1}, scheduled_at=10.0)
        finished = self.db.add_job(payload={"f": 1}, scheduled_at=20.0)
        self.db.update_job(finished, status="running", prompt_id="pf")
        self.db.mark_done(finished, prompt_id="pf", outputs={"x": 1})

        rows = self.db.list_jobs_paginated(["scheduled", "done"], limit=50, offset=0)
        ids = {r["id"] for r in rows}
        self.assertEqual(len(rows), 2)
        self.assertIn(live, ids)
        self.assertIn(finished, ids)
        statuses = {r["status"] for r in rows}
        self.assertEqual(statuses, {"scheduled", "done"})
        # Count agrees.
        self.assertEqual(self.db.count_jobs(["scheduled", "done"]), 2)

    def test_list_jobs_paginated_consistent_with_count(self):
        for i in range(7):
            self.db.add_job(payload={"i": i}, scheduled_at=10.0 + i)
        c1 = self.db.add_job(payload={}, scheduled_at=99.0)
        self.db.update_job(c1, status="cancelled")

        total = self.db.count_jobs(["scheduled"])
        self.assertEqual(total, 7)

        page1 = self.db.list_jobs_paginated(["scheduled"], limit=3, offset=0)
        page2 = self.db.list_jobs_paginated(["scheduled"], limit=3, offset=3)
        page3 = self.db.list_jobs_paginated(["scheduled"], limit=3, offset=6)
        self.assertEqual(len(page1), 3)
        self.assertEqual(len(page2), 3)
        self.assertEqual(len(page3), 1)
        # no overlap across pages
        ids = {r["id"] for r in page1} | {r["id"] for r in page2} | {r["id"] for r in page3}
        self.assertEqual(len(ids), 7)
        # offset past the end -> empty
        self.assertEqual(
            self.db.list_jobs_paginated(["scheduled"], limit=10, offset=999),
            [],
        )

    def test_clear_by_status_removes_matching(self):
        keep = self.db.add_job(payload={"keep": 1}, scheduled_at=1.0)
        for i in range(3):
            jid = self.db.add_job(payload={"c": i}, scheduled_at=2.0 + i)
            self.db.update_job(jid, status="cancelled")
        # one done job in history
        live = self.db.add_job(payload={"will-done": 1}, scheduled_at=99.0)
        self.db.update_job(live, status="running", prompt_id="p")
        self.db.mark_done(live, prompt_id="p", outputs={"x": 1})

        removed = self.db.clear_by_status(["cancelled", "done"])
        self.assertEqual(removed, 4)
        # only the kept row remains
        rows = self.db.list_jobs()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], keep)
        # history wiped
        self.assertEqual(self.db.list_history(), [])

    def test_repeat_job_copies_payload_and_priority_zero(self):
        # name "priority_zero" in the user's spec referred to "the new
        # job's priority is reset to a default value (100)" — pin that.
        src = self.db.add_job(
            payload={"workflow": {"nodes": [1, 2, 3]}, "label": "src"},
            scheduled_at=10.0,
            priority=999,  # wildly different to prove reset
            note="original",
        )
        self.db.update_job(src, status="running", prompt_id="p")
        self.db.mark_done(src, prompt_id="p", outputs={"i": ["a.png"]})

        new_id = self.db.repeat_job(src)
        self.assertIsNotNone(new_id)
        self.assertNotEqual(new_id, src)

        new_row = self.db.get_job(new_id)
        self.assertIsNotNone(new_row)
        assert new_row is not None  # pyright narrowing
        self.assertEqual(new_row["status"], "scheduled")
        # payload preserved verbatim
        self.assertEqual(
            json.loads(new_row["payload"]),
            {"workflow": {"nodes": [1, 2, 3]}, "label": "src"},
        )
        # priority reset to default 100 (NOT the source's 999)
        self.assertEqual(new_row["priority"], 100)
        # note recorded as a derivative of the source id
        self.assertTrue(new_row["note"].startswith("repeat of "))

        # unknown source -> None
        self.assertIsNone(self.db.repeat_job("not-a-real-id"))


# ---------------------------------------------------------------------------
# v0.3.10: workflow_title field tests.
# ---------------------------------------------------------------------------

class TestWorkflowTitle(unittest.TestCase):
    """Cover the optional ``workflow_title`` column added in v0.3.10.

    The field stores the ComfyUI workflow filename that was active when the
    job was queued, so the sidebar can label rows without re-fetching payload.
    Backend behaviour is intentionally minimal: accept a string (or NULL),
    persist it, surface it on read endpoints, propagate through repeat_job,
    and migrate legacy DBs that predate the column.
    """

    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_default_workflow_title_is_none(self):
        jid = self.db.add_job(payload={"a": 1}, scheduled_at=10.0)
        row = self.db.get_job(jid)
        # Either NULL or empty string is acceptable as "no title".
        self.assertFalse(row["workflow_title"], f"expected blank, got {row['workflow_title']!r}")

    def test_workflow_title_round_trips(self):
        jid = self.db.add_job(
            payload={"a": 1}, scheduled_at=10.0,
            workflow_title="SD工作流 无强化",
        )
        row = self.db.get_job(jid)
        self.assertEqual(row["workflow_title"], "SD工作流 无强化")

    def test_empty_workflow_title_normalised_to_null(self):
        # Some savers emit "" to mean "no title" -- treat identically to None.
        jid = self.db.add_job(payload={}, scheduled_at=10.0, workflow_title="")
        row = self.db.get_job(jid)
        self.assertFalse(row["workflow_title"])

    def test_non_string_workflow_title_rejected(self):
        # Defensive: numbers / dicts are never valid titles. Database layer
        # coerces to None when not a string (so add_job never crashes), but
        # the routes layer is the primary gate -- this test just locks the
        # current "coerce to None" behaviour.
        jid = self.db.add_job(payload={}, scheduled_at=10.0, workflow_title=12345)
        row = self.db.get_job(jid)
        self.assertFalse(row["workflow_title"])

    def test_list_jobs_exposes_workflow_title(self):
        a = self.db.add_job(payload={}, scheduled_at=10.0, workflow_title="alpha")
        b = self.db.add_job(payload={}, scheduled_at=20.0, workflow_title="beta")
        c = self.db.add_job(payload={}, scheduled_at=30.0, note="legacy")
        rows = self.db.list_jobs()
        titles = {r["id"]: r.get("workflow_title") for r in rows}
        self.assertEqual(titles[a], "alpha")
        self.assertEqual(titles[b], "beta")
        self.assertFalse(titles[c])

    def test_list_jobs_paginated_exposes_workflow_title(self):
        # Same as above but via the paginated helper used by /list.
        jid = self.db.add_job(payload={}, scheduled_at=10.0, workflow_title="page-test")
        page = self.db.list_jobs_paginated(["scheduled"], limit=10, offset=0)
        self.assertEqual(len(page), 1)
        self.assertEqual(page[0]["workflow_title"], "page-test")

    def test_get_job_with_outputs_live_exposes_workflow_title(self):
        jid = self.db.add_job(payload={"k": "v"}, scheduled_at=10.0,
                              workflow_title="live-title")
        row = self.db.get_job_with_outputs(jid)
        self.assertIsNotNone(row)
        assert row is not None  # type narrowing
        self.assertEqual(row["workflow_title"], "live-title")
        self.assertEqual(row["payload"], {"k": "v"})

    def test_workflow_title_propagates_to_history_on_mark_done(self):
        # When a live job finishes, its workflow_title must move to
        # job_history so the sidebar can still label it.
        jid = self.db.add_job(payload={"k": "v"}, scheduled_at=10.0,
                              workflow_title="to-be-done")
        self.db.update_job(jid, status="running", prompt_id="p-1")
        self.db.mark_done(jid, prompt_id="p-1", outputs={"images": ["a.png"]})
        # live row is gone
        self.assertIsNone(self.db.get_job(jid))
        # history row carries the title
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["workflow_title"], "to-be-done")

    def test_workflow_title_propagates_to_history_on_mark_failed(self):
        jid = self.db.add_job(payload={}, scheduled_at=10.0,
                              workflow_title="will-fail")
        self.db.update_job(jid, status="running", prompt_id="p-2")
        self.db.mark_failed(jid, error="boom")
        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["workflow_title"], "will-fail")

    def test_workflow_title_propagates_through_repeat_from_history(self):
        # The common case: re-run a finished job. workflow_title must follow.
        src = self.db.add_job(payload={"x": 1}, scheduled_at=10.0,
                              workflow_title="My Workflow")
        self.db.update_job(src, status="running", prompt_id="p-r")
        self.db.mark_done(src, prompt_id="p-r", outputs={"i": ["o.png"]})

        new_id = self.db.repeat_job(src)
        self.assertIsNotNone(new_id)
        assert new_id is not None
        new_row = self.db.get_job(new_id)
        self.assertIsNotNone(new_row)
        assert new_row is not None
        self.assertEqual(new_row["workflow_title"], "My Workflow")

    def test_workflow_title_propagates_through_repeat_from_live(self):
        # Edge case: re-running a job that is still scheduled. The live row
        # already has workflow_title, repeat_job must copy it forward.
        src = self.db.add_job(payload={"x": 2}, scheduled_at=10.0,
                              workflow_title="Live Title")
        new_id = self.db.repeat_job(src)
        self.assertIsNotNone(new_id)
        assert new_id is not None
        new_row = self.db.get_job(new_id)
        self.assertIsNotNone(new_row)
        assert new_row is not None
        self.assertEqual(new_row["workflow_title"], "Live Title")

    def test_update_job_accepts_workflow_title(self):
        jid = self.db.add_job(payload={}, scheduled_at=10.0,
                              workflow_title="original")
        # Replace the title via update_job.
        self.db.update_job(jid, workflow_title="renamed")
        self.assertEqual(self.db.get_job(jid)["workflow_title"], "renamed")
        # None clears it.
        self.db.update_job(jid, workflow_title=None)
        self.assertFalse(self.db.get_job(jid)["workflow_title"])
        # Empty string normalises to NULL.
        self.db.update_job(jid, workflow_title="")
        self.assertFalse(self.db.get_job(jid)["workflow_title"])

    def test_update_job_still_rejects_unknown_fields(self):
        # Sanity: adding workflow_title to the whitelist must not weaken
        # the existing unknown-field guard.
        jid = self.db.add_job(payload={}, scheduled_at=10.0)
        with self.assertRaises(ValueError):
            self.db.update_job(jid, payload={"x": 1})
        with self.assertRaises(ValueError):
            self.db.update_job(jid, garbage=1)

    def test_migration_adds_workflow_title_to_legacy_db(self):
        # Simulate a v0.3.9-era DB (no workflow_title column) and verify the
        # ALTER TABLE migration runs cleanly on first connect.
        legacy = database.ScheduledQueueDB(db_path=self.path)
        try:
            # The column shouldn't exist on a fresh legacy DB because we
            # never wrote one. But our CREATE TABLE in __init__ already
            # includes workflow_title in fresh DBs. So instead, simulate a
            # legacy DB by recreating the schema without the column.
            legacy._conn.executescript("""
                DROP TABLE scheduled_jobs;
                CREATE TABLE scheduled_jobs (
                  id TEXT PRIMARY KEY, prompt_id TEXT, payload TEXT NOT NULL,
                  client_id TEXT, note TEXT, priority INTEGER NOT NULL DEFAULT 100,
                  scheduled_at REAL NOT NULL, created_at REAL NOT NULL,
                  dispatched_at REAL, finished_at REAL, status TEXT NOT NULL DEFAULT 'scheduled',
                  error TEXT, retry_count INTEGER NOT NULL DEFAULT 0,
                  auto_retry INTEGER NOT NULL DEFAULT 0, queue_order INTEGER
                );
            """)
            legacy._conn.commit()
            legacy.close()

            # Re-open with our patched code; migration should ALTER TABLE.
            legacy2 = database.ScheduledQueueDB(db_path=self.path)
            try:
                cols = {r[1] for r in legacy2._conn.execute(
                    "PRAGMA table_info(scheduled_jobs)"
                ).fetchall()}
                self.assertIn("workflow_title", cols)
            finally:
                legacy2.close()
        finally:
            try:
                legacy.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# job_history.dispatched_at + outputs propagation through the paginated list.
# ---------------------------------------------------------------------------

class TestJobHistoryDispatchedAt(unittest.TestCase):
    """Cover v0.3.11: ``dispatched_at`` is copied from the live row into
    ``job_history`` when a job finishes, and the paginated list endpoint
    exposes both ``dispatched_at`` and a decoded ``outputs`` dict so the
    sidebar can show "完成于 HH:MM" and a thumbnail preview for finished
    rows without a per-row detail fetch.
    """

    def setUp(self):
        self.db, self.path = _fresh_db()

    def tearDown(self):
        self.db.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def _add_dispatch(self):
        """Add a job, claim it (so dispatched_at gets stamped), then return
        the (job_id, dispatched_at) tuple from the live row."""
        jid = self.db.add_job(payload={"p": 1}, scheduled_at=1.0)
        claimed = self.db.claim_next_due_job()
        assert claimed is not None
        self.assertEqual(claimed["id"], jid)
        self.assertEqual(claimed["status"], "dispatched")
        return jid, claimed["dispatched_at"]

    def test_finish_copies_dispatched_at(self):
        # When a dispatched job is marked done, the history row must carry
        # the same dispatched_at the live row had — not NULL.
        jid, dispatched_at = self._add_dispatch()
        self.db.update_job(jid, status="running", prompt_id="p-1")
        self.db.mark_done(jid, prompt_id="p-1", outputs={"images": ["a.png"]})

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        h0 = hist[0]
        assert h0 is not None  # pyright narrowing
        # dispatched_at is preserved verbatim.
        self.assertEqual(h0["dispatched_at"], dispatched_at)
        self.assertIsNotNone(h0["dispatched_at"])
        # And finished_at is freshly stamped (later than dispatched_at).
        self.assertGreater(h0["finished_at"], h0["dispatched_at"])

    def test_finish_copies_dispatched_at_for_failed(self):
        # Same propagation rule for mark_failed — the history row keeps
        # dispatched_at so the sidebar can show "failed after dispatch".
        jid, dispatched_at = self._add_dispatch()
        self.db.update_job(jid, status="running", prompt_id="p-2")
        self.db.mark_failed(jid, error="boom")

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        h0 = hist[0]
        assert h0 is not None  # pyright narrowing
        self.assertEqual(h0["status"], "failed")
        self.assertEqual(h0["dispatched_at"], dispatched_at)

    def test_finish_dispatched_at_null_when_never_dispatched(self):
        # If a job finishes without dispatch (shouldn't happen in normal
        # flow, but the schema must allow it), dispatched_at must stay NULL
        # rather than be silently defaulted.
        jid = self.db.add_job(payload={}, scheduled_at=1.0)
        # Cancel directly — never reaches the dispatcher.
        self.db.cancel_job(jid)
        # The cancelled row is in scheduled_jobs, not job_history. Drive it
        # through _finish to exercise the dispatched_at=NULL path.
        # Use mark_failed after the cancel to force the live→history move.
        # mark_failed does DELETE FROM scheduled_jobs; cancel_job already
        # left it there, so this is the path.
        self.db.mark_failed(jid, error="never-dispatched")

        hist = self.db.list_history()
        self.assertEqual(len(hist), 1)
        self.assertIsNone(hist[0]["dispatched_at"])

    def test_list_paginated_history_includes_dispatched_at(self):
        # The paginated helper used by /api/schedule/list must surface
        # dispatched_at on history rows so the sidebar can show it.
        jid, dispatched_at = self._add_dispatch()
        self.db.update_job(jid, status="running", prompt_id="pd")
        self.db.mark_done(jid, prompt_id="pd", outputs={"x": 1})

        rows = self.db.list_jobs_paginated(statuses=["done"], limit=50, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], jid)
        self.assertEqual(rows[0]["dispatched_at"], dispatched_at)
        self.assertEqual(rows[0]["finished_at"], hist_finished_at := rows[0]["finished_at"])
        self.assertGreater(hist_finished_at, dispatched_at)

        # And the unfiltered variant (statuses=None) must also surface it.
        rows_all = self.db.list_jobs_paginated(statuses=None, limit=50, offset=0)
        hist_rows = [r for r in rows_all if r["status"] == "done"]
        self.assertEqual(len(hist_rows), 1)
        self.assertEqual(hist_rows[0]["dispatched_at"], dispatched_at)

    def test_list_paginated_history_includes_outputs(self):
        # The paginated helper must decode the JSON `outputs` column into a
        # Python dict so the sidebar's thumbnail resolver sees a structured
        # value, not a raw string.
        jid, _ = self._add_dispatch()
        self.db.update_job(jid, status="running", prompt_id="po")
        self.db.mark_done(
            jid, prompt_id="po",
            outputs={"images": ["a.png", "b.png"], "text": ["greetings"]},
        )

        rows = self.db.list_jobs_paginated(statuses=["done"], limit=50, offset=0)
        self.assertEqual(len(rows), 1)
        outs = rows[0]["outputs"]
        # Must be a decoded dict, not the raw JSON string.
        self.assertIsInstance(outs, dict)
        self.assertEqual(outs["images"], ["a.png", "b.png"])
        self.assertEqual(outs["text"], ["greetings"])

        # Same guarantee via the unfiltered paginated call.
        rows_all = self.db.list_jobs_paginated(statuses=None, limit=50, offset=0)
        hist_rows = [r for r in rows_all if r["status"] == "done"]
        self.assertEqual(len(hist_rows), 1)
        self.assertIsInstance(hist_rows[0]["outputs"], dict)

    def test_list_paginated_outputs_none_for_failed_without_outputs(self):
        # When mark_failed is called without explicit outputs (the normal
        # failure path), the history row's outputs column is NULL; the
        # paginated list must surface that as Python None, not a string.
        jid, _ = self._add_dispatch()
        self.db.update_job(jid, status="running", prompt_id="pf")
        self.db.mark_failed(jid, error="boom")

        rows = self.db.list_jobs_paginated(statuses=["failed"], limit=50, offset=0)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["outputs"])

    def test_migration_adds_dispatched_at_to_legacy_job_history(self):
        # Simulate a pre-v0.3.11 DB where job_history lacks dispatched_at.
        # We can't just rebuild the schema with raw CREATE TABLE because
        # the fresh CREATE now includes the column, so drop & recreate
        # job_history in the legacy shape, then reopen.
        legacy = database.ScheduledQueueDB(db_path=self.path)
        try:
            legacy._conn.executescript("""
                DROP TABLE job_history;
                CREATE TABLE job_history (
                  id TEXT PRIMARY KEY, prompt_id TEXT, finished_at REAL NOT NULL,
                  status TEXT NOT NULL, outputs TEXT, error TEXT, payload TEXT,
                  workflow_title TEXT
                );
            """)
            legacy._conn.commit()
            legacy.close()

            legacy2 = database.ScheduledQueueDB(db_path=self.path)
            try:
                cols = {r[1] for r in legacy2._conn.execute(
                    "PRAGMA table_info(job_history)"
                ).fetchall()}
                self.assertIn("dispatched_at", cols)
                # And re-running __init__ is idempotent (no second ALTER).
                legacy2.close()
                legacy3 = database.ScheduledQueueDB(db_path=self.path)
                try:
                    cols2 = {r[1] for r in legacy3._conn.execute(
                        "PRAGMA table_info(job_history)"
                    ).fetchall()}
                    self.assertEqual(cols2, cols)
                finally:
                    legacy3.close()
            finally:
                try:
                    legacy2.close()
                except Exception:
                    pass
        finally:
            try:
                legacy.close()
            except Exception:
                pass

    def test_list_paginated_synthesises_dispatched_at_for_legacy_history_row(self):
        # Legacy job_history rows (archived before v0.3.11) carry NULL
        # dispatched_at because the column didn't exist when they were
        # inserted. ``list_jobs_paginated`` must synthesise a value so the
        # sidebar's "完成于 HH:MM:SS · Ns" duration row has something to
        # subtract from. The synthesis uses a conservative default
        # (``_LEGACY_DISPATCHED_AT_ESTIMATE``) and is tagged with
        # ``dispatched_at_estimated=True`` so callers can tell it apart
        # from a real stamp.
        legacy = database.ScheduledQueueDB(db_path=self.path)
        try:
            legacy._conn.executescript("""
                DROP TABLE job_history;
                CREATE TABLE job_history (
                  id TEXT PRIMARY KEY, prompt_id TEXT, finished_at REAL NOT NULL,
                  status TEXT NOT NULL, outputs TEXT, error TEXT, payload TEXT,
                  workflow_title TEXT
                );
                INSERT INTO job_history(id, finished_at, status, payload)
                  VALUES ('old-done', 1_000_000.0, 'done',
                          '{"p": 1}');
                INSERT INTO job_history(id, finished_at, status, payload, error)
                  VALUES ('old-failed', 1_000_010.0, 'failed',
                          '{"p": 2}', 'oops');
            """)
            legacy._conn.commit()
            legacy.close()

            fresh = database.ScheduledQueueDB(db_path=self.path)
            try:
                rows = fresh.list_jobs_paginated(
                    statuses=["done", "failed"], limit=50, offset=0,
                )
                self.assertEqual(len(rows), 2)
                by_id = {r["id"]: r for r in rows}

                # dispatched_at is back-filled from finished_at minus the
                # estimate; the marker flag is set so callers can distinguish
                # synthetic vs. real values.
                done_row = by_id["old-done"]
                # The synthesis produces a concrete timestamp equal to
                # finished_at - _LEGACY_DISPATCHED_AT_ESTIMATE (pinned in a
                # sibling test) — not None. We just verify the marker flag
                # here and the invariant dispatched_at < finished_at below.
                self.assertIsNotNone(done_row["dispatched_at"])
                self.assertTrue(done_row["dispatched_at_estimated"])

                # AND the underlying stored row is untouched — the synthesis
                # only affects the in-memory dict the API returns, not the
                # stored row (so a future backfill pass can replace these
                # values without losing the original NULL marker).
                raw = fresh._conn.execute(
                    "SELECT dispatched_at FROM job_history WHERE id=?",
                    ("old-done",),
                ).fetchone()
                self.assertIsNone(raw[0])

                failed_row = by_id["old-failed"]
                self.assertIsNotNone(failed_row["dispatched_at"])
                self.assertTrue(failed_row["dispatched_at_estimated"])
                self.assertEqual(failed_row["error"], "oops")

                # finished_at / status survive intact so the sidebar can
                # still show the absolute time and status badge.
                self.assertEqual(done_row["finished_at"], 1_000_000.0)
                self.assertEqual(done_row["status"], "done")
                self.assertEqual(failed_row["finished_at"], 1_000_010.0)
                self.assertEqual(failed_row["status"], "failed")

                # And the duration computed from the synthetic stamp is
                # positive and matches the constant (we can't subtract
                # directly without knowing finished_at_estimate, so verify
                # the invariant instead: dispatched_at < finished_at).
                self.assertLess(done_row["dispatched_at"], done_row["finished_at"])
                self.assertLess(failed_row["dispatched_at"], failed_row["finished_at"])
            finally:
                fresh.close()
        finally:
            try:
                legacy.close()
            except Exception:
                pass

    def test_list_paginated_does_not_overwrite_real_dispatched_at(self):
        # When the live row had a real dispatched_at, _finish copies it into
        # job_history; the synthesis pass must NOT clobber it or set the
        # ``estimated`` flag. New writes from v0.3.11+ keep their original
        # values exactly.
        jid, dispatched_at = self._add_dispatch()
        self.db.update_job(jid, status="running", prompt_id="reald")
        self.db.mark_done(jid, prompt_id="reald", outputs={"x": 1})

        rows = self.db.list_jobs_paginated(statuses=["done"], limit=50, offset=0)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        assert row is not None
        # Real stamp, untouched.
        self.assertEqual(row["dispatched_at"], dispatched_at)
        # No ``estimated`` flag means callers treat this as authoritative.
        self.assertNotIn("dispatched_at_estimated", row)
        # finished_at is the real one too.
        self.assertGreater(row["finished_at"], dispatched_at)

    def test_list_paginated_synthesis_uses_module_estimate(self):
        # The synthesised value MUST equal finished_at minus the module
        # constant, exactly. If we ever change the constant, this test
        # becomes a single place to update the expected arithmetic.
        legacy = database.ScheduledQueueDB(db_path=self.path)
        try:
            legacy._conn.executescript("""
                DROP TABLE job_history;
                CREATE TABLE job_history (
                  id TEXT PRIMARY KEY, prompt_id TEXT, finished_at REAL NOT NULL,
                  status TEXT NOT NULL, outputs TEXT, error TEXT, payload TEXT,
                  workflow_title TEXT
                );
                INSERT INTO job_history(id, finished_at, status, payload)
                  VALUES ('x', 5_000.0, 'done', '{}');
            """)
            legacy._conn.commit()
            legacy.close()

            fresh = database.ScheduledQueueDB(db_path=self.path)
            try:
                rows = fresh.list_jobs_paginated(
                    statuses=["done"], limit=50, offset=0,
                )
                self.assertEqual(len(rows), 1)
                row = rows[0]
                assert row is not None
                # Pin to the module constant so any change forces a deliberate
                # review of the synthetic dispatch estimate.
                estimate = database._LEGACY_DISPATCHED_AT_ESTIMATE
                self.assertEqual(
                    row["dispatched_at"],
                    max(0.0, row["finished_at"] - estimate),
                )
                # And the marker is present.
                self.assertTrue(row["dispatched_at_estimated"])
            finally:
                fresh.close()
        finally:
            try:
                legacy.close()
            except Exception:
                pass

    def test_list_paginated_no_synthesis_when_finished_at_missing(self):
        # Defensive: if a (hypothetical) legacy row had dispatched_at NULL
        # but finished_at also NULL, the synthesis must NOT try to
        # subtract from None and blow up — it should just leave dispatched_at
        # alone. finished_at is NOT NULL in the schema, so this is a
        # belt-and-braces guard against future schema drift.
        legacy = database.ScheduledQueueDB(db_path=self.path)
        try:
            # SQLite allows NOT NULL columns to be relaxed via table
            # rebuild; force the shape we want to test.
            legacy._conn.executescript("""
                DROP TABLE job_history;
                CREATE TABLE job_history (
                  id TEXT PRIMARY KEY, prompt_id TEXT, finished_at REAL,
                  status TEXT NOT NULL, outputs TEXT, error TEXT, payload TEXT,
                  workflow_title TEXT
                );
                INSERT INTO job_history(id, finished_at, status, payload)
                  VALUES ('nofin', NULL, 'done', '{}');
            """)
            legacy._conn.commit()
            legacy.close()

            fresh = database.ScheduledQueueDB(db_path=self.path)
            try:
                rows = fresh.list_jobs_paginated(
                    statuses=["done"], limit=50, offset=0,
                )
                self.assertEqual(len(rows), 1)
                row = rows[0]
                assert row is not None
                # No synthesis happened.
                self.assertIsNone(row["dispatched_at"])
                self.assertNotIn("dispatched_at_estimated", row)
            finally:
                fresh.close()
        finally:
            try:
                legacy.close()
            except Exception:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
