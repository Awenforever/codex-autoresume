import unittest

from codex_autoresume.announcements import fetch_announcement


class _Resp:
    def __init__(self, body):
        self._body = body.encode("utf-8")

    def read(self, n=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


PAGE = """
<html><body>
<p>The tracker also has a <b>landing page</b> in its own right (a looping
sample of the list, with the sign-in button under it) for convenience.</p>
<div class="banner">Landed as announced, around 10:00 Pacific Time.</div>
<div>Latest recorded reset: <time datetime="2026-10-23T17:00Z">
Fri, Oct 23, 2026, 17:00 UTC</time> Reset</div>
<blockquote>"Landing tomorrow before 10am PST together with a small ship."</blockquote>
</body></html>
"""


class TestAnnouncements(unittest.TestCase):
    def test_parse(self):
        import urllib.request
        orig = urllib.request.urlopen

        def fake_urlopen(req, timeout=0):
            return _Resp(PAGE)

        urllib.request.urlopen = fake_urlopen
        try:
            out = fetch_announcement()
        finally:
            urllib.request.urlopen = orig
        self.assertIn("Oct 23, 2026", out["latest_reset"])
        self.assertTrue(out["latest_reset_local"])    # converted to local tz
        # banner says "Landed as announced" -> landed wins, stale forecast
        # from the quote is suppressed
        self.assertEqual(out["status_kind"], "landed")
        self.assertIn("Landed as announced", out["status"])
        # display text stays VERBATIM (no machine-tz conversion)
        self.assertNotIn("本机", out["status"])
        self.assertIn("UTC", out["latest_reset_local"])
        self.assertIsNone(out["landing"])

    def test_parse_upcoming(self):
        import urllib.request
        page = PAGE.replace(
            "Landed as announced, around 10:00 Pacific Time.", "")
        orig = urllib.request.urlopen

        def fake_urlopen(req, timeout=0):
            return _Resp(page)

        urllib.request.urlopen = fake_urlopen
        try:
            out = fetch_announcement()
        finally:
            urllib.request.urlopen = orig
        self.assertEqual(out["status_kind"], "upcoming")
        self.assertIn("before 10am PST", out["landing"])
        # the prose "landing page" phrase must NOT be picked up
        self.assertNotIn("landing page", out["landing"].lower())

    def test_never_raises(self):
        import urllib.request

        def boom(req, timeout=0):
            raise OSError("network down")

        orig = urllib.request.urlopen
        urllib.request.urlopen = boom
        try:
            out = fetch_announcement()
        finally:
            urllib.request.urlopen = orig
        self.assertIsNone(out["latest_reset"])
        self.assertIsNone(out["landing"])


if __name__ == "__main__":
    unittest.main()
