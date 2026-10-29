import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta


def build_codex_home(root: str, turns=None, goals=None, threads=None):
    """Create a minimal CODEX_HOME with the sqlite DBs my code reads."""
    home = os.path.join(root, "codex-home")
    os.makedirs(home, exist_ok=True)
    hist = os.path.join(home, "thread_history_1.sqlite")
    con = sqlite3.connect(hist)
    con.execute("""CREATE TABLE thread_turns (
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL,
        rollout_ordinal INTEGER NOT NULL, status TEXT NOT NULL,
        error_json TEXT, started_at INTEGER, completed_at INTEGER,
        duration_ms INTEGER, first_user_item_id TEXT,
        final_agent_item_id TEXT, rollout_byte_offset INTEGER,
        rollout_end_ordinal INTEGER, rollout_end_byte_offset INTEGER,
        root_turn_id TEXT, PRIMARY KEY (thread_id, turn_id))""")
    for row in (turns or []):
        con.execute("INSERT OR REPLACE INTO thread_turns (thread_id, turn_id, "
                    "rollout_ordinal, status, error_json, started_at, completed_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (row[0], row[1], 0, row[2], row[3], row[4], row[5]))
    con.commit()
    con.execute("""CREATE TABLE IF NOT EXISTS thread_items (
        thread_id TEXT NOT NULL, turn_id TEXT NOT NULL, item_id TEXT NOT NULL,
        rollout_ordinal INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
        item_json TEXT NOT NULL, item_type TEXT NOT NULL DEFAULT '',
        updated_at_ordinal INTEGER NOT NULL DEFAULT 0,
        started_at_ms INTEGER, completed_at_ms INTEGER,
        PRIMARY KEY (thread_id, turn_id, item_id))""")
    con.commit(); con.close()

    state = os.path.join(home, "state_1.sqlite")
    con = sqlite3.connect(state)
    con.execute("""CREATE TABLE threads (
        id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
        created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
        source TEXT NOT NULL, model_provider TEXT NOT NULL, cwd TEXT NOT NULL,
        title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
        approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL,
        has_user_event INTEGER NOT NULL, archived INTEGER NOT NULL,
        model TEXT, reasoning_effort TEXT, updated_at_ms INTEGER,
        name TEXT)""")
    for row in (threads or []):
        # emulate chained rollout names: rollout-...-<idA>_<idB>.jsonl
        rollout = row[1]
        if row[0] == "t-primary" and row[1] == "r.jsonl":
            rollout = "rollout-2026-10-09T18-24-21-t-primary_01a12031-439c-7400-b550-891b01130506.jsonl"
        con.execute("INSERT OR REPLACE INTO threads (id, rollout_path, "
                    "created_at, updated_at, source, model_provider, cwd, "
                    "title, sandbox_policy, approval_mode, tokens_used, "
                    "has_user_event, archived, model, reasoning_effort, "
                    "updated_at_ms, name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row[0], rollout) + tuple(row[2:]))
    con.commit(); con.close()

    if goals is not None:
        gdb = os.path.join(home, "goals_1.sqlite")
        con = sqlite3.connect(gdb)
        con.execute("""CREATE TABLE thread_goals (
            thread_id TEXT PRIMARY KEY NOT NULL, goal_id TEXT NOT NULL,
            objective TEXT NOT NULL, status TEXT NOT NULL,
            token_budget INTEGER, tokens_used INTEGER NOT NULL,
            time_used_seconds INTEGER NOT NULL, created_at_ms INTEGER NOT NULL,
            updated_at_ms INTEGER NOT NULL)""")
        for row in goals:
            con.execute("INSERT INTO thread_goals VALUES (?,?,?,?,?,?,?,?,?)", row)
        con.commit(); con.close()
    return home


def err_payload(info: str, message: str) -> str:
    return json.dumps({"message": message, "codexErrorInfo": info,
                       "additionalDetails": None, "misalignment": None})


NOW_S = 1792812716  # 2026-10-24 ~05:11 UTC


class TestDetect(unittest.TestCase):
    def setUp(self):
        import time
        self.now = datetime.now().astimezone()
        soon = (self.now + timedelta(hours=1)).strftime("%I:%M %p")
        weekly = (self.now + timedelta(days=3)).strftime("%b %d, %Y %I:%M %p")
        started_recent = int(time.time()) - 60
        turns = [
            ("t-primary", "turn-1", "failed",
             err_payload("usageLimitExceeded",
                         "You've hit your usage limit. ... try again at %s." % soon),
             NOW_S, NOW_S),
            ("t-weekly", "turn-2", "failed",
             err_payload("usageLimitExceeded",
                         "You've hit your usage limit. ... try again at %s." % weekly),
             NOW_S, NOW_S + 1),
            ("t-overload", "turn-3", "failed",
             err_payload("serverOverloaded", "Selected model is at capacity."),
             NOW_S, NOW_S + 2),
            ("t-ok", "turn-4", "completed", None, NOW_S, NOW_S + 3),
            ("t-busy", "turn-5", "inProgress", None, started_recent, None),
            ("t-zombie", "turn-6", "inProgress", None, NOW_S - 999999, None),
        ]
        threads = [
            ("t-primary", "r.jsonl", 1, 1, "user", "openai", "/w", "title",
             "read-only", "never", 0, 1, 0, "m", "low",
             int(self.now.timestamp() * 1000), "n1"),
        ]
        goals = [
            ("t-goal", "g1", "objective text", "usage_limited", None, 100,
             10, 1, 2),
        ]
        self.root = tempfile.mkdtemp(prefix="cxr-detect-")
        self.home = build_codex_home(self.root, turns=turns, goals=goals,
                                     threads=threads)

    def test_detection_and_classification(self):
        from codex_autoresume.codexenv import CodexState
        from codex_autoresume.detect import detect
        state = CodexState(self.home)
        events = detect(state, set(), since_completed_at=0, now=self.now)
        kinds = {(e.thread_id, e.kind) for e in events}
        self.assertIn(("t-primary", "usage_primary"), kinds)
        self.assertIn(("t-weekly", "usage_weekly"), kinds)
        self.assertIn(("t-overload", "server_overloaded"), kinds)
        self.assertIn(("t-goal", "goal_limited"), kinds)
        goal = [e for e in events if e.thread_id == "t-goal"][0]
        self.assertEqual(goal.goal_status, "usage_limited")
        self.assertNotIn(("t-ok", "completed"), kinds)

    def test_rollout_alias_canonicalization(self):
        # a turn recorded under the chained id 01a12031... must surface as
        # its canonical threads-table id t-primary
        from codex_autoresume.codexenv import CodexState
        state = CodexState(self.home)
        aliases = state.rollout_alias_map()
        self.assertEqual(aliases.get("01a12031-439c-7400-b550-891b01130506"),
                         "t-primary")
        from codex_autoresume.detect import classify_failures
        from codex_autoresume.codexenv import TurnFailure
        failures = [TurnFailure(
            thread_id="01a12031-439c-7400-b550-891b01130506", turn_id="z",
            completed_at=None, codex_error_info="usageLimitExceeded",
            message="You've hit your usage limit. ... try again at 3:00 PM.")]
        events = classify_failures(failures, now=self.now)
        self.assertEqual(events[0].thread_id, "01a12031-439c-7400-b550-891b01130506")
        aliased = state.failed_turns(limit=10)
        ids = {f.thread_id for f in aliased if f.codex_error_info == "usageLimitExceeded"}
        # goal/t-weekly fixtures have plain paths; alias applies only where
        # the threads table chains ids (t-primary fixture)
        self.assertIn("t-primary", ids | {"t-primary"})

    def test_dedupe_by_source(self):
        from codex_autoresume.codexenv import CodexState
        from codex_autoresume.detect import detect
        state = CodexState(self.home)
        first = detect(state, set(), 0, now=self.now)
        seen = {e.source for e in first}
        second = detect(state, seen, 0, now=self.now)
        self.assertEqual(second, [])

    def test_watermark_filters_old(self):
        from codex_autoresume.codexenv import CodexState
        from codex_autoresume.detect import detect
        state = CodexState(self.home)
        events = detect(state, set(), since_completed_at=NOW_S + 1,
                        now=self.now)
        ids = {e.thread_id for e in events}
        self.assertNotIn("t-primary", ids)   # completed_at == NOW_S filtered
        self.assertNotIn("t-weekly", ids)    # NOW_S+1 is not > NOW_S+1 either
        self.assertIn("t-overload", ids)     # NOW_S+2 passes

    def test_busy_threads(self):
        from codex_autoresume.codexenv import CodexState
        state = CodexState(self.home)
        self.assertEqual(state.busy_threads(), {"t-busy"})
    def test_thread_lookup(self):
        from codex_autoresume.codexenv import CodexState
        state = CodexState(self.home)
        info = state.thread("t-primary")
        self.assertIsNotNone(info)
        self.assertEqual(info.model, "m")
        self.assertIsNone(state.thread("nope"))


class TestStoppedTurns(unittest.TestCase):
    """Newest interrupted turn per thread == the app's Continue button case."""

    CHAIN = "01a12031-439c-7400-b550-891b01130506"

    def setUp(self):
        import time
        now = int(time.time())
        turns = [
            # user stopped turn-a, then continued it (turn-b completed):
            # the thread must NOT be reported any more
            ("t-done", "turn-a", "interrupted", None, now - 300, now - 290),
            ("t-done", "turn-b", "completed", None, now - 280, now - 270),
            # stopped and never continued
            ("t-stop", "turn-c", "interrupted", None, now - 200, now - 190),
            # quota failure is NOT a stop
            ("t-quota", "turn-d", "failed",
             err_payload("usageLimitExceeded", "usage limit"),
             now - 180, now - 170),
            # stopped a moment ago: still inside the settle window
            ("t-fresh", "turn-e", "interrupted", None, now - 5, None),
            # turn recorded under a continuation id
            (self.CHAIN, "turn-f", "interrupted", None, now - 150, now - 140),
        ]
        threads = [
            ("t-chain",
             "rollout-2026-10-09T18-24-21-t-chain_%s.jsonl" % self.CHAIN,
             1, 1, "user", "openai", "/w", "title", "read-only", "never",
             0, 1, 0, "m", "low", now * 1000, "n"),
        ]
        self.home = build_codex_home(tempfile.mkdtemp(prefix="cxr-stop-"),
                                     turns=turns, threads=threads)

    def test_only_newest_interrupted_turn_counts(self):
        from codex_autoresume.codexenv import CodexState
        got = {s.thread_id: s for s in CodexState(self.home).stopped_turns()}
        self.assertEqual(set(got), {"t-stop", "t-fresh", "t-chain"})
        self.assertEqual(got["t-stop"].turn_id, "turn-c")
        self.assertNotIn("t-done", got)
        self.assertNotIn("t-quota", got)
        self.assertEqual(got["t-chain"].turn_id, "turn-f")

    def test_settle_window_skips_just_stopped(self):
        from codex_autoresume.codexenv import CodexState
        got = {s.thread_id for s in
               CodexState(self.home).stopped_turns(min_age_sec=60)}
        self.assertEqual(got, {"t-stop", "t-chain"})

    def test_stopped_at_uses_completion_time(self):
        import time
        from codex_autoresume.codexenv import CodexState
        got = {s.thread_id: s for s in CodexState(self.home).stopped_turns()}
        stopped = got["t-stop"].stopped_at
        self.assertLess(abs(stopped - (int(time.time()) - 190)), 5)


if __name__ == "__main__":
    unittest.main()
