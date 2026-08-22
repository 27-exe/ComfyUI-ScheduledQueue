"""ScheduledQueueDB tests covering the state machine, ordering and cancellation
rules. Pure stdlib + unittest; no third-party deps.
"""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
