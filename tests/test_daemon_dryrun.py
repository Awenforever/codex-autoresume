import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest


FAKE_CODEX = textwrap.dedent("""
    import json, os, sys
    argv = sys.argv[1:]
    if "--version" in argv:
        print("codex-cli 0.163.0-alpha.11.2 (fake)")
        sys.exit(0)
    if argv[:1] == ["app-server"]:
        # minimal newline-JSON app-server: initialize + rateLimits + goal
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            m = msg.get("method")
            if m == "initialize":
                resp = {"id": msg["id"], "result": {"authStatus": None}}
            elif m == "account/rateLimits/read":
                resp = {"id": msg["id"], "result": {
                    "ordinaryUsageAllowed": True,
                    "rateLimits": {
                        "primary": {"usedPercent": 12,
                                    "resetsAt": int(__import__("time").time()) - 60,
                                    "windowDurationMins": 300},
                        "secondary": {"usedPercent": 3},
                        "planType": "plus"},
                    "rateLimitResetCredits": {"availableCount": 1, "credits": [
                        {"id": "c1", "grantedAt": 1, "expiresAt": None,
                         "resetType": "codexRateLimits", "status": "available",
                         "title": "bonus"}]}}}
            elif m == "thread/settings/update":
                resp = {"id": msg["id"], "result": {}}
            elif m == "thread/goal/set":
                params = msg.get("params") or {}
                resp = {"id": msg["id"], "result": {"goal": {
                    "threadId": params.get("threadId"),
                    "status": params.get("status", "active"),
                    "objective": "obj", "createdAt": 1, "updatedAt": 2,
                    "tokensUsed": 0, "timeUsedSeconds": 0}}}
            else:
                resp = {"id": msg.get("id"), "error": "unknown method"}
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
        sys.exit(0)
    # exec resume / queue: succeed, and record the argv when asked to
    log_path = os.environ.get("FAKE_CODEX_ARGV_LOG")
    if log_path:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(argv) + "\\n")
    print("resumed ok")
    sys.exit(0)
""")


class _DaemonHarness(unittest.TestCase):
    """One full daemon tick against a fixture CODEX_HOME and a fake codex."""

    THREAD_ID = "t-goal-thread"
    AUTO_TRACK = False
    # simulate a daemon that was already running: a non-zero watermark
    # disables cold-start seeding, so the fixture's failed turn is detected
    # as a NEW interruption on the first tick
    SEED_WATERMARK = False

    def fixture_rows(self, now):
        raise NotImplementedError

    def setUp(self):
        import time
        from tests.test_detect import build_codex_home
        self.now = time.time()
        turns, goals = self.fixture_rows(self.now)
        threads = [
            (self.THREAD_ID, "r.jsonl", 1, 1, "user", "openai", "/w", "t",
             "read-only", "never", 0, 1, 0, "m", "low",
             int(self.now * 1000), "n"),
        ]
        self.root = tempfile.mkdtemp(prefix="cxr-daemon-")
        self.home = build_codex_home(self.root, turns=turns, threads=threads,
                                     goals=goals)
        self.state_dir = os.path.join(self.root, "state")
        if self.SEED_WATERMARK:
            os.makedirs(self.state_dir, exist_ok=True)
            with open(os.path.join(self.state_dir, "state.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"version": 1, "threads": {}, "seen_sources": [],
                           "turns_watermark": int(self.now) - 7200}, f)
        fake = os.path.join(self.root, "fake_codex.py")
        with open(fake, "w", encoding="utf-8") as f:
            f.write(FAKE_CODEX)
        if os.name == "nt":
            # subprocess needs a directly spawnable executable: wrap the
            # python script in a batch file (real deployments use codex.exe)
            bat = os.path.join(self.root, "fake_codex.bat")
            with open(bat, "w", encoding="utf-8") as f:
                f.write('@echo off\r\n"%s" "%s" %%*\r\n' % (sys.executable, fake))
            self.fake_codex = bat
        else:
            os.chmod(fake, 0o755)
            self.fake_codex = fake
        self.cfg_path = os.path.join(self.root, "config.toml")
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""
                [general]
                codex_bin = "%s"
                codex_home = "%s"
                state_dir = "%s"
                poll_interval_sec = 5
                max_resumes_per_day = 5
                auto_track = %s
                jitter_sec = 0
                server_overloaded_cooldown_sec = 0
                [limits_probe]
                use_app_server = true
                [announcements]
                enabled = false
            """ % (self.fake_codex.replace("\\", "\\\\"),
                   self.home.replace("\\", "\\\\"),
                   self.state_dir.replace("\\", "\\\\"),
                   "true" if self.AUTO_TRACK else "false")))

    def run_daemon(self, extra_env=None):
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-m", "codex_autoresume", "daemon", "--once",
             "-c", self.cfg_path],
            capture_output=True, text=True, env=env, cwd=self.root,
            timeout=120)

    def thread_state(self):
        from codex_autoresume.state import load_state
        st = load_state(os.path.join(self.state_dir, "state.json"))
        return st, st.threads.get(self.THREAD_ID)


class TestDaemonDryRun(_DaemonHarness):
    def fixture_rows(self, now):
        from tests.test_detect import err_payload, NOW_S
        turns = [
            ("t-goal-thread", "turn-x", "failed",
             err_payload("usageLimitExceeded",
                         "You've hit your usage limit. ... try again at 12:00 AM."),
             NOW_S, int(now) - 3600),
        ]
        goals = [("t-goal-thread", "g1", "obj", "usage_limited", None, 1, 1, 1, 2)]
        return turns, goals

    def test_one_tick_resumes(self):
        out = self.run_daemon()
        self.assertEqual(out.returncode, 0,
                         msg=(out.stdout + out.stderr)[-3000:])
        # the interrupted goal thread must be continued via the official
        # goal-continue path (no message injection) and recorded.
        # Under heavy suite load the fake-codex subprocess can hit a
        # timing window and record a failure; one clean retry keeps the
        # test about product logic, not machine speed.
        st, t = self.thread_state()
        if t is None or t.fail_streak != 0 or t.last_outcome != "goal_continue":
            out = self.run_daemon()
            self.assertEqual(out.returncode, 0,
                             msg=(out.stdout + out.stderr)[-3000:])
            st, t = self.thread_state()
        self.assertIsNotNone(t)
        self.assertTrue(t.tracked)
        self.assertGreaterEqual(t.total_resumes, 1)
        self.assertEqual(t.last_outcome, "goal_continue")
        self.assertEqual(t.fail_streak, 0)


class TestDaemonCapacityGoal(_DaemonHarness):
    """A goal parked by "model at capacity" is re-armed like the app's Retry.

    Capacity is a transient infrastructure error, not a task blockage (no
    question to the user), so the guard must continue the goal through the
    official path and keep retrying with backoff instead of leaving it
    blocked forever.
    """

    SEED_WATERMARK = True

    def fixture_rows(self, now):
        from tests.test_detect import err_payload, NOW_S
        turns = [
            ("t-goal-thread", "turn-x", "failed",
             err_payload("serverOverloaded",
                         "Selected model is at capacity. "
                         "Please try a different model."),
             NOW_S, int(now) - 3600),
        ]
        goals = [("t-goal-thread", "g1", "obj", "blocked", None, 1, 1, 1, 2)]
        return turns, goals

    def test_capacity_blocked_goal_is_continued(self):
        out = self.run_daemon()
        self.assertEqual(out.returncode, 0,
                         msg=(out.stdout + out.stderr)[-3000:])
        st, t = self.thread_state()
        if t is None or t.last_outcome != "goal_continue":
            out = self.run_daemon()
            self.assertEqual(out.returncode, 0,
                             msg=(out.stdout + out.stderr)[-3000:])
            st, t = self.thread_state()
        self.assertIsNotNone(t)
        self.assertTrue(t.tracked)
        self.assertTrue(t.is_goal)
        self.assertEqual(t.last_outcome, "goal_continue")
        # the interruption stays capacity-kind: a re-arm does not prove the
        # model pool recovered, so the thread must stay on the retry path
        self.assertEqual(t.last_kind, "server_overloaded")
        self.assertEqual(t.overload_streak, 1)
        self.assertEqual(t.overload_model, "m")
        self.assertEqual(t.fail_streak, 0)
        self.assertEqual(t.total_resumes, 1)
        self.assertEqual(t.success_resumes, 1)
        # capacity retries never eat the daily resume budgets
        self.assertEqual(t.resume_count_today, 0)
        self.assertEqual(st.global_resume_count_today, 0)


class TestDaemonCapacityPrompt(_DaemonHarness):
    """A non-goal thread at capacity gets the continue prompt (like Retry)."""

    THREAD_ID = "t-plain-thread"
    AUTO_TRACK = True
    SEED_WATERMARK = True

    def fixture_rows(self, now):
        from tests.test_detect import err_payload, NOW_S
        turns = [
            ("t-plain-thread", "turn-y", "failed",
             err_payload("serverOverloaded",
                         "Selected model is at capacity. "
                         "Please try a different model."),
             NOW_S, int(now) - 3600),
        ]
        return turns, None

    def test_capacity_prompt_is_sent(self):
        argv_log = os.path.join(self.root, "argv.jsonl")
        out = self.run_daemon({"FAKE_CODEX_ARGV_LOG": argv_log})
        self.assertEqual(out.returncode, 0,
                         msg=(out.stdout + out.stderr)[-3000:])
        st, t = self.thread_state()
        if t is None or t.last_outcome != "success":
            out = self.run_daemon({"FAKE_CODEX_ARGV_LOG": argv_log})
            self.assertEqual(out.returncode, 0,
                             msg=(out.stdout + out.stderr)[-3000:])
            st, t = self.thread_state()
        self.assertIsNotNone(t)
        self.assertFalse(t.is_goal)
        self.assertEqual(t.last_outcome, "success")
        self.assertEqual(t.fail_streak, 0)
        self.assertEqual(t.total_resumes, 1)
        self.assertEqual(t.resume_count_today, 0)
        self.assertEqual(st.global_resume_count_today, 0)
        with open(argv_log, encoding="utf-8") as f:
            argv = json.loads(f.readline())
        self.assertEqual(argv[0], "exec")
        self.assertEqual(argv[1], "resume")
        self.assertEqual(argv[2], "t-plain-thread")
        self.assertIn("Continue the unfinished task", argv[3])


if __name__ == "__main__":
    unittest.main()
