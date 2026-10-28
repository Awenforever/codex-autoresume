"""Rollover detection must survive daemon restarts (persisted baseline).

The weekly reset once went unrecorded because the daemon restarted shortly
before the reset: the in-memory baseline was gone, the first post-restart
snapshot already showed the post-reset values, and no drop was ever seen.
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

import codex_autoresume.daemon as dm
from codex_autoresume.config import load_config
from codex_autoresume.ratelimits import QuotaSnapshot, Window
from codex_autoresume.state import WatcherState, load_state


def _make_daemon(tmp):
    d = object.__new__(dm.Daemon)
    d.cfg = load_config(None)
    d.codex_home = tmp
    d.state = WatcherState(state_path=os.path.join(tmp, "state.json"))
    return d


def _snap(p, s):
    return QuotaSnapshot(
        primary=Window(used_percent=p),
        secondary=Window(used_percent=s))


class ResetEventPersistenceTest(unittest.TestCase):
    def test_drop_after_restart_is_recorded(self):
        tmp = tempfile.mkdtemp(prefix="cxr-re-")
        d = _make_daemon(tmp)
        d._snapshot = _snap(85, 78)
        d._record_reset_events()
        self.assertEqual(d.state.reset_events, [])
        self.assertEqual(d.state.prev_primary_pct, 85)

        # daemon restarts: fresh object, state reloaded from disk
        d2 = _make_daemon(tmp)
        d2.state = load_state(d.state.state_path)
        d2._snapshot = _snap(0, 0)     # both windows rolled over
        d2._record_reset_events()
        kinds = sorted(e["kind"] for e in d2.state.reset_events)
        self.assertEqual(kinds, ["primary", "weekly"])
        self.assertEqual(d2.state.prev_secondary_pct, 0)

    def test_no_event_without_drop(self):
        tmp = tempfile.mkdtemp(prefix="cxr-re2-")
        d = _make_daemon(tmp)
        d._snapshot = _snap(50, 50)
        d._record_reset_events()
        d._snapshot = _snap(60, 55)    # rising, not resetting
        d._record_reset_events()
        self.assertEqual(d.state.reset_events, [])
        self.assertEqual(d.state.prev_primary_pct, 60)


class WeeklyExhaustionTimestampTest(unittest.TestCase):
    """Exhausted means "ran dry at", never "weekly resets at"."""

    def test_observed_climb_is_persisted_and_kept(self):
        tmp = tempfile.mkdtemp(prefix="cxr-wex-")
        d = _make_daemon(tmp)
        at0 = datetime(2026, 10, 26, 5, 21, 22, tzinfo=timezone.utc)
        with mock.patch.object(dm, "_now", return_value=at0):
            d._snapshot = _snap(20, 99)
            d._record_reset_events()
        self.assertIsNone(d.state.secondary_exhausted_at)
        at1 = at0 + timedelta(seconds=61)
        with mock.patch.object(dm, "_now", return_value=at1):
            d._snapshot = _snap(20, 100)
            d._record_reset_events()
        self.assertEqual(d.state.secondary_exhausted_at,
                         "2026-10-26T05:22:23+00:00")
        # survives a restart
        st = load_state(d.state.state_path)
        self.assertEqual(st.secondary_exhausted_at,
                         "2026-10-26T05:22:23+00:00")
        # still full later: the timestamp of the climb is kept
        d2 = _make_daemon(tmp)
        d2.state = st
        with mock.patch.object(dm, "_now",
                               return_value=at1 + timedelta(hours=6)):
            d2._snapshot = _snap(20, 100)
            d2._record_reset_events()
        self.assertEqual(d2.state.secondary_exhausted_at,
                         "2026-10-26T05:22:23+00:00")

    def test_startup_while_already_exhausted_does_not_claim_now(self):
        tmp = tempfile.mkdtemp(prefix="cxr-wex2-")
        d = _make_daemon(tmp)
        # state written by an older build: already at 100%, no timestamp
        d.state.prev_secondary_pct = 100
        at1 = datetime(2026, 10, 28, 5, 54, 12, tzinfo=timezone.utc)
        with mock.patch.object(dm, "_now", return_value=at1):
            d._snapshot = _snap(0, 100)
            d._record_reset_events()
        self.assertIsNone(d.state.secondary_exhausted_at)
        self.assertIsNone(load_state(d.state.state_path).secondary_exhausted_at)

    def test_clear_on_weekly_rollover(self):
        tmp = tempfile.mkdtemp(prefix="cxr-wex3-")
        d = _make_daemon(tmp)
        d.state.prev_secondary_pct = 99
        at1 = datetime(2026, 10, 26, 5, 21, 22, tzinfo=timezone.utc)
        with mock.patch.object(dm, "_now", return_value=at1):
            d._snapshot = _snap(0, 100)
            d._record_reset_events()
        self.assertTrue(d.state.secondary_exhausted_at)
        at2 = datetime(2026, 10, 30, 3, 0, 0, tzinfo=timezone.utc)
        with mock.patch.object(dm, "_now", return_value=at2):
            d._snapshot = _snap(0, 2)     # weekly window rolled over
            d._record_reset_events()
        self.assertIsNone(d.state.secondary_exhausted_at)
        kinds = [e["kind"] for e in d.state.reset_events]
        self.assertIn("weekly", kinds)
        # and the cleared value is what a restart sees
        self.assertIsNone(load_state(d.state.state_path).secondary_exhausted_at)


if __name__ == "__main__":
    unittest.main()
