"""Newest-turn verification, explicit arming, and cold-start seeding."""

import argparse
import copy
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace

from codex_autoresume import cli
from codex_autoresume.codexenv import CodexState
from codex_autoresume.config import DEFAULTS
from codex_autoresume.control import ControlApi
from codex_autoresume.daemon import Daemon
from codex_autoresume.detect import InterruptionEvent, detect, seed_seen
from codex_autoresume.ratelimits import QuotaSnapshot, Window
from codex_autoresume.state import (
    ThreadState, WatcherState, arm_thread, load_state)
from tests.test_detect import build_codex_home, err_payload

CANON = "t-primary"
ALIAS = "01a12031-439c-7400-b550-891b01130506"


def _thread_row(tid, now_ms, rollout="r.jsonl"):
    return (tid, rollout, 1, 1, "user", "openai", "/w", "t",
            "read-only", "never", 0, 1, 0, "m", "low", now_ms, "n")


def _daemon(home: str) -> Daemon:
    d = object.__new__(Daemon)
    d.cfg = copy.deepcopy(DEFAULTS)
    d.codex_home = home
    d.state = WatcherState(state_path=os.path.join(
        tempfile.mkdtemp(prefix="cxr-lt-"), "state.json"))
    d._snapshot = None
    d._snapshot_at = 0.0
    d._state_mtime = 0.0
    return d


class TestLatestTurns(unittest.TestCase):
    def test_newest_turn_wins(self):
        now = int(time.time())
        root = tempfile.mkdtemp(prefix="cxr-lt-")
        home = build_codex_home(root, turns=[
            (CANON, "turn-old", "failed",
             err_payload("usageLimitExceeded",
                         "You've hit your usage limit."), now - 3600, now - 3500),
            (CANON, "turn-new", "completed", None, now - 600, now - 60),
        ], threads=[_thread_row(CANON, now * 1000)])
        lt = CodexState(home).latest_turn(CANON)
        self.assertIsNotNone(lt)
        self.assertEqual(lt.turn_id, "turn-new")
        self.assertEqual(lt.status, "completed")
        self.assertEqual(lt.codex_error_info, "")

    def test_continuation_id_folds_onto_canonical_thread(self):
        now = int(time.time())
        root = tempfile.mkdtemp(prefix="cxr-lt-")
        home = build_codex_home(root, turns=[
            (CANON, "turn-canon", "completed", None, now - 900, now - 800),
            (ALIAS, "turn-alias", "failed",
             err_payload("usageLimitExceeded",
                         "You've hit your usage limit."), now - 300, now - 200),
        ], threads=[_thread_row(CANON, now * 1000)])
        cs = CodexState(home)
        lt = cs.latest_turn(CANON)
        self.assertEqual(lt.thread_id, CANON)
        self.assertEqual(lt.turn_id, "turn-alias")
        self.assertEqual(lt.as_failure().codex_error_info,
                         "usageLimitExceeded")
        self.assertIn("usage limit", lt.as_failure().message)

    def test_unknown_thread_has_no_turn(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-lt-"))
        self.assertIsNone(CodexState(home).latest_turn("nope"))


class TestSendTimeRecheck(unittest.TestCase):
    def test_completed_newest_turn_cancels_the_prompt(self):
        now = int(time.time())
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-lt-"), turns=[
            ("t1", "turn-1", "completed", None, now - 600, now - 300)])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        t.last_kind = "usage_primary"
        t.last_reset_at = (datetime.now().astimezone()
                           - timedelta(minutes=5)).isoformat()
        t.next_wake_iso = datetime.now().astimezone().isoformat()
        self.assertFalse(d._interruption_still_owns_thread("t1", t))
        self.assertEqual(t.last_kind, "")
        self.assertIsNone(t.last_reset_at)
        self.assertIsNone(t.next_wake_iso)
        saved = load_state(d.state.state_path).threads["t1"]
        self.assertEqual(saved.last_kind, "")

    def test_failed_newest_turn_keeps_ownership(self):
        now = int(time.time())
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-lt-"), turns=[
            ("t1", "turn-1", "failed",
             err_payload("usageLimitExceeded", "limit"), now - 600, now - 300)])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        t.last_kind = "usage_primary"
        self.assertTrue(d._interruption_still_owns_thread("t1", t))
        self.assertEqual(t.last_kind, "usage_primary")

    def test_live_turn_defers_instead_of_prompting(self):
        now = int(time.time())
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-lt-"), turns=[
            ("t1", "turn-1", "inProgress", None, now - 60, None)])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        t.last_kind = "usage_primary"
        self.assertFalse(d._interruption_still_owns_thread("t1", t))
        self.assertIsNotNone(t.next_wake_iso)
        self.assertGreater(
            datetime.fromisoformat(t.next_wake_iso),
            datetime.now().astimezone())

    def test_unreadable_history_cancels_the_prompt(self):
        d = _daemon(os.path.join(tempfile.mkdtemp(prefix="cxr-lt-"),
                                 "missing-home"))
        t = d.state.thread("t1")
        t.tracked = True
        t.last_kind = "usage_primary"
        self.assertFalse(d._interruption_still_owns_thread("t1", t))


class TestStaleLimitEvidence(unittest.TestCase):
    def test_old_failure_with_past_reset_does_not_block(self):
        d = _daemon(tempfile.mkdtemp(prefix="cxr-lt-"))
        now = datetime.now().astimezone()
        occurred = now - timedelta(days=3)
        event = InterruptionEvent(
            thread_id="t1", kind="usage_primary",
            reset_at=occurred + timedelta(hours=1), source="turn:x",
            occurred_at=occurred)
        raw = QuotaSnapshot(
            primary=Window(used_percent=12,
                           resets_at=int(now.timestamp()) - 60),
            secondary=Window(used_percent=3))
        d._note_limit_evidence([event], raw)
        self.assertIsNone(d.state.limit_until_iso)

    def test_fresh_unparsable_failure_still_blocks(self):
        d = _daemon(tempfile.mkdtemp(prefix="cxr-lt-"))
        now = datetime.now().astimezone()
        event = InterruptionEvent(
            thread_id="t1", kind="usage_primary", reset_at=None,
            source="turn:x", occurred_at=now - timedelta(minutes=2))
        raw = QuotaSnapshot(primary=Window(used_percent=99),
                            secondary=Window(used_percent=3))
        d._note_limit_evidence([event], raw)
        self.assertIsNotNone(d.state.limit_until_iso)


class TestArmThread(unittest.TestCase):
    def test_explicit_arm_resets_the_retry_budget(self):
        t = ThreadState(
            tracked=False, manual=False, fail_streak=6, overload_streak=4,
            next_wake_iso="2099-01-01T00:00:00+00:00")
        arm_thread(t)
        self.assertTrue(t.manual)
        self.assertTrue(t.tracked)
        self.assertEqual(t.fail_streak, 0)
        self.assertEqual(t.overload_streak, 0)
        self.assertIsNone(t.next_wake_iso)


class TestControlArm(unittest.TestCase):
    def _api(self):
        d = SimpleNamespace(
            state=WatcherState(state_path=os.path.join(
                tempfile.mkdtemp(prefix="cxr-lt-"), "state.json")),
            _state_mtime=0.0)
        api = object.__new__(ControlApi)
        api.daemon = d
        return api

    def test_guard_on_arms_and_off_disarms(self):
        api = self._api()
        t = api.daemon.state.thread("t1")
        t.fail_streak = 6
        res = api.set_thread("t1", {"guard": True})
        self.assertTrue(t.manual)
        self.assertTrue(t.tracked)
        self.assertEqual(t.fail_streak, 0)
        self.assertEqual(t.last_kind, "")      # never fabricated
        self.assertTrue(res["guard"])
        api.set_thread("t1", {"guard": False})
        self.assertFalse(t.manual)
        self.assertFalse(t.tracked)


class TestCmdWatch(unittest.TestCase):
    def test_watch_arms_without_fabricating_a_kind(self):
        now = int(time.time())
        root = tempfile.mkdtemp(prefix="cxr-lt-")
        home = build_codex_home(root, turns=[],
                                threads=[_thread_row(CANON, now * 1000)])
        state_dir = os.path.join(root, "state")
        os.makedirs(state_dir, exist_ok=True)
        cfg_path = os.path.join(root, "config.toml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write('[general]\ncodex_home = "%s"\nstate_dir = "%s"\n'
                    % (home.replace("\\", "\\\\"),
                       state_dir.replace("\\", "\\\\")))
        args = argparse.Namespace(thread=CANON, last=False, all=False,
                                  goal=False, config=cfg_path, state_dir=None)
        self.assertEqual(cli.cmd_watch(args), 0)
        t = load_state(os.path.join(state_dir, "state.json")).threads[CANON]
        self.assertTrue(t.manual)
        self.assertTrue(t.tracked)
        self.assertEqual(t.fail_streak, 0)
        self.assertEqual(t.last_kind, "")


class TestSeedSeenWatermark(unittest.TestCase):
    def test_only_history_at_or_below_the_watermark_is_seeded(self):
        now = int(time.time())
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-lt-"), turns=[
            ("t1", "turn-old", "failed",
             err_payload("usageLimitExceeded", "old"), now - 7200, now - 7100),
            ("t1", "turn-new", "failed",
             err_payload("usageLimitExceeded", "new"), now - 120, now - 60),
        ])
        cs = CodexState(home)
        watermark = now - 3600
        seen, wm = seed_seen(cs, set(), watermark, up_to=watermark)
        self.assertEqual(seen, {"turn:turn-old"})
        self.assertEqual(wm, watermark)
        events = detect(cs, seen, since_completed_at=wm,
                        now=datetime.now().astimezone())
        self.assertEqual([e.source for e in events], ["turn:turn-new"])

    def test_fresh_failure_without_completed_at_is_not_seeded(self):
        now = int(time.time())
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-lt-"), turns=[
            ("t1", "turn-live", "failed",
             err_payload("usageLimitExceeded", "live"), now - 60, None),
        ])
        seen, _ = seed_seen(CodexState(home), set(), now - 3600, up_to=now - 3600)
        self.assertEqual(seen, set())


if __name__ == "__main__":
    unittest.main()
