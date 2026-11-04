"""A missing codex executable must not burn a thread's resume budget."""

import os
import tempfile
import unittest

from codex_autoresume.daemon import Daemon, _is_spawn_error, _spawn_error_tail
from codex_autoresume.ratelimits import RateLimitError
from codex_autoresume.state import WatcherState


def _daemon():
    d = object.__new__(Daemon)
    d.state = WatcherState(state_path=os.path.join(
        tempfile.mkdtemp(prefix="cxr-spawn-"), "state.json"))
    return d


class TestSpawnErrorClassification(unittest.TestCase):
    def test_missing_executable_is_a_spawn_error(self):
        self.assertTrue(_is_spawn_error(FileNotFoundError(2, "missing")))
        self.assertTrue(_is_spawn_error(OSError(2, "missing")))

    def test_other_failures_are_not_spawn_errors(self):
        self.assertFalse(_is_spawn_error(OSError(13, "denied")))
        self.assertFalse(_is_spawn_error(RateLimitError("usage limit")))

    def test_history_tail_detection(self):
        self.assertTrue(_spawn_error_tail("[WinError 2] not found"))
        self.assertTrue(_spawn_error_tail("FileNotFoundError: missing"))
        self.assertFalse(_spawn_error_tail("usage limit reached"))


class TestSpawnFailureRepair(unittest.TestCase):
    def test_goal_budget_of_spawn_failures_is_cleared(self):
        d = _daemon()
        t = d.state.thread("t1")
        t.last_error = "goal_continue"
        t.fail_streak = 3
        t.next_wake_iso = "2030-01-01T00:00:00+08:00"
        t.history = [{"kind": "goal_continue_error", "ok": False,
                      "error_tail": "[WinError 2] nope"} for _ in range(3)]
        d._repair_spawn_failure_budget()
        self.assertEqual(t.fail_streak, 0)
        self.assertIsNone(t.next_wake_iso)

    def test_mixed_failures_keep_the_budget(self):
        d = _daemon()
        t = d.state.thread("t2")
        t.last_error = "goal_continue"
        t.fail_streak = 2
        t.history = [
            {"kind": "goal_continue_error", "ok": False,
             "error_tail": "[WinError 2] nope"},
            {"kind": "goal_continue_error", "ok": False,
             "error_tail": "usage limit reached"},
        ]
        d._repair_spawn_failure_budget()
        self.assertEqual(t.fail_streak, 2)

    def test_continue_budget_of_spawn_failures_is_cleared(self):
        d = _daemon()
        t = d.state.thread("t3")
        t.last_error = "continue_error"
        t.continue_fails = 2
        t.history = [{"kind": "continue_error", "ok": False,
                      "spawn_error": True} for _ in range(2)]
        d._repair_spawn_failure_budget()
        self.assertEqual(t.continue_fails, 0)


if __name__ == "__main__":
    unittest.main()
