import json
import os
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta

from codex_autoresume.state import ThreadState, WatcherState, load_state


class _StubDaemon:
    """Minimal daemon surface the ControlApi needs."""

    def __init__(self, state_dir, threads):
        self.cfg = {
            "general": {"dry_run": True},
            "control_api": {"enabled": True, "host": "127.0.0.1", "port": 0},
            "resume": {"prompt_generic": "默认继续指令"},
        }
        self.state_path = os.path.join(state_dir, "state.json")
        self.state = WatcherState(state_path=self.state_path)
        for tid, t in threads.items():
            self.state.threads[tid] = t
        self.codex_home = None
        self._snapshot = None
        self._state_mtime = 0.0
        self._threads_meta = threads   # id -> dict(title=, updated_ms=)

    def _effective_thread_config(self, thread_id, t):
        tc = {"weekly_resume": False, "model": None, "reasoning_effort": None}
        if t is not None:
            if t.guard_weekly is not None:
                tc["weekly_resume"] = t.guard_weekly
            if t.guard_model:
                tc["model"] = t.guard_model
            if t.guard_effort:
                tc["reasoning_effort"] = t.guard_effort
        return tc

    def model_catalog(self):
        return [{"id": "gpt-6.1-sol", "name": "GPT-6.1 Sol"},
                {"id": "gpt-5.6-terra", "name": "GPT-5.6 Terra"}]

    def announcement(self):
        return {"url": "https://example.com", "latest_reset": None,
                "landing": None, "fetched_at": None}


class TestControlApi(unittest.TestCase):
    def setUp(self):
        from tests.test_detect import build_codex_home
        self.root = tempfile.mkdtemp(prefix="cxr-ctl-")
        home = build_codex_home(self.root, threads=[
            ("t-alpha", "r.jsonl", 1, 1, "user", "openai", "/w",
             "Hermes 接手", "ro", "never", 0, 1, 0, "m", "low",
             int(datetime.now().timestamp() * 1000), "Alpha"),
        ])
        t = ThreadState(tracked=False)
        self.daemon = _StubDaemon(self.root, {"t-alpha": t})
        self.daemon.codex_home = home
        from codex_autoresume.control import ControlApi
        self.api = ControlApi(self.daemon)
        self.api.start("127.0.0.1", 0)
        port = self.api.httpd.server_address[1]
        self.base = "http://127.0.0.1:%d" % port

    def tearDown(self):
        self.api.stop()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return json.loads(r.read().decode())

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode())

    def test_status_lists_threads(self):
        s = self._get("/api/status")
        self.assertTrue(s["dry_run"])
        self.assertFalse(s["paused"])
        alpha = [t for t in s["threads"] if t["id"] == "t-alpha"]
        self.assertEqual(len(alpha), 1)
        self.assertFalse(alpha[0]["guard"])

    def test_set_guard_toggles_and_persists(self):
        out = self._post("/api/threads/t-alpha",
                         {"guard": True, "weekly": True,
                          "model": "gpt-6.1-sol", "effort": "high"})
        self.assertTrue(out["guard"])
        st = load_state(self.daemon.state_path)
        t = st.threads["t-alpha"]
        self.assertTrue(t.tracked and t.manual)
        self.assertTrue(t.guard_weekly)
        self.assertEqual(t.guard_model, "gpt-6.1-sol")
        self.assertEqual(t.guard_effort, "high")
        s = self._get("/api/status")
        alpha = [x for x in s["threads"] if x["id"] == "t-alpha"][0]
        self.assertTrue(alpha["guard"])
        self.assertTrue(alpha["weekly"])
        self.assertEqual(alpha["model"], "gpt-6.1-sol")

    def test_pause_endpoint(self):
        self._post("/api/pause", {"paused": True})
        self.assertTrue(self._get("/api/status")["paused"])

    def test_panel_served(self):
        with urllib.request.urlopen(self.base + "/", timeout=10) as r:
            html = r.read().decode()
        self.assertIn("codex-autoresume", html)
        self.assertIn("守护", html)


class TestMcpTools(unittest.TestCase):
    def setUp(self):
        from tests.test_detect import build_codex_home
        self.root = tempfile.mkdtemp(prefix="cxr-mcp-")
        home = build_codex_home(self.root, threads=[
            ("t-beta", "r.jsonl", 1, 1, "user", "openai", "/w",
             "Sentinel 批量转换任务", "ro", "never", 0, 1, 0, "m", "low",
             int(datetime.now().timestamp() * 1000), "Beta"),
        ])
        daemon = _StubDaemon(self.root, {"t-beta": ThreadState()})
        daemon.codex_home = home
        from codex_autoresume.control import ControlApi
        self.api = ControlApi(daemon)
        self.api.start("127.0.0.1", 0)
        self.base = "http://127.0.0.1:%d" % self.api.httpd.server_address[1]

    def tearDown(self):
        self.api.stop()

    def test_status_tool(self):
        from codex_autoresume.mcp_server import tool_call
        out = tool_call("autoresume_status", {}, base=self.base)
        self.assertIn("threads", out)
        self.assertTrue(any(t["id"] == "t-beta" for t in out["threads"]))

    def test_set_guard_by_title(self):
        from codex_autoresume.mcp_server import tool_call
        out = tool_call("autoresume_set_guard",
                        {"guard": True, "title_contains": "beta",
                         "weekly": False, "model": "", "effort": "high"},
                        base=self.base)
        self.assertEqual(out.get("thread"), "t-beta")
        st = load_state(os.path.join(self.root, "state.json"))
        self.assertTrue(st.threads["t-beta"].tracked)

    def test_unknown_tool(self):
        from codex_autoresume.mcp_server import tool_call
        out = tool_call("nope", {}, base=self.base)
        self.assertIn("error", out)


if __name__ == "__main__":
    unittest.main()
