"""Capacity ("Selected model is at capacity") retry policy in the daemon."""

import copy
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta

from codex_autoresume.config import DEFAULTS
from codex_autoresume.daemon import Daemon
from codex_autoresume.planner import Decision
from codex_autoresume.resume import ResumeOutcome
from codex_autoresume.state import WatcherState


class _FakePool:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


def _daemon_stub() -> Daemon:
    d = object.__new__(Daemon)
    d.cfg = copy.deepcopy(DEFAULTS)
    d.state = WatcherState(
        state_path=os.path.join(tempfile.mkdtemp(prefix="cxr-cap-"),
                                "state.json"))
    d._snapshot = None
    d._snapshot_at = 0.0
    d._state_mtime = 0.0
    d._inflight = {}
    d._inflight_lock = threading.Lock()
    d._resume_pool = _FakePool()
    return d


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


class TestCapacityFailures(unittest.TestCase):
    def test_overloaded_does_not_park(self):
        d = _daemon_stub()
        t = d.state.thread("t1")
        t.tracked = True
        t.fail_streak = 5
        d._record("t1", ResumeOutcome(
            ok=False, kind="overloaded", exit_code=1,
            stderr_tail="Selected model is at capacity."), "gpt-6.1-sol")
        self.assertEqual(t.fail_streak, 5)          # unchanged, not incremented
        self.assertEqual(t.last_kind, "server_overloaded")
        self.assertEqual(t.overload_model, "gpt-6.1-sol")
        wake = datetime.fromisoformat(t.next_wake_iso)
        self.assertGreater(wake, datetime.now().astimezone())

    def test_usage_limit_still_counts_failures(self):
        d = _daemon_stub()
        t = d.state.thread("t1")
        reset = datetime.now().astimezone() + timedelta(minutes=15)
        d._record("t1", ResumeOutcome(
            ok=False, kind="usage_limit", exit_code=1,
            reset_at_iso=reset.isoformat(),
            stderr_tail="You've hit your usage limit."), None)
        self.assertEqual(t.fail_streak, 1)
        self.assertEqual(t.last_kind, "usage_primary")


class TestCapacityModelRotation(unittest.TestCase):
    def test_rotates_through_fallbacks(self):
        d = _daemon_stub()
        d.cfg["general"]["fallback_models"] = ["m-a", "m-b"]
        t = d.state.thread("t1")
        t.overload_model = "primary-model"
        self.assertEqual(d._capacity_model("t1", t, "primary-model"), "m-a")
        t.overload_model = "m-a"
        self.assertEqual(d._capacity_model("t1", t, "m-a"), "m-b")
        t.overload_model = "m-b"
        # every fallback has hit capacity: alternate rather than retry the
        # model that just failed
        self.assertEqual(d._capacity_model("t1", t, "m-b"), "m-a")

    def test_without_fallbacks_keeps_the_model(self):
        d = _daemon_stub()
        t = d.state.thread("t1")
        self.assertEqual(d._capacity_model("t1", t, "only-model"),
                         "only-model")


class TestCapacityBudgets(unittest.TestCase):
    def test_capacity_retry_bypasses_daily_caps(self):
        d = _daemon_stub()
        today = _today()
        t = d.state.thread("t1")
        t.tracked = True
        t.resume_count_day = today
        t.resume_count_today = 12
        d.state.global_resume_day = today
        d.state.global_resume_count_today = 60
        dec = Decision("due", "t1", "model at capacity; retrying",
                       kind="server_overloaded", prompt="continue")
        d._launch_resume("t1", t, dec, None)
        self.assertEqual(len(d._resume_pool.submitted), 1)
        self.assertEqual(t.total_resumes, 1)
        self.assertEqual(t.resume_count_today, 12)
        self.assertEqual(d.state.global_resume_count_today, 60)

    def test_quota_resume_still_respects_daily_caps(self):
        d = _daemon_stub()
        today = _today()
        t = d.state.thread("t1")
        t.tracked = True
        t.resume_count_day = today
        t.resume_count_today = 12
        dec = Decision("due", "t1", "reset window passed; resuming",
                       kind="goal_limited", prompt="continue")
        d._launch_resume("t1", t, dec, None)
        self.assertEqual(d._resume_pool.submitted, [])
        self.assertEqual(t.total_resumes, 0)


if __name__ == "__main__":
    unittest.main()
