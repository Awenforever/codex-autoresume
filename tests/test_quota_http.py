import json
import os
import tempfile
import unittest


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


USAGE = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 63, "reset_at": 1792850000,
                           "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 58, "reset_at": 1793382104,
                             "limit_window_seconds": 604800},
    },
}


class TestQuotaHttp(unittest.TestCase):
    def _home(self):
        d = tempfile.mkdtemp(prefix="cxr-http-")
        with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as f:
            json.dump({"tokens": {"access_token": "tok-xyz",
                                  "account_id": "acct-1"},
                       "auth_mode": "chatgpt"}, f)
        return d

    def test_normalization(self):
        import urllib.request
        from codex_autoresume.ratelimits import fetch_quota_http
        home = self._home()
        calls = []

        def fake(req, timeout=0):
            calls.append(req.full_url)
            return _Resp(USAGE)

        # the fetch goes through a proxy-aware opener; route the fake in
        # by stubbing build_opener (returns an object whose open() is fake)
        class _FakeOpener:
            def open(self, req, timeout=0):
                return fake(req, timeout)

        orig = urllib.request.build_opener
        urllib.request.build_opener = lambda *a, **k: _FakeOpener()
        try:
            snap = fetch_quota_http(home)
        finally:
            urllib.request.build_opener = orig
        self.assertTrue(snap.ordinary_usage_allowed)
        self.assertEqual(snap.primary.used_percent, 63)
        self.assertEqual(snap.primary.resets_at, 1792850000)
        self.assertEqual(snap.primary.window_duration_mins, 300)
        self.assertEqual(snap.secondary.used_percent, 58)
        self.assertEqual(snap.plan_type, "plus")
        self.assertEqual(calls[0],
                         "https://chatgpt.com/backend-api/wham/usage")

    def test_missing_token_raises(self):
        from codex_autoresume.ratelimits import fetch_quota_http, RateLimitError
        d = tempfile.mkdtemp(prefix="cxr-http2-")
        with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as f:
            json.dump({"tokens": {}}, f)
        with self.assertRaises(RateLimitError):
            fetch_quota_http(d)

    def test_models_from_cache(self):
        from codex_autoresume.ratelimits import models_from_cache
        d = tempfile.mkdtemp(prefix="cxr-models-")
        with open(os.path.join(d, "models_cache.json"), "w",
                  encoding="utf-8") as f:
            json.dump({"models": [
                {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol",
                 "visibility": "public"},
                {"slug": "secret-model", "display_name": "Secret",
                 "visibility": "hidden"},
                {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra"},
            ]}, f)
        out = models_from_cache(d)
        self.assertEqual([m["id"] for m in out],
                         ["gpt-6-astra", "gpt-6.1-sol"])   # hidden skipped
        self.assertIsNone(models_from_cache(os.path.join(d, "nope")))


if __name__ == "__main__":
    unittest.main()
