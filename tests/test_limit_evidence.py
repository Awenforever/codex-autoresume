"""First-hand quota-limit evidence: persistence, effective snapshot, guards."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta

from codex_autoresume.daemon import Daemon, LIMIT_GRACE
from codex_autoresume.detect import InterruptionEvent
from codex_autoresume.planner import plan_for_thread
from codex_autoresume.ratelimits import QuotaSnapshot, Window
from codex_autoresume.state import ThreadState, WatcherState


def _daemon_stub() -> Daemon:
    d = object.__new__(Daemon)
    d.state = WatcherState(
        state_path=os.path.join(tempfile.mkdtemp(prefix="cxr-limit-"),
                                "state.json"))
    d._snapshot = None
    d._snapshot_at = 0.0
    d._state_mtime = 0.0
    return d


def _cfg():
    import copy
    from codex_autoresume.config import DEFAULTS
    return copy.deepcopy(DEFAULTS)


class TestLimitEvidence(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now().astimezone()
        self.d = _daemon_stub()

    def snap(self, p=99, s=40, p_reset=None, s_reset=None):
        return QuotaSnapshot(
            primary=Window(used_percent=p,
                           resets_at=int(p_reset.timestamp()) if p_reset else None),
            secondary=Window(used_percent=s,
                             resets_at=int(s_reset.timestamp()) if s_reset else None))

    def usage_event(self, kind="usage_primary", reset=None):
        return InterruptionEvent(thread_id="t1", kind=kind, reset_at=reset,
                                 source="turn:x")

    def test_usage_event_sets_primary_limit(self):
        reset = self.now + timedelta(minutes=15)
        self.d._note_limit_evidence([self.usage_event("usage_primary", reset)],
                                    self.snap(p=99, p_reset=reset))
        window, until = self.d._active_limit()
        self.assertEqual(window, "primary")
        self.assertEqual(until, reset + LIMIT_GRACE)

    def test_weekly_event_sets_weekly_limit(self):
        reset = self.now + timedelta(days=2)
        self.d._note_limit_evidence([self.usage_event("usage_weekly", reset)],
                                    self.snap(p=20, s=99, s_reset=reset))
        self.assertEqual(self.d._active_limit()[0], "weekly")

    def test_effective_snapshot_reports_empty_without_mutating_raw(self):
        reset = self.now + timedelta(minutes=15)
        raw = self.snap(p=99, p_reset=reset)
        self.d._set_limit("primary", reset)
        eff = self.d._effective_snapshot(raw)
        self.assertEqual(eff.primary.used_percent, 100)
        self.assertEqual(eff.secondary.used_percent, 40)
        self.assertEqual(raw.primary.used_percent, 99)

    def test_effective_snapshot_clears_expired_limit(self):
        self.d.state.limit_until_iso = (
            self.now - timedelta(minutes=1)).isoformat()
        self.d.state.limit_window = "primary"
        raw = self.snap(p=99, p_reset=self.now + timedelta(minutes=15))
        self.assertIs(self.d._effective_snapshot(raw), raw)
        self.assertIsNone(self.d.state.limit_until_iso)
        self.assertIsNone(self.d._active_limit())

    def test_goal_with_paired_reset_sets_limit(self):
        reset = self.now + timedelta(minutes=15)
        t = self.d.state.thread("t1")
        t.tracked = True
        t.last_reset_at = reset.isoformat()
        self.d._note_limit_evidence(
            [InterruptionEvent("t1", "goal_limited", None, "goal:t1",
                               goal_status="usage_limited")],
            self.snap(p=99, p_reset=reset))
        self.assertEqual(self.d._active_limit()[0], "primary")

    def test_goal_corroborated_by_weekly_snapshot(self):
        # unix epochs are whole seconds: strip the microsecond first
        s_reset = self.now.replace(microsecond=0) + timedelta(days=5)
        self.d.state.thread("t1")
        self.d._note_limit_evidence(
            [InterruptionEvent("t1", "goal_limited", None, "goal:t1",
                               goal_status="usage_limited")],
            self.snap(p=10, s=99, s_reset=s_reset))
        self.assertEqual(self.d.state.limit_window, "weekly")
        self.assertEqual(datetime.fromisoformat(self.d.state.limit_until_iso),
                         s_reset + LIMIT_GRACE)

    def test_goal_without_corroboration_sets_nothing(self):
        self.d.state.thread("t1")
        self.d._note_limit_evidence(
            [InterruptionEvent("t1", "goal_limited", None, "goal:t1",
                               goal_status="usage_limited")],
            self.snap(p=8, s=3))
        self.assertIsNone(self.d._active_limit())

    def test_blocked_goal_sets_nothing(self):
        self.d.state.thread("t1")
        self.d._note_limit_evidence(
            [InterruptionEvent("t1", "goal_limited", None, "goal:t1",
                               goal_status="blocked")],
            self.snap(p=99, p_reset=self.now + timedelta(minutes=15)))
        self.assertIsNone(self.d._active_limit())

    def test_restore_from_persisted_thread_evidence(self):
        t = self.d.state.thread("t1")
        t.tracked = True
        t.last_kind = "usage_primary"
        t.last_reset_at = (self.now + timedelta(minutes=10)).isoformat()
        self.d._restore_limit_evidence()
        self.assertEqual(self.d._active_limit()[0], "primary")

    def test_launch_resume_defers_while_evidence_active(self):
        reset = self.now + timedelta(minutes=10)
        self.d._set_limit("primary", reset)
        t = ThreadState(tracked=True)
        self.d._launch_resume("t1", t, None, None)
        self.assertIsNotNone(t.next_wake_iso)
        self.assertEqual(datetime.fromisoformat(t.next_wake_iso),
                         reset + LIMIT_GRACE)

    def test_effective_snapshot_feeds_planner_goal_guard(self):
        # goal row with no reset time of its own: the effective snapshot is
        # what prevents the immediate Continue on a trailing 99%
        reset = self.now + timedelta(minutes=15)
        raw = self.snap(p=99, p_reset=reset, s=40)
        self.d._set_limit("primary", reset)
        eff = self.d._effective_snapshot(raw)
        ev = InterruptionEvent("t1", "goal_limited", None, "goal:t1",
                               goal_status="usage_limited")
        d = plan_for_thread(
            ev, ThreadState(tracked=True, is_goal=True), _cfg(), {},
            thread_last_update_ms=int(self.now.timestamp() * 1000),
            busy=False, now=self.now, snapshot=eff,
            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")


class TestLimitStateRoundtrip(unittest.TestCase):
    def test_limit_fields_persist(self):
        from codex_autoresume.state import load_state, save_state
        path = os.path.join(tempfile.mkdtemp(prefix="cxr-limit-state-"),
                            "state.json")
        st = WatcherState(state_path=path)
        st.limit_until_iso = "2026-10-26T15:38:00+08:00"
        st.limit_window = "primary"
        save_state(st)
        st2 = load_state(path)
        self.assertEqual(st2.limit_until_iso, st.limit_until_iso)
        self.assertEqual(st2.limit_window, "primary")


if __name__ == "__main__":
    unittest.main()
