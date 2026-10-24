import os
import subprocess
import sys
import tempfile
import textwrap
import unittest


FAKE_CODEX = textwrap.dedent("""
    import json, sys
    argv = sys.argv[1:]
    if "--version" in argv:
        print("codex-cli 0.163.0-alpha.11.2 (fake)")
        sys.exit(0)
    if argv[:2] == ["app-server"]:
        # minimal newline-JSON app-server: initialize + rateLimits
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            msg = json.loads(line)
            if msg.get("method") == "initialize":
                resp = {"id": msg["id"], "result": {"authStatus": None}}
            elif msg.get("method") == "account/rateLimits/read":
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
            else:
                resp = {"id": msg.get("id"), "error": "unknown method"}
            sys.stdout.write(json.dumps(resp) + "\\n")
            sys.stdout.flush()
        sys.exit(0)
    # exec resume / queue: succeed
    print("resumed ok")
    sys.exit(0)
""")


class TestDaemonDryRun(unittest.TestCase):
    """Full daemon tick against a fixture CODEX_HOME and a fake codex."""

    def setUp(self):
        import time
        from tests.test_detect import build_codex_home, err_payload, NOW_S
        self.now = time.time()
        turns = [
            ("t-goal-thread", "turn-x", "failed",
             err_payload("usageLimitExceeded",
                         "You've hit your usage limit. ... try again at 12:00 AM."),
             NOW_S, int(self.now) - 3600),
        ]
        threads = [
            ("t-goal-thread", "r.jsonl", 1, 1, "user", "openai", "/w", "t",
             "read-only", "never", 0, 1, 0, "m", "low",
             int(self.now * 1000), "n"),
        ]
        goals = [("t-goal-thread", "g1", "obj", "usage_limited", None, 1, 1, 1, 2)]
        self.root = tempfile.mkdtemp(prefix="cxr-daemon-")
        home = build_codex_home(self.root, turns=turns, threads=threads,
                                goals=goals)
        self.home = home
        self.state_dir = os.path.join(self.root, "state")
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

    def test_one_tick_resumes(self):
        from tests.test_detect import err_payload  # noqa: F401
        cfg_path = os.path.join(self.root, "config.toml")
        with open(cfg_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""
                [general]
                codex_bin = "%s"
                codex_home = "%s"
                state_dir = "%s"
                poll_interval_sec = 5
                max_resumes_per_day = 5
                grace_after_reset_sec = 0
                jitter_sec = 0
                [limits_probe]
                use_app_server = true
            """ % (self.fake_codex.replace("\\", "\\\\"),
                   self.home.replace("\\", "\\\\"),
                   self.state_dir.replace("\\", "\\\\"))))
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONPATH"] = os.path.dirname(
            os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            [sys.executable, "-m", "codex_autoresume", "daemon", "--once",
             "-c", cfg_path],
            capture_output=True, text=True, env=env, cwd=self.root,
            timeout=120)
        combined = out.stdout + out.stderr
        self.assertEqual(out.returncode, 0, msg=combined[-3000:])
        # the interrupted thread must have been resumed and recorded
        from codex_autoresume.state import load_state
        st = load_state(os.path.join(self.state_dir, "state.json"))
        t = st.threads.get("t-goal-thread")
        self.assertIsNotNone(t)
        self.assertTrue(t.tracked)
        self.assertGreaterEqual(t.total_resumes, 1)
        self.assertEqual(t.last_outcome, "success")
        self.assertEqual(t.fail_streak, 0)


if __name__ == "__main__":
    unittest.main()
