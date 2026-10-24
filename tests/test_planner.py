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
        self.assertEqual(d.action, "skip")
        self.assertIn("weekly", d.reason)

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

    def test_quota_gate_attempts_when_deadline_near(self):
        # deadline inside the attempt window -> try even if the flag lags
        past = self.now - timedelta(minutes=10)
        snap = QuotaSnapshot(ordinary_usage_allowed=False,
                             primary=Window(used_percent=100,
                                            resets_at=int((self.now + timedelta(minutes=20)).timestamp())))
        d = plan_for_thread(self.event("usage_primary", past),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=snap,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "due")

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

    def test_overloaded_tracks_with_cooldown(self):
        d = plan_for_thread(self.event("server_overloaded"),
                            ThreadState(tracked=True), base_cfg(), {},
                            thread_last_update_ms=int(self.now.timestamp() * 1000),
                            busy=False, now=self.now, snapshot=None,
                            today=self.now.strftime("%Y-%m-%d"))
        self.assertEqual(d.action, "track")

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
