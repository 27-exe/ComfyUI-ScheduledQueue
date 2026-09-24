"""Scheduled pause / resume trigger tests.

The scheduled action must be *identical* to the manual action, so these
tests assert the delegation to ``routes._pause_all_blocking`` (not a
reimplementation) and the one-shot clearing of the rule.
"""
from __future__ import annotations

import datetime as _dt
import sys
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'src'))

from comfyui_scheduled_queue import database, scheduler  # noqa: E402
from comfyui_scheduled_queue import routes  # noqa: E402


def _dtlocal(ts):
    """Format a POSIX timestamp exactly as the UI date picker does."""
    return _dt.datetime.fromtimestamp(ts).strftime('%Y-%m-%dT%H:%M')


class _FakeClock:
    """Replace time.time inside scheduler for deterministic triggers."""

    def __init__(self, now):
        self.now = now

    def __enter__(self):
        self._real = time.time
        time.time = lambda: self.now
        return self

    def __exit__(self, *exc):
        time.time = self._real


class _RecordingRoutes:
    """Stand-in for the routes module so we can prove delegation."""

    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome

    def _pause_all_blocking(self, db, url):
        self.calls.append((db, url))
        if self.outcome:
            raise self.outcome
        # Mirror the one observable side effect the real helper always has,
        # so callers (and these tests) can assert "the pause happened".
        db.set_state('paused', '1')
        return {'paused': True}


def _fresh_db(tmpdir, name):
    return database.ScheduledQueueDB(
        db_path=str(Path(tmpdir) / name)
    )


class _PatchPause:
    """Context manager routing routes._pause_all_blocking to a recorder."""

    def __init__(self, recorder):
        self.recorder = recorder

    def __enter__(self):
        self._saved = routes._pause_all_blocking
        routes._pause_all_blocking = self.recorder._pause_all_blocking
        return self.recorder

    def __exit__(self, *exc):
        routes._pause_all_blocking = self._saved


class ParseWallclockTests(unittest.TestCase):
    def test_minute_precision(self):
        import datetime as dt
        got = scheduler._parse_wallclock('2026-09-25T23:00')
        expected = dt.datetime(2026, 9, 25, 23, 0).timestamp()
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, expected, delta=1.0)

    def test_accepts_seconds(self):
        import datetime as dt
        got = scheduler._parse_wallclock('2026-09-25T23:00:30')
        expected = dt.datetime(2026, 9, 25, 23, 0, 30).timestamp()
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got, expected, delta=1.0)

    def test_accepts_space_separator(self):
        self.assertEqual(
            scheduler._parse_wallclock('2026-09-25T23:00'),
            scheduler._parse_wallclock('2026-09-25 23:00'),
        )

    def test_malformed_returns_none(self):
        for bad in ['', None, 'not-a-date', '2026-13-45T99:99',
                    '2026/09/25T23:00']:
            self.assertIsNone(scheduler._parse_wallclock(bad), bad)

    def test_future_and_past_are_distinguishable(self):
        now = time.time()
        past = scheduler._parse_wallclock(
            time.strftime('%Y-%m-%dT%H:%M', time.localtime(now - 3600))
        )
        future = scheduler._parse_wallclock(
            time.strftime('%Y-%m-%dT%H:%M', time.localtime(now + 3600))
        )
        self.assertIsNotNone(past)
        self.assertIsNotNone(future)
        self.assertLess(past, now)
        self.assertGreater(future, now)


class ScheduledPauseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _fresh_db(self.tmp.name, 'p.db')
        # A brand-new database seeds ``paused='1'`` (database.py:175), which
        # would mask a "did not fire" assertion. Start from a known clean
        # state so each test only observes what its own rule did.
        self.db.set_state('paused', '0')
        self.sched = scheduler.SchedulerThread(
            self.db, comfyui_url='http://127.0.0.1:9999',
        )

    def tearDown(self):
        self.tmp.cleanup()

    def _arm_pause(self, minutes_from_now):
        target = time.time() + minutes_from_now * 60
        self.db.set_pause_at(_dtlocal(target))

    def test_future_rule_does_not_fire(self):
        # 90 minutes, not 30: the UI's minute granularity means a parsed
        # rule can land up to 59s *earlier* than the wall-clock moment the
        # user picked. A 90-minute gap survives that rounding and is still
        # unambiguously in the future.
        self._arm_pause(+90)
        self.sched._check_scheduled_pause_resume()
        self.assertNotEqual(self.db.get_state('paused'), '1')
        # rule stays armed
        self.assertTrue(self.db.get_pause_at())

    def test_past_rule_fires_and_clears(self):
        self._arm_pause(-5)
        with _PatchPause(_RecordingRoutes()) as rec:
            self.sched._check_scheduled_pause_resume()
        self.assertEqual(len(rec.calls), 1)
        self.assertFalse(self.db.get_pause_at())
        self.assertEqual(self.db.get_state('paused'), '1')

    def test_pause_delegates_to_routes_helper(self):
        self._arm_pause(-1)
        rec = _RecordingRoutes()
        with _PatchPause(rec):
            self.sched._check_scheduled_pause_resume()
        self.assertEqual(len(rec.calls), 1)
        db_arg, url_arg = rec.calls[0]
        self.assertIs(db_arg, self.db)
        self.assertEqual(url_arg, 'http://127.0.0.1:9999')

    def test_pause_clears_rule_even_if_pause_raises(self):
        self._arm_pause(-1)
        boom = _RecordingRoutes(outcome=RuntimeError('comfyui down'))
        with _PatchPause(boom):
            self.sched._check_scheduled_pause_resume()
            # second pass must NOT retry -- rule already cleared
            self.sched._check_scheduled_pause_resume()
        self.assertEqual(len(boom.calls), 1)
        self.assertFalse(self.db.get_pause_at())

    def test_second_tick_after_fire_is_noop(self):
        self._arm_pause(-1)
        rec = _RecordingRoutes()
        with _PatchPause(rec):
            self.sched._check_scheduled_pause_resume()
            self.sched._check_scheduled_pause_resume()
        self.assertEqual(len(rec.calls), 1)


class ScheduledResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _fresh_db(self.tmp.name, 'r.db')
        self.db.set_state('paused', '0')
        self.sched = scheduler.SchedulerThread(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_clears_flag_and_clears_rule(self):
        self.db.set_state('paused', '1')
        target = time.time() - 60
        self.db.set_resume_at(_dtlocal(target))

        self.sched._check_scheduled_pause_resume()

        self.assertEqual(self.db.get_state('paused'), '0')
        self.assertFalse(self.db.get_resume_at())

    def test_resume_resets_interrupted_rows(self):
        """Mirror resume_all_handler: interrupted rows go back to
        scheduled so the resumed queue actually drains."""
        self.db.set_state('paused', '1')
        self.db.set_resume_at(_dtlocal(time.time() - 60))
        # Build an interrupted row through the public API.
        job = self.db.add_job(payload='{}', scheduled_at=time.time() - 10)
        self.db.mark_dispatched(job, prompt_id='p-x')
        # Force it into interrupted the way recover_orphans does.
        with self.db._conn:
            self.db._conn.execute(
                "UPDATE scheduled_jobs SET status='interrupted' WHERE id=?",
                (job,),
            )
        self.assertEqual(
            self.db.list_jobs(status_filter=['interrupted'])[0]['status'],
            'interrupted',
        )

        self.sched._check_scheduled_pause_resume()

        rows = self.db.list_jobs(status_filter=['scheduled'])
        self.assertIn(job, [r['id'] for r in rows])

    def test_future_resume_does_not_fire(self):
        target = time.time() + 3600
        self.db.set_resume_at(_dtlocal(target))
        self.db.set_state('paused', '1')
        self.sched._check_scheduled_pause_resume()
        self.assertEqual(self.db.get_state('paused'), '1')
        self.assertTrue(self.db.get_resume_at())

    def test_both_rules_fire_in_one_pass(self):
        """Mis-ordered pair (pause_at later than resume_at, both past):
        resume runs second, so the flag ends up cleared. Documented
        behaviour, not a guess."""
        self.db.set_pause_at(_dtlocal(time.time() - 120))
        self.db.set_resume_at(_dtlocal(time.time() - 60))
        rec = _RecordingRoutes()
        with _PatchPause(rec):
            self.sched._check_scheduled_pause_resume()
        self.assertEqual(len(rec.calls), 1)
        self.assertEqual(self.db.get_state('paused'), '0')
        self.assertFalse(self.db.get_pause_at())
        self.assertFalse(self.db.get_resume_at())


class LoopRobustnessTests(unittest.TestCase):
    """A broken rule must never take the scheduler down."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = _fresh_db(self.tmp.name, 'x.db')
        self.sched = scheduler.SchedulerThread(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def test_malformed_timestamp_is_ignored(self):
        self.db.set_pause_at('total-garbage')
        self.sched._check_scheduled_pause_resume()   # must not raise
        # unparseable value survives: nothing fired, nothing cleared
        self.assertEqual(self.db.get_pause_at(), 'total-garbage')

    def test_db_failure_does_not_raise(self):
        with unittest.mock.patch.object(
            type(self.db), 'get_pause_at',
            side_effect=RuntimeError('db locked'),
        ):
            self.sched._check_scheduled_pause_resume()   # must not raise


if __name__ == '__main__':
    unittest.main()
