"""Guard phases, delivery detection, prompt defaults, and panel ordering.

Phase semantics (only machine-authoritative signals):
  * goal status complete          -> completed (true completion)
  * failed/interrupted + limit    -> waiting  (interruption)
  * completed turn, goal active   -> stage    (a stage of a longer task)
  * completed turn after a guard
    resume on an ordinary thread  -> delivered (guard's promise fulfilled)
Anything else stays watching; ordinary prose is never read as "done".
"""

import copy
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import datetime

from codex_autoresume.codexenv import CodexState
from codex_autoresume.config import (
    DEFAULTS, DEFAULT_PROMPT_GENERIC, DEFAULT_PROMPT_GOAL)
from codex_autoresume.control import ControlApi, ORDER_HOLD_SECONDS
from codex_autoresume.daemon import Daemon
from codex_autoresume.state import WatcherState, load_state
from tests.test_detect import build_codex_home, err_payload

NOW = int(time.time())


def _thread_row(tid, updated_ms):
    return (tid, "r.jsonl", 1, 1, "user", "openai", "/w", "title",
            "read-only", "never", 0, 1, 0, "m", "low", updated_ms, tid)


def _daemon(home, state_path=None):
    d = object.__new__(Daemon)
    d.cfg = copy.deepcopy(DEFAULTS)
    d.codex_home = home
    d.state = WatcherState(state_path=state_path or os.path.join(
        tempfile.mkdtemp(prefix="cxr-ph-"), "state.json"))
    d._inflight = set()
    d._inflight_lock = threading.Lock()
    d._snapshot = None
    d._state_mtime = 0.0
    return d


def _add_final_item(home, thread_id, turn_id, phase="final_answer"):
    hist = os.path.join(home, "thread_history_1.sqlite")
    con = sqlite3.connect(hist)
    iid = "item-" + turn_id
    try:
        con.execute(
            "INSERT OR REPLACE INTO thread_items (thread_id, turn_id, "
            "item_id, rollout_ordinal, created_at_ms, item_json, item_type) "
            "VALUES (?,?,?,?,?,?,?)",
            (thread_id, turn_id, iid, 0, NOW * 1000,
             json.dumps({"phase": phase}), "agentMessage"))
        con.execute(
            "UPDATE thread_turns SET final_agent_item_id=? "
            "WHERE thread_id=? AND turn_id=?", (iid, thread_id, turn_id))
        con.commit()
    finally:
        con.close()


def _refresh(d, busy=None):
    codex = CodexState(d.codex_home)
    latest = codex.latest_turns()
    goals = {g["thread_id"]: g.get("status") for g in codex.goals()}
    d._refresh_phases(latest, goals, busy or set(),
                      datetime.now().astimezone())
    return latest


def _goal_row(tid, status):
    return (tid, "g-" + tid, "objective", status, 0, 0, 0,
            NOW * 1000, NOW * 1000)


class TestFinalAnswerFlag(unittest.TestCase):
    def test_completed_turn_with_final_answer_is_flagged(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[
            ("t1", "turn-1", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-1", "final_answer")
        self.assertTrue(CodexState(home).latest_turn("t1").final_answer)

    def test_non_final_agent_message_is_not_a_delivery(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[
            ("t1", "turn-1", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-1", "commentary")
        self.assertFalse(CodexState(home).latest_turn("t1").final_answer)

    def test_failed_turn_has_no_final_item(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[
            ("t1", "turn-1", "failed",
             err_payload("usageLimitExceeded", "limit"), NOW - 60, NOW - 30)])
        self.assertFalse(CodexState(home).latest_turn("t1").final_answer)


class TestInterruptionPhases(unittest.TestCase):
    def test_quota_and_capacity_are_waiting(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        t.last_kind = "usage_primary"
        _refresh(d)
        self.assertEqual((t.phase, t.phase_reason), ("waiting", "quota"))
        t.last_kind = "server_overloaded"
        _refresh(d)
        self.assertEqual((t.phase, t.phase_reason), ("waiting", "capacity"))

    def test_goal_statuses_park_as_waiting(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        cases = {"usage_limited": "quota", "budget_limited": "budget",
                 "blocked": "blocked"}
        for status, reason in cases.items():
            home = build_codex_home(root + status, turns=[],
                                    goals=[_goal_row("g1", status)])
            d = _daemon(home)
            t = d.state.thread("g1")
            t.tracked = True
            _refresh(d)
            self.assertEqual((t.phase, t.phase_reason), ("waiting", reason),
                             status)

    def test_all_paused_wins_over_waiting(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[],
                                goals=[_goal_row("g1", "usage_limited")])
        d = _daemon(home)
        d.state.paused = True
        t = d.state.thread("g1")
        t.tracked = True
        _refresh(d)
        self.assertEqual((t.phase, t.phase_reason), ("paused", "all"))


class TestCompletionPhases(unittest.TestCase):
    def test_goal_complete_is_the_flip(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("g1", "turn-1", "completed", None, NOW - 60, NOW - 30)],
            goals=[_goal_row("g1", "complete")])
        _add_final_item(home, "g1", "turn-1")
        d = _daemon(home)
        t = d.state.thread("g1")
        t.tracked = True
        latest = _refresh(d)
        self.assertEqual((t.phase, t.phase_reason), ("completed", "goal"))
        self.assertEqual(t.phase_turn_id, "turn-1")
        self.assertTrue(any(h["kind"] == "goal_complete" for h in t.history))
        # a vanished goal row must not undo the completion flip
        d._refresh_phases(latest, {}, set(), datetime.now().astimezone())
        self.assertEqual(t.phase, "completed")

    def test_new_turn_after_completion_returns_to_watching(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[
            ("g1", "turn-1", "completed", None, NOW - 60, NOW - 30)],
            goals=[_goal_row("g1", "complete")])
        _add_final_item(home, "g1", "turn-1")
        d = _daemon(home)
        t = d.state.thread("g1")
        t.tracked = True
        _refresh(d)
        self.assertEqual(t.phase, "completed")
        con = sqlite3.connect(os.path.join(
            home, "thread_history_1.sqlite"))
        con.execute(
            "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, "
            "status, started_at, completed_at) VALUES (?,?,?,?,?,?)",
            ("g1", "turn-2", 1, "inProgress", NOW - 5, None))
        con.commit()
        con.close()
        d._refresh_phases(CodexState(home).latest_turns(), {},
                          set(), datetime.now().astimezone())
        self.assertEqual(t.phase, "watching")

    def test_completed_turn_with_active_goal_is_stage_not_completed(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("g1", "turn-1", "completed", None, NOW - 60, NOW - 30)],
            goals=[_goal_row("g1", "active")])
        _add_final_item(home, "g1", "turn-1")
        d = _daemon(home)
        t = d.state.thread("g1")
        t.tracked = True
        _refresh(d)
        self.assertEqual((t.phase, t.phase_reason), ("stage", "goal"))


class TestDeliveryPhases(unittest.TestCase):
    def _resumed(self, home, turn="turn-2"):
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        t.resume_floor_turn_id = "turn-1"
        t.resume_floor_open = True
        return d, t

    def test_completed_resume_turn_is_delivered(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "failed",
             err_payload("usageLimitExceeded", "limit"), NOW - 120, NOW - 90),
            ("t1", "turn-2", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-2")
        d, t = self._resumed(home)
        _refresh(d)
        self.assertEqual(t.phase, "delivered")
        self.assertEqual(t.delivered_turn_id, "turn-2")
        self.assertTrue(any(h["kind"] == "delivered" for h in t.history))

    def test_same_turn_finished_in_place_is_delivered(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-1")
        d, t = self._resumed(home)
        _refresh(d)
        self.assertEqual(t.phase, "delivered")

    def test_new_turn_returns_to_watching(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "completed", None, NOW - 120, NOW - 90),
            ("t1", "turn-2", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-2")
        d, t = self._resumed(home)
        _refresh(d)
        self.assertEqual(t.phase, "delivered")
        con = sqlite3.connect(os.path.join(home, "thread_history_1.sqlite"))
        con.execute(
            "INSERT INTO thread_turns (thread_id, turn_id, rollout_ordinal, "
            "status, started_at, completed_at) VALUES (?,?,?,?,?,?)",
            ("t1", "turn-3", 2, "completed", NOW - 10, NOW - 5))
        con.commit()
        con.close()
        d._refresh_phases(CodexState(home).latest_turns(), {},
                          set(), datetime.now().astimezone())
        self.assertEqual(t.phase, "watching")

    def test_completed_without_floor_is_not_delivered(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-1")
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        _refresh(d)
        self.assertEqual(t.phase, "watching")
        self.assertIsNone(t.delivered_turn_id)

    def test_delivered_floor_turn_without_final_answer_is_watching(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "completed", None, NOW - 60, NOW - 30)])
        _add_final_item(home, "t1", "turn-1", "commentary")
        d, t = self._resumed(home)
        _refresh(d)
        self.assertEqual(t.phase, "watching")

    def test_goal_thread_never_uses_delivery(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("g1", "turn-2", "completed", None, NOW - 60, NOW - 30)],
            goals=[_goal_row("g1", "active")])
        _add_final_item(home, "g1", "turn-2")
        d = _daemon(home)
        t = d.state.thread("g1")
        t.tracked = True
        t.is_goal = True
        t.resume_floor_turn_id = "turn-1"
        t.resume_floor_open = True
        _refresh(d)
        self.assertIsNone(t.delivered_turn_id)
        self.assertEqual(t.phase, "stage")


class TestPhaseLifecycle(unittest.TestCase):
    def test_arm_resets_everything(self):
        from codex_autoresume.state import arm_thread
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        t.phase = "delivered"
        t.phase_reason = ""
        t.phase_turn_id = "x"
        t.delivered_turn_id = "x"
        t.resume_floor_turn_id = "y"
        t.resume_floor_open = True
        arm_thread(t)
        self.assertEqual(t.phase, "watching")
        self.assertIsNone(t.delivered_turn_id)
        self.assertIsNone(t.resume_floor_turn_id)
        self.assertFalse(t.resume_floor_open)

    def test_guard_off_clears_phase_but_keeps_stopped_visible(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.phase = "completed"
        t.phase_reason = "goal"
        _refresh(d)
        self.assertEqual(t.phase, "")
        # a guard that turned ITSELF off keeps the reason visible
        t.phase = "stopped"
        t.phase_reason = "weekly"
        _refresh(d)
        self.assertEqual((t.phase, t.phase_reason), ("stopped", "weekly"))

    def test_busy_thread_is_running(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "inProgress", None, NOW - 10, None)])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.tracked = True
        _refresh(d, busy={"t1"})
        self.assertEqual((t.phase, t.phase_reason), ("running", ""))

    def test_capture_floor_marks_an_unfinished_turn_open(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "failed",
             err_payload("usageLimitExceeded", "limit"), NOW - 60, NOW - 30)])
        d = _daemon(home)
        t = d.state.thread("t1")
        t.delivered_turn_id = "old"
        d._capture_resume_floor("t1", t)
        self.assertEqual(t.resume_floor_turn_id, "turn-1")
        self.assertTrue(t.resume_floor_open)
        self.assertIsNone(t.delivered_turn_id)

    def test_capture_floor_marks_a_completed_turn_closed(self):
        home = build_codex_home(tempfile.mkdtemp(prefix="cxr-ph-"), turns=[
            ("t1", "turn-1", "completed", None, NOW - 60, NOW - 30)])
        d = _daemon(home)
        t = d.state.thread("t1")
        d._capture_resume_floor("t1", t)
        self.assertFalse(t.resume_floor_open)


class TestPromptDefaults(unittest.TestCase):
    def test_prompts_are_short_and_carry_the_three_exits(self):
        for prompt in (DEFAULT_PROMPT_GOAL, DEFAULT_PROMPT_GENERIC):
            self.assertLess(len(prompt), 560)
            self.assertIn("Verify the actual state", prompt)
            self.assertIn("no redoing", prompt)
            self.assertIn("already done", prompt)
            self.assertIn("destructive", prompt)
            self.assertIn("Automated message", prompt)
        self.assertIn("acceptance check", DEFAULT_PROMPT_GOAL)
        self.assertEqual(DEFAULTS["resume"]["prompt_goal"],
                         DEFAULT_PROMPT_GOAL)
        self.assertEqual(DEFAULTS["resume"]["prompt_generic"],
                         DEFAULT_PROMPT_GENERIC)


class TestPinAndReorder(unittest.TestCase):
    def _api(self, home=None):
        d = _daemon(home or build_codex_home(
            tempfile.mkdtemp(prefix="cxr-ph-"), turns=[]))
        api = object.__new__(ControlApi)
        api.daemon = d
        return api

    def test_reorder_writes_rank_and_pin_atomically(self):
        api = self._api()
        res = api.reorder({"order": ["b", "a", "c"], "pinned": ["b"]})
        self.assertEqual(res, {"ordered": 3, "pinned": 1})
        st = load_state(api.daemon.state.state_path)
        self.assertEqual(st.threads["b"].ui_order, 0)
        self.assertEqual(st.threads["a"].ui_order, 10)
        self.assertEqual(st.threads["c"].ui_order, 20)
        self.assertTrue(st.threads["b"].ui_pin)
        self.assertFalse(st.threads["a"].ui_pin)
        # no ``moved`` list: the whole arrangement is treated as deliberate
        self.assertIsNotNone(st.threads["a"].ui_order_at)
        self.assertIsNotNone(st.threads["c"].ui_order_at)

    def test_reorder_marks_only_the_moved_card(self):
        api = self._api()
        api.reorder({"order": ["a", "b", "c"], "pinned": [], "moved": ["b"]})
        st = load_state(api.daemon.state.state_path)
        self.assertIsNone(st.threads["a"].ui_order_at)
        self.assertIsNotNone(st.threads["b"].ui_order_at)
        self.assertIsNone(st.threads["c"].ui_order_at)

    def _pinned_status(self, held_at):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[], threads=[
            _thread_row("p1", 1000), _thread_row("p2", 2000),
            _thread_row("p3", 3000)])
        api = self._api(home)
        d = api.daemon
        d.cached_announcement = lambda: None
        d.cached_models = lambda: []
        d.cached_update = lambda: {}
        st = d.state
        for tid, order in (("p1", 0), ("p2", 10), ("p3", 20)):
            t = st.thread(tid)
            t.ui_pin = True
            t.ui_order = order
        st.thread("p1").ui_order_at = held_at
        return [x["id"] for x in api.status()["threads"]]

    def test_hold_keeps_slot_while_peers_follow_recency(self):
        order = self._pinned_status(time.time())
        self.assertEqual(order, ["p1", "p3", "p2"])

    def test_expired_hold_rejoins_recency(self):
        order = self._pinned_status(time.time() - ORDER_HOLD_SECONDS - 5)
        self.assertEqual(order, ["p3", "p2", "p1"])

    def test_reorder_rejects_garbage(self):
        api = self._api()
        self.assertIn("error", api.reorder({"order": "a,b"}))
        self.assertIn("error", api.reorder({"order": [1, 2]}))
        self.assertIn("error", api.reorder({}))

    def test_pin_toggle_does_not_touch_guard(self):
        api = self._api()
        t = api.daemon.state.thread("t1")
        api.set_thread("t1", {"ui_pin": True})
        self.assertTrue(t.ui_pin)
        self.assertFalse(t.tracked)
        self.assertFalse(t.manual)
        api.set_thread("t1", {"ui_pin": False})
        self.assertFalse(t.ui_pin)

    def test_status_puts_pinned_first_then_manual_then_recency(self):
        root = tempfile.mkdtemp(prefix="cxr-ph-")
        home = build_codex_home(root, turns=[], threads=[
            _thread_row("t1", 1000), _thread_row("t2", 2000),
            _thread_row("t3", 3000), _thread_row("t4", 4000)])
        api = self._api(home)
        d = api.daemon
        d.cached_announcement = lambda: None
        d.cached_models = lambda: []
        d.cached_update = lambda: {}
        st = d.state
        st.thread("t1").ui_pin = True
        st.thread("t2").ui_order = 20
        st.thread("t3").ui_order = 10
        order = [x["id"] for x in api.status()["threads"]]
        self.assertEqual(order, ["t1", "t4", "t3", "t2"])
        api.reorder({"order": ["t2", "t3", "t4", "t1"],
                     "pinned": ["t2", "t1"]})
        order = [x["id"] for x in api.status()["threads"]]
        self.assertEqual(order, ["t2", "t1", "t3", "t4"])


if __name__ == "__main__":
    unittest.main()
