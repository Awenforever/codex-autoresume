"""UIA Continue-click fallback: verified, unambiguous, never fake-success."""
import os
import tempfile
import unittest
from unittest import mock

import codex_autoresume.codexenv as ce
import codex_autoresume.daemon as dm
import codex_autoresume.uia as uia
from codex_autoresume.config import load_config
from codex_autoresume.state import WatcherState


class _Goals:
    def __init__(self, rows):
        self._rows = rows

    def __call__(self, home):
        return self

    def goals(self):
        return [{"thread_id": r[0], "status": r[1]} for r in self._rows]


class UAIGoalContinueTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="cxr-uia-")
        self.d = object.__new__(dm.Daemon)
        self.d.cfg = load_config(None)
        self.d.codex_home = self.tmp
        self.d.codex_bin = "codex.exe"
        self.d.state = WatcherState(
            state_path=os.path.join(self.tmp, "state.json"))
        self.t = self.d.state.thread("g1")
        self.t.is_goal = True
        self.t.tracked = True
        patcher = mock.patch.object(dm.Daemon, "_play_sound")
        self.play_sound = patcher.start()
        self.addCleanup(patcher.stop)

    def test_click_flips_status_counts_success(self):
        goals = _Goals([("g1", "usage_limited")])
        flipped = _Goals([("g1", "active")])

        state_seq = {"n": 0}

        class Seq:
            def __call__(self, home):
                state_seq["n"] += 1
                return goals if state_seq["n"] == 1 else flipped

        with mock.patch.object(ce, "CodexState", Seq()), \
             mock.patch.object(uia, "click_continue",
                               return_value={"clicked": True, "detail": "pid=1"}), \
             mock.patch.object(dm.time, "sleep"):
            ok = self.d._uia_goal_continue("g1", self.t)
        self.assertTrue(ok)
        self.assertEqual(self.t.success_resumes, 1)
        self.assertEqual(self.t.last_outcome, "uia_continue")
        self.assertEqual(self.t.fail_streak, 0)
        self.assertEqual(self.t.history[-1]["kind"], "uia_continue")
        self.assertIsNotNone(self.t.resume_ok_until)
        self.play_sound.assert_called_once_with("resume")

    def test_ambiguous_goals_skip_click(self):
        goals = _Goals([("g1", "usage_limited"), ("g2", "usage_limited")])
        with mock.patch.object(ce, "CodexState", goals), \
             mock.patch.object(uia, "click_continue") as click:
            ok = self.d._uia_goal_continue("g1", self.t)
        self.assertFalse(ok)
        click.assert_not_called()

    def test_click_without_flip_is_busy(self):
        goals = _Goals([("g1", "usage_limited")])
        with mock.patch.object(ce, "CodexState", goals), \
             mock.patch.object(uia, "click_continue",
                               return_value={"clicked": True, "detail": "pid=1"}):
            ok = self.d._uia_goal_continue("g1", self.t)
        self.assertFalse(ok)
        self.assertEqual(self.t.success_resumes, 0)
        self.assertEqual(self.t.last_outcome, "")

    def test_not_clickable_is_busy(self):
        goals = _Goals([("g1", "usage_limited")])
        with mock.patch.object(ce, "CodexState", goals), \
             mock.patch.object(uia, "click_continue",
                               return_value={"clicked": False, "detail": "none"}):
            ok = self.d._uia_goal_continue("g1", self.t)
        self.assertFalse(ok)

    def test_disabled_by_config(self):
        goals = _Goals([("g1", "usage_limited")])
        self.d.cfg["general"]["uia_resume"] = False
        with mock.patch.object(ce, "CodexState", goals), \
             mock.patch.object(uia, "click_continue") as click:
            ok = self.d._uia_goal_continue("g1", self.t)
        self.assertFalse(ok)
        click.assert_not_called()


class SilentSpawnTest(unittest.TestCase):
    def test_uia_powershell_never_opens_a_console(self):
        completed = mock.Mock(stdout=b"[]")
        with mock.patch.object(uia.subprocess, "run",
                               return_value=completed) as run:
            uia._run_ps("$null")
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         uia.CREATE_NO_WINDOW)

    def test_codex_version_probe_never_opens_a_console(self):
        completed = mock.Mock(stdout="codex 1.0", stderr="")
        with mock.patch.object(ce.subprocess, "run",
                               return_value=completed) as run:
            ce.codex_version("codex.exe")
        self.assertEqual(run.call_args.kwargs["creationflags"],
                         ce.CREATE_NO_WINDOW)


if __name__ == "__main__":
    unittest.main()
