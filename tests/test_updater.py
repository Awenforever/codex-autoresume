"""Self-update: version ordering, release feed, preflight and helper."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from codex_autoresume import updater


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                       text=True, encoding="utf-8", errors="replace")
    if check:
        assert r.returncode == 0, r.stderr
    return r


class VersionTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(updater.parse_version("v0.3.50"), (0, 3, 50))
        self.assertEqual(updater.parse_version("0.3.50"), (0, 3, 50))
        self.assertEqual(updater.parse_version(" v0.3.50-rc1 "), (0, 3, 50))
        self.assertIsNone(updater.parse_version("latest"))
        self.assertIsNone(updater.parse_version(""))

    def test_ordering(self):
        self.assertTrue(updater.is_newer("0.3.50", "0.3.49"))
        self.assertTrue(updater.is_newer("v0.4.0", "0.3.99"))
        self.assertFalse(updater.is_newer("0.3.49", "0.3.49"))
        self.assertFalse(updater.is_newer("0.3.48", "0.3.49"))
        self.assertFalse(updater.is_newer("0.3.50-rc1", "0.3.50"))
        self.assertFalse(updater.is_newer("nonsense", "0.3.49"))


class ReleaseFeedTest(unittest.TestCase):
    def test_parse_github_release(self):
        payload = {"tag_name": "v0.3.50", "html_url": "https://x/rel",
                   "published_at": "2026-10-27T00:00:00Z",
                   "name": "v0.3.50 notes"}
        with mock.patch("codex_autoresume.announcements._http_get",
                        return_value=json.dumps(payload)):
            rel = updater.fetch_latest_release("https://api/x")
        self.assertEqual(rel["version"], "0.3.50")
        self.assertEqual(rel["tag"], "v0.3.50")
        self.assertEqual(rel["url"], "https://x/rel")

    def test_rejects_bad_feed(self):
        with mock.patch("codex_autoresume.announcements._http_get",
                        return_value="not json"):
            with self.assertRaises(ValueError):
                updater.fetch_latest_release("https://api/x")
        with mock.patch("codex_autoresume.announcements._http_get",
                        return_value=json.dumps({"name": "nightly"})):
            with self.assertRaises(ValueError):
                updater.fetch_latest_release("https://api/x")


class PipSpecTest(unittest.TestCase):
    def test_pins_git_sources_to_the_release_tag(self):
        self.assertEqual(
            updater.resolve_pip_spec(
                "git+https://github.com/a/b", "v0.3.50"),
            "git+https://github.com/a/b@v0.3.50")

    def test_leaves_refs_and_local_paths_alone(self):
        self.assertEqual(
            updater.resolve_pip_spec(
                "git+https://github.com/a/b@main", "v0.3.50"),
            "git+https://github.com/a/b@main")
        self.assertEqual(updater.resolve_pip_spec("C:/src/repo", "v0.3.50"),
                         "C:/src/repo")
        self.assertEqual(updater.resolve_pip_spec("", "v0.3.50"), "")


class _GitCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cxr-upd-")
        self.remote = os.path.join(self.root, "remote.git")
        _git(self.root, "init", "--bare", "-b", "main", self.remote)
        self.seed = os.path.join(self.root, "seed")
        _git(self.root, "init", "-b", "main", self.seed)
        _git(self.seed, "config", "user.email", "t@example.com")
        _git(self.seed, "config", "user.name", "t")
        self._bump("0.3.49")
        _git(self.seed, "add", ".")
        _git(self.seed, "commit", "-m", "v0.3.49")
        _git(self.seed, "remote", "add", "origin", self.remote)
        _git(self.seed, "push", "-u", "origin", "main")
        self.repo = os.path.join(self.root, "repo")
        _git(self.root, "clone", self.remote, self.repo)

    def _bump(self, ver):
        _write(os.path.join(self.seed, "pyproject.toml"),
               "[project]\nversion = %r\n" % ver)
        _write(os.path.join(self.seed, "codex_autoresume", "__init__.py"),
               '__version__ = "%s"\n' % ver)

    @staticmethod
    def _cfg(repo):
        return {"update": {"enabled": True, "repo": repo,
                           "pip_source": "", "plugin_spec": ""}}


class PreflightTest(_GitCase):
    def test_clean_checkout_with_upstream_is_ready(self):
        ok, reason, src = updater.preflight(self._cfg(self.repo))
        self.assertTrue(ok, reason)
        self.assertEqual(src["kind"], "source")
        self.assertEqual(src["root"], os.path.abspath(self.repo))

    def test_dirty_checkout_is_refused(self):
        _write(os.path.join(self.repo, "README.md"), "local edit")
        ok, reason, _ = updater.preflight(self._cfg(self.repo))
        self.assertFalse(ok)
        self.assertIn("uncommitted", reason)

    def test_missing_repo_is_refused(self):
        ok, reason, _ = updater.preflight(
            self._cfg(os.path.join(self.root, "nope")))
        self.assertFalse(ok)
        self.assertIn("not found", reason)

    def test_pip_source_is_ready(self):
        cfg = {"update": {"enabled": True,
                          "pip_source": "git+https://github.com/a/b"}}
        with mock.patch("codex_autoresume.installer._repo_root",
                        return_value=None):
            ok, reason, src = updater.preflight(cfg)
        self.assertTrue(ok, reason)
        self.assertEqual(src["kind"], "pip")

    def test_restart_command_never_drives_a_sandbox(self):
        self.assertEqual(updater.restart_command(self.root), [])


class HelperTest(_GitCase):
    def _job(self, state_dir, pid, **over):
        job = {
            "pid": pid, "state_dir": state_dir,
            "log_path": os.path.join(state_dir, "update.log"),
            "result_path": os.path.join(state_dir, "update_last.json"),
            "job_path": os.path.join(state_dir, "update_job.json"),
            "kind": "source", "repo": self.repo, "pip_source": "",
            "python_exe": "", "codex_bin": "", "plugin_spec": "",
            "relaunch_cmd": [sys.executable, "-c",
                             "open(%r, 'w').write('x')"
                             % os.path.join(state_dir, "relaunched")],
            "restart_cmd": [], "from": "0.3.49", "to": "0.3.50",
            "tag": "v0.3.50", "wait_sec": 10,
        }
        job.update(over)
        os.makedirs(state_dir, exist_ok=True)
        with open(job["job_path"], "w", encoding="utf-8") as f:
            json.dump(job, f)
        return job

    @staticmethod
    def _dead_pid():
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait(timeout=30)
        return p.pid

    def test_helper_pulls_updates_version_and_relaunches(self):
        self._bump("0.3.50")
        _git(self.seed, "add", ".")
        _git(self.seed, "commit", "-m", "v0.3.50")
        _git(self.seed, "push", "origin", "main")
        state = os.path.join(self.root, "state")
        job = self._job(state, self._dead_pid())
        helper = updater.write_helper(state)
        r = subprocess.run([sys.executable, helper, job["job_path"]],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        last = updater.read_last(state)
        self.assertEqual(last["status"], "ok", last)
        self.assertEqual(last["version"], "0.3.50")
        with open(os.path.join(self.repo, "codex_autoresume",
                               "__init__.py"), encoding="utf-8") as f:
            self.assertIn('"0.3.50"', f.read())
        marker = os.path.join(state, "relaunched")
        for _ in range(50):
            if os.path.exists(marker):
                break
            time.sleep(0.1)
        self.assertTrue(os.path.exists(marker))
        self.assertFalse(os.path.exists(job["job_path"]))
        with open(job["log_path"], encoding="utf-8") as f:
            self.assertIn("finished: ok", f.read())

    def test_helper_refuses_to_touch_a_live_daemon(self):
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        try:
            state = os.path.join(self.root, "state2")
            job = self._job(state, proc.pid, wait_sec=0.5)
            helper = updater.write_helper(state)
            r = subprocess.run([sys.executable, helper, job["job_path"]],
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
            last = updater.read_last(state)
            self.assertEqual(last["status"], "failed")
            self.assertIn("did not exit", last["error"])
            self.assertFalse(os.path.exists(
                os.path.join(state, "relaunched")))
        finally:
            proc.kill()
            proc.wait(timeout=30)

    def test_helper_source_compiles(self):
        compile(updater._HELPER_SRC, "update_helper.py", "exec")

    def test_helper_forwards_live_system_proxy(self):
        import sys
        import types
        mod = types.ModuleType("helper")
        exec(compile(updater._HELPER_SRC, "update_helper.py", "exec"),
             mod.__dict__)
        if mod.os.name != "nt":
            self.skipTest("Windows registry proxy")

        class FakeKey:
            pass

        fake = types.ModuleType("winreg")
        fake.HKEY_CURRENT_USER = object()
        fake.OpenKey = lambda *a: FakeKey()
        fake.CloseKey = lambda k: None

        def query(key, name):
            if name == "ProxyEnable":
                return (1, 0)
            return ("127.0.0.1:9999", 1)

        fake.QueryValueEx = query
        old = sys.modules.get("winreg")
        cleared = {k: os.environ.pop(k) for k in
                   ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
                   if k in os.environ}
        sys.modules["winreg"] = fake
        try:
            env = mod._proxy_env()
        finally:
            if old is None:
                del sys.modules["winreg"]
            else:
                sys.modules["winreg"] = old
            os.environ.update(cleared)
        self.assertEqual(env["HTTPS_PROXY"], "http://127.0.0.1:9999")
        self.assertEqual(env["http_proxy"], "http://127.0.0.1:9999")
        # -c http.proxy=... beats a stale http.proxy in the git config
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "http.proxy")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "http://127.0.0.1:9999")


class DaemonUpdateTest(unittest.TestCase):
    def _daemon(self, **update_cfg):
        from codex_autoresume.daemon import Daemon
        d = object.__new__(Daemon)
        d.cfg = {"update": {"enabled": True, "check_sec": 3600,
                            "release_url": "https://api/x",
                            "timeout_sec": 1, **update_cfg}}
        d._update = None
        d._update_at = 0.0
        d._update_started = False
        d._update_last = {"status": "ok", "to": "0.3.49", "at": "x"}
        d._inflight = {}
        return d

    def test_available_flag_and_reason(self):
        from codex_autoresume import __version__ as current
        parts = [int(x) for x in current.split(".")]
        parts[-1] += 1
        nxt = ".".join(str(x) for x in parts)
        d = self._daemon()
        rel = {"version": nxt, "tag": "v" + nxt, "url": "",
               "published_at": ""}
        with mock.patch.object(updater, "fetch_latest_release",
                               return_value=rel):
            with mock.patch.object(updater, "preflight",
                                   return_value=(False, "dirty", None)):
                info = d.update_info(force=True)
        self.assertTrue(info["available"])
        self.assertEqual(info["latest"], nxt)
        self.assertFalse(info["can_update"])
        self.assertEqual(info["reason"], "dirty")
        self.assertEqual(info["last"]["to"], "0.3.49")
        self.assertFalse(info["in_progress"])

    def test_check_error_is_cached_not_raised(self):
        d = self._daemon()
        with mock.patch.object(updater, "fetch_latest_release",
                               side_effect=OSError("offline")):
            info = d.update_info(force=True)
        self.assertFalse(info["available"])
        self.assertIn("offline", info["error"])
        # the failure is cached for check_sec (tick must not refetch)
        with mock.patch.object(updater, "fetch_latest_release",
                               side_effect=AssertionError("refetched")):
            again = d.update_info()
        self.assertEqual(again["error"], info["error"])

    def test_force_bypasses_the_check_interval(self):
        from codex_autoresume import __version__ as current
        d = self._daemon()
        d._update = {"enabled": True, "current": current, "latest": None,
                     "available": False, "can_update": False,
                     "reason": "", "error": ""}
        d._update_at = time.monotonic()      # cached seconds ago
        rel = {"version": current, "tag": "v" + current, "url": "",
               "published_at": ""}
        with mock.patch.object(updater, "fetch_latest_release",
                               return_value=rel) as fetch:
            d.update_info()
        fetch.assert_not_called()
        with mock.patch.object(updater, "fetch_latest_release",
                               return_value=rel) as fetch:
            d.update_info(force=True)
        fetch.assert_called_once()

    def test_begin_update_refuses_when_current(self):
        d = self._daemon()
        d._update = {"enabled": True, "current": "0.3.49",
                     "latest": "0.3.49", "available": False,
                     "can_update": False, "in_progress": False}
        d._update_at = time.monotonic()
        self.assertEqual(d.begin_update(),
                         {"started": False,
                          "error": "this install is already up to date"})


if __name__ == "__main__":
    unittest.main()
