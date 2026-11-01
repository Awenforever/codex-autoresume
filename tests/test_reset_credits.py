import json
import os
import tempfile
import unittest

USAGE = {
    "plan_type": "plus",
    "rate_limit": {
        "allowed": True,
        "limit_reached": False,
        "primary_window": {"used_percent": 0, "reset_at": 1793517626,
                           "limit_window_seconds": 18000},
        "secondary_window": {"used_percent": 100, "reset_at": 1793774170,
                             "limit_window_seconds": 604800},
    },
    "rate_limit_reset_credits": {"available_count": 1,
                                 "applicable_available_count": 1},
}

CREDITS = {
    "available_count": 1,
    "total_earned_count": 0,
    "history_enabled": True,
    "credits": [{
        "id": "RateLimitResetCredit_x",
        "reset_type": "codex_rate_limits",
        "is_supported_by_plan": True,
        "status": "available",
        "granted_at": "2026-10-31T17:36:09.289698Z",
        "expires_at": "2026-11-30T17:36:09.289698Z",
        "title": "bonus",
    }],
}


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _home():
    d = tempfile.mkdtemp(prefix="cxr-credits-")
    with open(os.path.join(d, "auth.json"), "w", encoding="utf-8") as f:
        json.dump({"tokens": {"access_token": "tok", "account_id": "acct"}}, f)
    return d


def _fetch(routes):
    import urllib.error
    import urllib.request
    from codex_autoresume.ratelimits import fetch_quota_http

    class _Opener:
        def open(self, req, timeout=0):
            body = routes.get(req.full_url)
            if isinstance(body, Exception):
                raise body
            if body is None:
                raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)
            return _Resp(body)

    orig = urllib.request.build_opener
    urllib.request.build_opener = lambda *a, **k: _Opener()
    try:
        return fetch_quota_http(_home())
    finally:
        urllib.request.build_opener = orig


class TestHttpResetCredits(unittest.TestCase):
    def test_iso_timestamps_and_count(self):
        from codex_autoresume.ratelimits import WHAM_CREDITS_URL, WHAM_USAGE_URL
        snap = _fetch({WHAM_USAGE_URL: USAGE, WHAM_CREDITS_URL: CREDITS})
        self.assertEqual(snap.reset_credits_count, 1)
        self.assertEqual(len(snap.reset_credits), 1)
        c = snap.reset_credits[0]
        self.assertEqual(c.granted_at, 1793468169)
        self.assertEqual(c.expires_at, 1796060169)
        self.assertEqual(c.status, "available")
        self.assertTrue(c.available)
        self.assertEqual(snap.available_reset_credits, [c])

    def test_detail_endpoint_failure_keeps_usage_count(self):
        import urllib.error
        from codex_autoresume.ratelimits import WHAM_CREDITS_URL, WHAM_USAGE_URL
        snap = _fetch({WHAM_USAGE_URL: USAGE,
                       WHAM_CREDITS_URL: urllib.error.HTTPError(
                           WHAM_CREDITS_URL, 500, "boom", {}, None)})
        # the usage payload is authoritative: cards stay visible even when
        # the detail endpoint is down
        self.assertEqual(snap.reset_credits_count, 1)
        self.assertEqual(snap.reset_credits, [])

    def test_partial_detail_list_uses_available_count(self):
        from codex_autoresume.ratelimits import WHAM_CREDITS_URL, WHAM_USAGE_URL
        partial = dict(CREDITS, available_count=3)
        snap = _fetch({WHAM_USAGE_URL: USAGE, WHAM_CREDITS_URL: partial})
        self.assertEqual(len(snap.reset_credits), 1)
        self.assertEqual(snap.reset_credits_count, 3)

    def test_missing_usage_summary_falls_back_to_detail(self):
        from codex_autoresume.ratelimits import WHAM_CREDITS_URL, WHAM_USAGE_URL
        usage = {k: v for k, v in USAGE.items()
                 if k != "rate_limit_reset_credits"}
        snap = _fetch({WHAM_USAGE_URL: usage, WHAM_CREDITS_URL: CREDITS})
        self.assertEqual(snap.reset_credits_count, 1)


class TestAppServerResetCredits(unittest.TestCase):
    def _resp(self, summary):
        return {
            "ordinaryUsageAllowed": False,
            "rateLimits": {"primary": {"usedPercent": 0},
                           "secondary": {"usedPercent": 100}},
            "rateLimitResetCredits": summary,
        }

    def test_available_count_wins_over_list_length(self):
        from codex_autoresume.ratelimits import QuotaSnapshot
        snap = QuotaSnapshot.from_response(self._resp({
            "availableCount": 2,
            "credits": [{"id": "a", "grantedAt": 1, "expiresAt": None,
                         "status": "available", "resetType": "codex"}],
        }))
        self.assertEqual(len(snap.reset_credits), 1)
        self.assertEqual(snap.reset_credits_count, 2)

    def test_fallback_counts_only_available_statuses(self):
        from codex_autoresume.ratelimits import QuotaSnapshot
        snap = QuotaSnapshot.from_response(self._resp({
            "credits": [
                {"id": "a", "grantedAt": 1, "status": "available"},
                {"id": "b", "grantedAt": 2, "status": "redeemed"},
                {"id": "c", "grantedAt": 3, "status": "expired"},
                {"id": "d", "grantedAt": 4, "status": None},
            ],
        }))
        self.assertEqual([c.id for c in snap.available_reset_credits],
                         ["a", "d"])
        self.assertEqual(snap.reset_credits_count, 2)

    def test_nearest_expiry_ignores_used_credits(self):
        from codex_autoresume.ratelimits import QuotaSnapshot
        snap = QuotaSnapshot.from_response(self._resp({
            "availableCount": 1,
            "credits": [
                {"id": "old", "grantedAt": 1, "expiresAt": 100,
                 "status": "redeemed"},
                {"id": "live", "grantedAt": 2, "expiresAt": 500,
                 "status": "available"},
            ],
        }))
        live = snap.available_reset_credits
        self.assertEqual([c.id for c in live], ["live"])
        self.assertEqual(min(c.expires_at for c in live if c.expires_at), 500)

    def test_iso_timestamps_from_app_server(self):
        from codex_autoresume.ratelimits import QuotaSnapshot
        snap = QuotaSnapshot.from_response(self._resp({
            "availableCount": 1,
            "credits": [{"id": "a",
                         "grantedAt": "2026-10-31T17:36:09.289698Z",
                         "expiresAt": "2026-11-30T17:36:09.289698Z",
                         "status": "available"}],
        }))
        self.assertEqual(snap.reset_credits[0].granted_at, 1793468169)
        self.assertEqual(snap.reset_credits[0].expires_at, 1796060169)


class TestEpoch(unittest.TestCase):
    def test_formats(self):
        from codex_autoresume.ratelimits import _epoch
        expected = 1793468169
        self.assertEqual(_epoch(expected), expected)
        self.assertEqual(_epoch(str(expected)), expected)
        self.assertEqual(_epoch(1793468169289), expected)
        self.assertEqual(_epoch(1793468169.9), expected)
        self.assertEqual(_epoch("2026-10-31T17:36:09Z"), expected)
        self.assertEqual(_epoch("2026-10-31T17:36:09.289698Z"), expected)
        self.assertEqual(_epoch("2026-11-01T01:36:09+08:00"), expected)

    def test_invalid(self):
        from codex_autoresume.ratelimits import _epoch, _count
        self.assertIsNone(_epoch(None))
        self.assertIsNone(_epoch(""))
        self.assertIsNone(_epoch("not-a-time"))
        self.assertIsNone(_epoch(True))
        self.assertIsNone(_count(None))
        self.assertIsNone(_count(-1))
        self.assertEqual(_count("3"), 3)


if __name__ == "__main__":
    unittest.main()
