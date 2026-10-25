"""Rollover detection must survive daemon restarts (persisted baseline).

The weekly reset once went unrecorded because the daemon restarted shortly
before the reset: the in-memory baseline was gone, the first post-restart
snapshot already showed the post-reset values, and no drop was ever seen.
"""
import os
import tempfile
import unittest
from unittest import mock

import codex_autoresume.daemon as dm
from codex_autoresume.config import load_config
from codex_autoresume.ratelimits import QuotaSnapshot, Window
from codex_autoresume.state import WatcherState, load_state


class ResetEventPersistenceTest(unittest.TestCase):
    def _daemon(self, tmp):
        d = object.__new__(dm.Daemon)
        d.cfg = load_config(None)
        d.codex_home = tmp
        d.state = WatcherState(
            state_path=os.path.join(tmp, "state.json"))
        return d

    def _snap(self, p, s):
        return QuotaSnapshot(
            primary=Window(used_percent=p),
            secondary=Window(used_percent=s))

    def test_drop_after_restart_is_recorded(self):
        tmp = tempfile.mkdtemp(prefix="cxr-re-")
        d = self._daemon(tmp)
        d._snapshot = self._snap(85, 78)
        d._record_reset_events()
        self.assertEqual(d.state.reset_events, [])
        self.assertEqual(d.state.prev_primary_pct, 85)

        # daemon restarts: fresh object, state reloaded from disk
        d2 = self._daemon(tmp)
        d2.state = load_state(d.state.state_path)
        d2._snapshot = self._snap(0, 0)     # both windows rolled over
        d2._record_reset_events()
        kinds = sorted(e["kind"] for e in d2.state.reset_events)
        self.assertEqual(kinds, ["primary", "weekly"])
        self.assertEqual(d2.state.prev_secondary_pct, 0)

    def test_no_event_without_drop(self):
        tmp = tempfile.mkdtemp(prefix="cxr-re2-")
        d = self._daemon(tmp)
        d._snapshot = self._snap(50, 50)
        d._record_reset_events()
        d._snapshot = self._snap(60, 55)    # rising, not resetting
        d._record_reset_events()
        self.assertEqual(d.state.reset_events, [])
        self.assertEqual(d.state.prev_primary_pct, 60)


if __name__ == "__main__":
    unittest.main()
