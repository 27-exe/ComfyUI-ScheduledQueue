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


if __name__ == "__main__":
    unittest.main(verbosity=2)
