"""The weekly ratio pool: only complete 5h windows, stable across restarts.

Weekly growth inside a block tracks primary burn, so a completed block is
one valid (dP, dS) pair. The open window's baseline is persisted: a daemon
restart must neither lose it nor pair two blocks' deltas together.
"""
import os
import tempfile
import unittest

import codex_autoresume.daemon as dm
from codex_autoresume.config import load_config
from codex_autoresume.ratelimits import QuotaSnapshot, Window
from codex_autoresume.state import WatcherState, load_state, save_state

B0 = 1_800_000_000
WIN = 5 * 3600


class WeeklyRatioTrackerTest(unittest.TestCase):
    def _daemon(self, tmp, state=None):
        d = object.__new__(dm.Daemon)
        d.cfg = load_config(None)
        d.codex_home = tmp
        d.state = state or WatcherState(
            state_path=os.path.join(tmp, "state.json"))
        d._state_mtime = 0.0
        return d

    def _feed(self, d, p, s, b=B0, plan="plus"):
        d._snapshot = QuotaSnapshot(
            primary=Window(used_percent=p, resets_at=b),
            secondary=Window(used_percent=s),
            plan_type=plan)
        d._track_weekly_ratio()

    def test_completed_window_is_pooled_and_persisted(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr-"))
        self._feed(d, 0, 10)
        self._feed(d, 50, 18)
        self._feed(d, 100, 26)
        self._feed(d, 2, 26, b=B0 + WIN)     # block rolled
        self.assertEqual(d.state.weekly_ratio_windows,
                         [{"b": B0, "dp": 100, "ds": 16}])
        self.assertEqual((d.state.weekly_ratio_b, d.state.weekly_ratio_p0,
                          d.state.weekly_ratio_s0),
                         (B0 + WIN, 2, 26))
        st2 = load_state(d.state.state_path)
        self.assertEqual(st2.weekly_ratio_windows,
                         [{"b": B0, "dp": 100, "ds": 16}])
        self.assertEqual((st2.weekly_ratio_p1, st2.weekly_ratio_s1), (2, 26))

    def test_open_window_survives_restart(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr2-"))
        self._feed(d, 10, 5)
        self._feed(d, 20, 6)
        save_state(d.state)                  # the tick-end save
        d2 = self._daemon(os.path.dirname(d.state.state_path),
                          load_state(d.state.state_path))
        self.assertEqual((d2.state.weekly_ratio_p0,
                          d2.state.weekly_ratio_p1), (10, 20))
        self._feed(d2, 30, 7)
        self._feed(d2, 0, 7, b=B0 + WIN)     # completed across the restart
        self.assertEqual(d2.state.weekly_ratio_windows,
                         [{"b": B0, "dp": 20, "ds": 2}])

    def test_mid_block_weekly_reset_discards_open_window(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr3-"))
        self._feed(d, 40, 15)
        self._feed(d, 60, 22)
        d.state.last_weekly_eta = 123.0
        self._feed(d, 60, 0)                 # weekly reset, block unchanged
        self.assertEqual(d.state.weekly_ratio_windows, [])
        self.assertIsNone(d.state.last_weekly_eta)
        self.assertEqual((d.state.weekly_ratio_p0, d.state.weekly_ratio_s0),
                         (60, 0))

    def test_credit_refill_without_epoch_change_closes_window(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr4-"))
        self._feed(d, 0, 10)
        self._feed(d, 50, 18)
        self._feed(d, 0, 18)                 # primary refilled mid-block
        self.assertEqual(d.state.weekly_ratio_windows,
                         [{"b": B0, "dp": 50, "ds": 8}])

    def test_short_window_is_not_pooled(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr5-"))
        self._feed(d, 0, 10)
        self._feed(d, 5, 11)
        self._feed(d, 0, 11, b=B0 + WIN)
        self.assertEqual(d.state.weekly_ratio_windows, [])

    def test_epoch_jitter_does_not_close_the_window(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr6-"))
        self._feed(d, 10, 5)
        self._feed(d, 20, 6, b=B0 + 120)
        self.assertEqual(d.state.weekly_ratio_windows, [])
        self.assertEqual((d.state.weekly_ratio_p0,
                          d.state.weekly_ratio_p1), (10, 20))

    def test_plan_change_clears_pool(self):
        d = self._daemon(tempfile.mkdtemp(prefix="cxr-wr7-"))
        self._feed(d, 0, 10)
        self._feed(d, 100, 26)
        self._feed(d, 2, 26, b=B0 + WIN)
        self.assertEqual(len(d.state.weekly_ratio_windows), 1)
        self._feed(d, 4, 27, b=B0 + WIN, plan="business")
        self.assertEqual(d.state.weekly_ratio_windows, [])
        self.assertEqual((d.state.weekly_ratio_b, d.state.weekly_ratio_p0),
                         (B0 + WIN, 4))


if __name__ == "__main__":
    unittest.main()
