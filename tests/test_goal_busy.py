"""Goal threads whose writer is held by the app must NOT count as failures.

The desktop app holds the thread writer while the goal is running (or the
conversation is open). thread/goal/set then answers "already has an active
writer" — the guard must stand down quietly and retry at the next poll
instead of incrementing fail_streak / error history forever.
"""
import os
import tempfile
import unittest
from unittest import mock

import codex_autoresume.codexenv as ce
import codex_autoresume.daemon as dm
import codex_autoresume.ratelimits as rl
from codex_autoresume.config import load_config
from codex_autoresume.state import WatcherState


class FakeWriterBusyClient:
    def __init__(self, codex_bin):
        pass

    def _rpc(self, method, params):
        raise rl.RateLimitError(
            "thread/goal/set failed: {'code': -32603, 'message': "
            "'failed to record goal instruction: thread abc already has "
            "an active writer'}")

    def close(self):
        pass


class GoalContinueBusyTest(unittest.TestCase):
    def test_writer_conflict_maps_to_goal_busy_error(self):
        with mock.patch.object(rl, "AppServerClient", FakeWriterBusyClient):
            with self.assertRaises(rl.GoalBusyError):
                rl.goal_continue("codex.exe", "abc")
        # plain rate-limit errors stay untouched
        class Other:
            def __init__(self, b):
                pass

            def _rpc(self, m, p):
                raise rl.RateLimitError("something else")

            def close(self):
                pass
        with mock.patch.object(rl, "AppServerClient", Other):
            with self.assertRaises(rl.RateLimitError):
                rl.goal_continue("codex.exe", "abc")


class _GoalsUsageLimited:
    def __init__(self, home):
        pass

    def goals(self):
        return [{"thread_id": "g1", "status": "usage_limited"}]

    def busy_threads(self):
        return set()


class GoalResumeBusyHandlingTest(unittest.TestCase):
    def test_busy_writer_is_not_a_failure(self):
        tmp = tempfile.mkdtemp(prefix="cxr-busy-")
        d = object.__new__(dm.Daemon)
        d.cfg = load_config(None)
        d.codex_home = tmp
        d.codex_bin = "codex.exe"
        d.state = WatcherState(
            state_path=os.path.join(tmp, "state.json"))
        t = d.state.thread("g1")
        t.is_goal = True
        t.tracked = True
        t.fail_streak = 0
        decision = mock.Mock(model=None, reasoning_effort=None)

        with mock.patch.object(ce, "CodexState", _GoalsUsageLimited), \
             mock.patch.object(rl, "goal_continue",
                               side_effect=rl.GoalBusyError("busy")):
            handled = d._goal_resume("g1", t, decision)

        self.assertTrue(handled)
        self.assertEqual(t.fail_streak, 0)
        self.assertEqual(t.total_resumes, 0)
        self.assertEqual(t.success_resumes, 0)
        self.assertEqual(t.last_outcome, "writer_busy")
        self.assertIsNotNone(t.next_wake_iso)
        errors = [h for h in t.history
                  if isinstance(h, dict) and h.get("kind") == "goal_continue_error"]
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
