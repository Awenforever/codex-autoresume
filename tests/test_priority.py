"""Per-conversation priority: 0 = highest; same priority resumes together.

A lower priority may only start while no higher-priority guarded task is
RUNNING (live turn, in-flight resume, or a resume submitted in the same
tick). Waiting on quota never blocks others.
"""
import os
import tempfile
import threading
import unittest

from codex_autoresume.config import load_config
from codex_autoresume.daemon import Daemon
from codex_autoresume.state import ThreadState, WatcherState


class _Gate:
    def __init__(self):
        d = object.__new__(Daemon)
        d.cfg = load_config(None)
        d.state = WatcherState(
            state_path=os.path.join(tempfile.mkdtemp(prefix="cxr-prio-"),
                                    "state.json"))
        d._inflight = {}
        d._inflight_lock = threading.Lock()
        d._update_started = False
        d._resume_pool = None
        d._active_limit = lambda: None
        self.d = d
        self.launched = []
        d._launch_resume = lambda tid, t, dec, info: self._ok(tid)
        d._launch_continue = lambda tid, t, s: self._ok(tid)

    def _ok(self, tid):
        self.launched.append(tid)
        return True

    def thread(self, tid, prio=None, tracked=True):
        t = ThreadState(tracked=tracked, priority=prio)
        self.d.state.threads[tid] = t
        return t

    def job(self, tid, prio, kind=0, stopped=None):
        return (prio, kind, tid, self.d.state.threads[tid], None, None,
                stopped)


class PriorityGateTest(unittest.TestCase):
    def setUp(self):
        self.g = _Gate()

    def test_same_priority_launches_together_lower_waits(self):
        self.g.thread("a", 0)
        self.g.thread("b", 0)
        self.g.thread("c", 1)
        jobs = [self.g.job("c", 1), self.g.job("a", 0), self.g.job("b", 0)]
        self.g.d._launch_jobs(jobs, set())
        self.assertEqual(self.g.launched, ["a", "b"])

    def test_lower_priority_runs_when_nothing_higher_is_active(self):
        self.g.thread("c", 2)
        self.g.thread("d", 2)
        self.g.d._launch_jobs([self.g.job("c", 2), self.g.job("d", 2)], set())
        self.assertEqual(self.g.launched, ["c", "d"])

    def test_higher_priority_live_turn_holds_lower(self):
        self.g.thread("busy", 0)
        self.g.thread("c", 1)
        self.g.d._launch_jobs([self.g.job("c", 1)], {"busy"})
        self.assertEqual(self.g.launched, [])

    def test_higher_priority_inflight_holds_lower(self):
        self.g.thread("a", 0)
        self.g.thread("c", 1)
        self.g.d._inflight["a"] = 1.0
        self.g.d._launch_jobs([self.g.job("c", 1)], set())
        self.assertEqual(self.g.launched, [])

    def test_unguarded_busy_thread_does_not_gate(self):
        self.g.thread("user-chat", 0, tracked=False)
        self.g.thread("c", 1)
        self.g.d._launch_jobs([self.g.job("c", 1)], {"user-chat"})
        self.assertEqual(self.g.launched, ["c"])

    def test_quota_waiting_high_priority_does_not_starve(self):
        # tracked + waiting on quota, but no live turn and nothing in flight
        self.g.thread("waiting", 0)
        self.g.thread("c", 1)
        self.g.d._launch_jobs([self.g.job("c", 1)], set())
        self.assertEqual(self.g.launched, ["c"])

    def test_continued_turn_obeys_the_same_gate(self):
        self.g.thread("a", 0)
        self.g.thread("stop", 1)
        jobs = [self.g.job("a", 0), self.g.job("stop", 1, kind=1,
                                              stopped=object())]
        self.g.d._launch_jobs(jobs, set())
        self.assertEqual(self.g.launched, ["a"])


class PrioritySourceTest(unittest.TestCase):
    def setUp(self):
        self.g = _Gate()

    def test_default_is_zero(self):
        self.g.thread("x")
        self.assertEqual(self.g.d._priority_of("x"), 0)

    def test_config_value_applies(self):
        self.g.d.cfg["threads"]["x"] = {"priority": 3}
        self.g.thread("x")
        self.assertEqual(self.g.d._priority_of("x"), 3)

    def test_state_override_beats_config(self):
        self.g.d.cfg["threads"]["x"] = {"priority": 3}
        self.g.thread("x", prio=1)
        self.assertEqual(self.g.d._priority_of("x"), 1)


class LaunchContinueTest(unittest.TestCase):
    def setUp(self):
        self.g = _Gate()
        # use the real _launch_continue (the gate helper shadows it)
        del self.g.d._launch_continue
        submitted = []
        self.g.d._resume_pool = type(
            "P", (), {"submit": lambda s, f, *a: submitted.append((f, a))})()
        self.submitted = submitted

    def test_launch_continue_submits_rpc_twin(self):
        from codex_autoresume.codexenv import StoppedTurn
        t = self.g.thread("x")
        ok = self.g.d._launch_continue("x", t,
                                       StoppedTurn("x", "turn-1", 123))
        self.assertTrue(ok)
        self.assertEqual(t.continued_turn_id, "turn-1")
        self.assertEqual(t.resume_count_today, 1)
        self.assertEqual(len(self.submitted), 1)
        fn, args = self.submitted[0]
        self.assertEqual(fn, self.g.d._do_continue)
        self.assertEqual(args, ("x", "turn-1"))


if __name__ == "__main__":
    unittest.main()
