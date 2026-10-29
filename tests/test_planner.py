import unittest
from datetime import datetime, timedelta

from codex_autoresume.detect import InterruptionEvent
from codex_autoresume.planner import merge_settings, pick_fallback_model, plan_for_thread
from codex_autoresume.ratelimits import QuotaSnapshot, Window
from codex_autoresume.state import ThreadState


def base_cfg(**overrides):
    import copy
    from codex_autoresume.config import DEFAULTS
    cfg = copy.deepcopy(DEFAULTS)
    for dotted, val in overrides.items():
        section, key = dotted.split(".", 1)
        cfg[section][key] = val
    return cfg


def now_utc():
    return datetime.now().astimezone()


class TestPlanner(unittest.TestCase):
    def setUp(self):
        self.now = now_utc()
        self.reset = self.now + timedelta(minutes=30)

    def event(self, kind="usage_primary", reset=None):
        return InterruptionEvent(thread_id="t1", kind=kind, reset_at=reset,
                                 source="turn:x", detail="d")

    def test_weekly_off_skips(self):
        d = plan_for_thread(self.event("usage_weekly", self.now + timedelta(days=3)),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        # weekly guard off = stop permanently, not just skip this event
        self.assertEqual(d.action, "untrack")
        self.assertIn("weekly", d.reason)

    def test_goal_waits_without_headroom(self):
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=100,
                                            resets_at=int((self.now + timedelta(hours=2)).timestamp())))
        d = plan_for_thread(self.event("goal_limited"),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertIn("5h window exhausted", d.reason)

    def test_goal_due_with_headroom(self):
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=8,
                                            resets_at=int((self.now + timedelta(hours=4)).timestamp())))
        d = plan_for_thread(self.event("goal_limited"),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")

    def test_weekly_all_waits(self):
        d = plan_for_thread(self.event("usage_weekly", self.now + timedelta(days=3)),
                            ThreadState(tracked=True),
                            base_cfg(**{"general.weekly_resume": "all"}), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreater(d.wake_at, self.now + timedelta(days=2))

    def test_due_after_reset(self):
        past = self.now - timedelta(minutes=10)
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")
        self.assertIsNotNone(d.prompt)

    def test_quota_available_resumes_despite_future_reset(self):
        # usage window is fresh (allowed, low usage): resume immediately
        # even though a parsed reset time lies in the future
        snap = QuotaSnapshot(ordinary_usage_allowed=True,
                             primary=Window(used_percent=3,
                                            resets_at=int((self.now + timedelta(hours=4)).timestamp())))
        d = plan_for_thread(self.event("usage_primary", self.reset),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")

    def test_wait_before_reset(self):
        d = plan_for_thread(self.event("usage_primary", self.reset),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at, self.reset)

    def test_quota_gate_waits_when_deadline_far(self):
        # message reset passed, but snapshot shows the CURRENT window's
        # reset 2h away and usage still capped -> wait quietly, no attempt
        past = self.now - timedelta(minutes=10)
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=100,
                                            resets_at=int((self.now + timedelta(hours=2)).timestamp())))
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertIn("quota gate", d.reason)

    def test_quota_gate_waits_until_deadline(self):
        # deadline ahead -> NEVER attempt early: a resume before the window
        # actually resets appends another "usage limit" turn
        past = self.now - timedelta(minutes=10)
        deadline = self.now + timedelta(minutes=20)
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=100,
                                            resets_at=int(deadline.timestamp())))
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at, self.now)

    def test_quota_gate_advisory_after_deadline(self):
        # ordinaryUsageAllowed can lag reality: once the parsed reset time
        # has passed, attempt anyway -- a still-limited resume fails cheap
        past = self.now - timedelta(minutes=10)
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=100,
                                            resets_at=int((self.now - timedelta(minutes=5)).timestamp())))
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")

    def test_quota_gate_allows(self):
        past = self.now - timedelta(minutes=10)
        snap = QuotaSnapshot(ordinary_usage_allowed=True,
                             primary=Window(used_percent=10))
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")

    def test_future_reset_beats_trailing_99pct_snapshot(self):
        # live bug: the interruption says the 5h window is empty until its
        # reset, while the last snapshot still reads 99%. Resuming on that
        # number appended a second "you've reached your limit" turn.
        reset = self.now + timedelta(minutes=15)
        snap = QuotaSnapshot(ordinary_usage_allowed=True,
                             primary=Window(used_percent=99,
                                            resets_at=int(reset.timestamp())))
        d = plan_for_thread(self.event("usage_primary", reset),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at, reset)
        self.assertIn("interruption reset", d.reason)

    def test_goal_paired_reset_beats_trailing_99pct_snapshot(self):
        # goal rows have no reset time, but the paired failed turn does;
        # goal_limited inherits it and must not fire on a 99% snapshot either
        reset = self.now + timedelta(minutes=15)
        snap = QuotaSnapshot(ordinary_usage_allowed=True,
                             primary=Window(used_percent=99,
                                            resets_at=int(reset.timestamp())))
        ev = InterruptionEvent(thread_id="t1", kind="goal_limited",
                               reset_at=reset, source="synthetic",
                               goal_status="usage_limited")
        t = ThreadState(tracked=True, is_goal=True)
        d = plan_for_thread(ev, t, base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at, reset)

    def test_weekly_event_waits_even_with_5h_headroom(self):
        # a weekly-limit turn cannot be continued while the weekly window is
        # still empty, no matter how much 5h headroom the snapshot shows
        weekly_reset = self.now + timedelta(days=2)
        snap = QuotaSnapshot(ordinary_usage_allowed=True,
                             primary=Window(used_percent=20,
                                            resets_at=int((self.now + timedelta(hours=3)).timestamp())),
                             secondary=Window(used_percent=99,
                                              resets_at=int(weekly_reset.timestamp())))
        d = plan_for_thread(self.event("usage_weekly", weekly_reset),
                            ThreadState(tracked=True),
                            base_cfg(**{"general.weekly_resume": "all"}), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at, weekly_reset)

    def test_weekly_exhaustion_blocks_primary_headroom(self):
        # 5h refilled but the weekly window is empty: a resume fails instantly
        past = self.now - timedelta(minutes=10)
        snap = QuotaSnapshot(ordinary_usage_allowed=True,
                             primary=Window(used_percent=10,
                                            resets_at=int((self.now + timedelta(hours=4)).timestamp())),
                             secondary=Window(used_percent=100,
                                              resets_at=int((self.now + timedelta(days=3)).timestamp())))
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")

    def test_per_thread_cap(self):
        t = ThreadState(tracked=True, resume_count_today=12,
                        resume_count_day=self.now.strftime("%Y-%m-%d"))
        d = plan_for_thread(self.event("usage_primary", self.now - timedelta(hours=1)),
                            t, base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "skip")
        self.assertIn("cap", d.reason)

    def test_stale_thread_not_auto_tracked(self):
        old_ms = int((self.now - timedelta(hours=100)).timestamp() * 1000)
        d = plan_for_thread(self.event(), ThreadState(),
                            base_cfg(**{"general.auto_track": True}), {},
                            thread_last_update_ms=old_ms, busy=False,
                            now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "skip")

    def test_manual_overrides_stale(self):
        old_ms = int((self.now - timedelta(hours=100)).timestamp() * 1000)
        d = plan_for_thread(self.event("usage_primary", self.now - timedelta(hours=1)),
                            ThreadState(tracked=True, manual=True),
                            base_cfg(), {},
                            thread_last_update_ms=old_ms, busy=False,
                            now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")

    def test_cooldown_after_resume(self):
        t = ThreadState(tracked=True,
                        last_resume_iso=(self.now - timedelta(seconds=30)).isoformat())
        d = plan_for_thread(self.event("usage_primary", self.now - timedelta(hours=1)),
                            t, base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertIn("cooldown", d.reason)

    def test_disabled_thread_skipped(self):
        d = plan_for_thread(self.event(), ThreadState(tracked=True),
                            base_cfg(), {"enabled": False},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "skip")

    def test_parked_after_streak(self):
        t = ThreadState(tracked=True, fail_streak=6)
        d = plan_for_thread(self.event("usage_primary", self.now - timedelta(hours=1)),
                            t, base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "skip")
        self.assertIn("parked", d.reason)

    def test_overloaded_retries_after_cooldown(self):
        # "Selected model is at capacity" is not a task blockage: it must
        # become an actual retry, not a thread that is merely "tracked"
        d = plan_for_thread(self.event("server_overloaded"),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")
        self.assertIn("capacity", d.reason)
        self.assertIsNotNone(d.prompt)

    def test_overloaded_waits_between_attempts(self):
        # an attempt 60s ago is still inside the per-thread minimum spacing
        # (180s here), which wins over the shorter capacity backoff
        t = ThreadState(tracked=True,
                        last_resume_iso=(self.now - timedelta(seconds=60)).isoformat())
        d = plan_for_thread(self.event("server_overloaded"),
                            t, base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at,
                                self.now + timedelta(seconds=100))

    def test_overloaded_backoff_is_capped_at_three_minutes(self):
        # base 60s * 2^4 = 16 min uncapped; the retry ceiling is 3 minutes
        cfg = base_cfg(**{"general.min_resume_interval_sec": 30,
                          "general.server_overloaded_cooldown_sec": 60})
        t = ThreadState(tracked=True, overload_streak=5,
                        last_resume_iso=(self.now - timedelta(seconds=30)).isoformat())
        d = plan_for_thread(self.event("server_overloaded"),
                            t, cfg, {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        # capped cooldown 180s minus 30s already elapsed
        self.assertEqual(d.wake_at,
                         self.now + timedelta(seconds=150))

    def test_overloaded_quota_gate_still_applies(self):
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=100,
                                            resets_at=int((self.now + timedelta(hours=2)).timestamp())))
        d = plan_for_thread(self.event("server_overloaded"),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        self.assertGreaterEqual(d.wake_at,
                                self.now + timedelta(minutes=90))

    def test_overloaded_not_charged_to_daily_cap(self):
        today = self.now.strftime("%Y-%m-%d")
        t = ThreadState(tracked=True, resume_count_today=12,
                        resume_count_day=today)
        d = plan_for_thread(self.event("server_overloaded"),
                            t, base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=today)
        self.assertEqual(d.action, "due")

    def test_overloaded_wait_follows_capacity_backoff(self):
        # capacity retries are spaced by the 180s cooldown even when
        # min_resume_interval is lower
        cfg = base_cfg(**{"general.min_resume_interval_sec": 30})
        t = ThreadState(tracked=True, overload_streak=2,
                        last_resume_iso=(self.now - timedelta(seconds=60)).isoformat())
        d = plan_for_thread(self.event("server_overloaded"),
                            t, cfg, {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "wait")
        # 180s cooldown minus 60s already elapsed
        self.assertEqual(d.wake_at,
                         self.now + timedelta(seconds=120))

    def test_settings_precedence(self):
        cfg = base_cfg(**{"resume.model": "global-model",
                          "general.weekly_resume": "per-thread"})
        cfg["threads"]["t1"] = {"model": "thread-model",
                                "reasoning_effort": "high",
                                "weekly_resume": True}
        s = merge_settings(cfg, cfg["threads"]["t1"])
        self.assertEqual(s.model, "thread-model")
        self.assertEqual(s.reasoning_effort, "high")
        self.assertTrue(s.weekly_enabled)
        s2 = merge_settings(cfg, {})
        self.assertEqual(s2.model, "global-model")
        self.assertFalse(s2.weekly_enabled)

    def test_fallback_chain(self):
        cfg = base_cfg(**{"general.fallback_models": ["m-a", "m-b"]})
        self.assertEqual(pick_fallback_model(cfg, []), "m-a")
        self.assertEqual(pick_fallback_model(cfg, ["m-a"]), "m-b")
        self.assertIsNone(pick_fallback_model(cfg, ["m-a", "m-b"]))


if __name__ == "__main__":
    unittest.main()
