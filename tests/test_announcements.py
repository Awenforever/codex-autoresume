import json
import unittest
from unittest import mock

from codex_autoresume import announcements as mod
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
<article>
<time class="cx-post-t" datetime="2026-10-23T23:47:00Z">2h</time>
<blockquote class="cx-quote" cite="https://x.com/thsottiaux/status/2114400000000000000">
<p>Landing tomorrow before 10am PST together with a small ship.</p>
</blockquote>
</article>
</body></html>
"""

FEED = {
    "fetched_at": "2026-10-27T03:35:00Z",
    "stale": False,
    "tweets": [
        {"id": "2114800277677092936", "at": "2026-10-26T19:24:11Z",
         "text": "Day 22/ Terra is back! Shipped a new model to everyone.",
         "url": "https://x.com/thsottiaux/status/2114800277677092936",
         "kind": "boost", "is_reply": False},
        {"id": "2114588174249071102", "at": "2026-10-26T05:21:22Z",
         "text": "Day 21/ today is a reset.",
         "url": "https://x.com/thsottiaux/status/2114588174249071102",
         "kind": "signal", "is_reply": False},
        {"id": "2114000000000000000", "at": None,
         "declared_at": "2026-10-25T06:00:00Z",
         "text": "Older limits note", "kind": "limits", "is_reply": True},
    ],
}

WR_PAGE = """
<html><body><ul>
<li class="log-item">
  <span class="pill codex">CODEX</span>
  <span class="log-item-time">3 hours ago</span>
  <p class="log-item-text">Terra is back! Day 22 ships a new model.</p>
  <a class="log-link" href="https://x.com/thsottiaux/status/2114800277677092936">view</a>
</li>
<li class="log-item">
  <span class="pill reset">RESET</span>
  <span class="log-item-time">22 hours ago</span>
  <p class="log-item-text">Day 21/ today is a reset. &amp; we are back.</p>
  <a class="log-link" href="https://x.com/thsottiaux/status/2114588174249071102">view</a>
</li>
<li class="log-item">
  <span class="pill tibo">TIBO</span>
  <span class="log-item-time">5 hours ago</span>
  <p class="log-item-text">@someone thanks!</p>
  <a class="log-link" href="https://x.com/thsottiaux/status/2114700000000000000">view</a>
</li>
<li class="log-item">
  <span class="pill tibo">TIBO</span>
  <span class="log-item-time">2 hours ago</span>
  <p class="log-item-text">Reminder you can customize the appearance of chatgpt.</p>
</li>
</ul></body></html>
"""


def _fake_http(pages):
    def get(url, accept, timeout, limit=0):
        for key, body in pages.items():
            if key in url:
                if isinstance(body, Exception):
                    raise body
                return body
        raise OSError("no route for " + url)
    return get


class TestAnnouncements(unittest.TestCase):
    def test_parse(self):
        with mock.patch.object(mod, "_http_get", _fake_http({"opentherank": PAGE})):
            out = fetch_announcement()
        self.assertIn("Oct 23, 2026", out["latest_reset"])
        self.assertTrue(out["latest_reset_local"])    # converted to local tz
        # banner says "Landed as announced" -> landed wins, stale forecast
        # from the quote is suppressed
        self.assertEqual(out["status_kind"], "landed")
        self.assertIn("Landed as announced", out["status"])
        # display text stays VERBATIM (no machine-tz conversion)
        self.assertNotIn("本机", out["status"])
        self.assertRegex(out["latest_reset_local"], r"^\d{2}-\d{2} \d{2}:\d{2}$")
        self.assertIsNone(out["landing"])
        self.assertEqual(len(out["posts"]), 1)
        post = out["posts"][0]
        self.assertEqual(post["id"], "2114400000000000000")
        self.assertEqual(post["at"], "2026-10-23T23:47:00Z")
        self.assertEqual(post["source"], "opentherank")
        self.assertEqual(post["kind"], "post")

    def test_parse_upcoming(self):
        page = PAGE.replace(
            "Landed as announced, around 10:00 Pacific Time.", "")
        with mock.patch.object(mod, "_http_get", _fake_http({"opentherank": page})):
            out = fetch_announcement()
        self.assertEqual(out["status_kind"], "upcoming")
        self.assertIn("before 10am PST", out["landing"])
        # the prose "landing page" phrase must NOT be picked up
        self.assertNotIn("landing page", out["landing"].lower())

    def test_never_raises(self):
        with mock.patch.object(mod, "_http_get",
                               side_effect=OSError("network down")):
            out = fetch_announcement()
        self.assertIsNone(out["latest_reset"])
        self.assertIsNone(out["landing"])
        self.assertIsNone(out["fetched_at"])
        self.assertEqual(out["posts"], [])


class HttpGetTest(unittest.TestCase):
    def test_fresh_opener_and_headers(self):
        seen = []

        class _Opener:
            def open(self, req, timeout=0):
                seen.append((req, timeout))
                return _Resp("ok")

        with mock.patch.object(mod.urllib.request, "build_opener",
                               side_effect=lambda *a: _Opener()) as bo:
            self.assertEqual(mod._http_get("https://e.test/a", "text/plain", 3),
                             "ok")
            self.assertEqual(mod._http_get("https://e.test/a", "text/plain", 3),
                             "ok")
        # a NEW opener per request: urllib's global opener caches the proxy
        # and would keep dialing a rotated-dead port forever
        self.assertEqual(bo.call_count, 2)
        req = seen[0][0]
        self.assertEqual(req.get_header("User-agent"), mod.UA)
        self.assertEqual(req.get_header("Accept"), "text/plain")
        self.assertEqual(seen[0][1], 3)


class CodexResetFeedTest(unittest.TestCase):
    def test_parse_feed(self):
        with mock.patch.object(mod, "_http_get",
                               _fake_http({"codex-reset": json.dumps(FEED)})):
            out = mod.fetch_codex_reset()
        self.assertTrue(out["ok"])
        self.assertEqual(out["fetched_at"], "2026-10-27T03:35:00Z")
        self.assertFalse(out["stale"])
        by_id = {p["id"]: p for p in out["posts"]}
        self.assertEqual(len(by_id), 3)
        self.assertEqual(by_id["2114800277677092936"]["kind"], "feature")
        self.assertEqual(by_id["2114588174249071102"]["kind"], "reset")
        self.assertEqual(by_id["2114000000000000000"]["kind"], "limit")
        # declared_at fallback when "at" is absent
        self.assertEqual(by_id["2114000000000000000"]["at"],
                         "2026-10-25T06:00:00Z")

    def test_bad_json_never_raises(self):
        with mock.patch.object(mod, "_http_get", _fake_http({"codex-reset": "<html>"})):
            out = mod.fetch_codex_reset()
        self.assertFalse(out["ok"])
        self.assertTrue(out["error"])
        self.assertEqual(out["posts"], [])


class WillResetFeedTest(unittest.TestCase):
    def test_parse_timeline(self):
        with mock.patch.object(mod, "_http_get", _fake_http({"willreset": WR_PAGE})):
            out = mod.fetch_willreset()
        self.assertTrue(out["ok"])
        self.assertEqual(len(out["posts"]), 3)   # short reply dropped
        self.assertFalse(any("thanks" in p["text"] for p in out["posts"]))
        by_id = {p["id"]: p for p in out["posts"] if p["id"]}
        self.assertEqual(len(by_id), 2)
        self.assertEqual(by_id["2114800277677092936"]["at"],
                         "2026-10-26T19:24:11Z")   # snowflake, exact
        self.assertEqual(by_id["2114800277677092936"]["kind"], "feature")
        self.assertEqual(by_id["2114588174249071102"]["kind"], "reset")
        self.assertIn("& we are back", by_id["2114588174249071102"]["text"])
        # item without a status link: relative time fallback still dated
        loose = [p for p in out["posts"] if not p["id"]]
        self.assertEqual(len(loose), 1)
        self.assertTrue(loose[0]["at"])
        self.assertFalse(loose[0]["is_reply"])

    def test_snowflake(self):
        self.assertEqual(mod._snowflake_iso("2114800277677092936"),
                         "2026-10-26T19:24:11Z")
        self.assertEqual(mod._snowflake_iso("2114922374978277786"),
                         "2026-10-27T03:29:22Z")
        self.assertIsNone(mod._snowflake_iso("not-an-id"))


class MergeTest(unittest.TestCase):
    def test_dedupe_by_id_and_text_oldest_first(self):
        cr = {"id": "1", "at": "2026-10-26T05:21:22Z", "text": "short",
              "source": "codex-reset"}
        wr = {"id": "1", "at": "2026-10-26T05:21:22Z",
              "text": "a much longer version of the same post text",
              "source": "willreset"}
        old = {"id": "2", "at": "2026-10-26T03:00:00Z", "text": "Older post",
               "source": "willreset"}
        loose = {"id": "", "at": "2026-10-26T04:00:00Z", "text": "Older post",
                 "source": "opentherank"}
        merged = mod._merge_posts([cr], [wr], [old, loose])
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged[0]["at"], "2026-10-26T03:00:00Z")
        self.assertEqual(merged[0]["source"], "willreset")
        self.assertEqual(merged[1]["text"], wr["text"])

    def test_same_length_prefers_curated_source(self):
        a = {"id": "1", "at": "2026-10-26T05:21:22Z", "text": "same words",
             "source": "opentherank"}
        b = {"id": "1", "at": "2026-10-26T05:21:22Z", "text": "same words",
             "source": "codex-reset"}
        merged = mod._merge_posts([b], [a])
        self.assertEqual(merged[0]["source"], "codex-reset")

    def test_only_trailing_tco_shortlinks_are_stripped(self):
        # X appends a t.co card at the end of a post; that trailing link is
        # dropped. A link in subject position is content and stays.
        a = {"id": "1", "at": "2026-10-26T05:21:22Z",
             "text": "https://t.co/Fgy0dBAHsJ has landed",
             "source": "willreset"}
        b = {"id": "2", "at": "2026-10-26T06:21:22Z",
             "text": "Terra is back now https://t.co/1ilnCkAPuy",
             "source": "codex-reset"}
        c = {"id": "3", "at": "2026-10-26T07:21:22Z",
             "text": "https://t.co/onlycard", "source": "willreset"}
        merged = mod._merge_posts([a, b, c])
        self.assertEqual([p["text"] for p in merged],
                         ["https://t.co/Fgy0dBAHsJ has landed",
                          "Terra is back now"])

    def test_cap_keeps_newest_80(self):
        posts = [{"id": str(i), "at": "2026-10-26T%02d:%02d:00Z"
                  % (i // 60 % 24, i % 60), "text": "post %d" % i,
                  "source": "willreset"} for i in range(100)]
        merged = mod._merge_posts(posts)
        self.assertEqual(len(merged), 80)
        self.assertEqual(merged[-1]["text"], "post 99")


class FetchNewsTest(unittest.TestCase):
    def test_merges_all_sources(self):
        pages = {"opentherank": PAGE, "codex-reset": json.dumps(FEED),
                 "willreset": WR_PAGE}
        with mock.patch.object(mod, "_http_get", _fake_http(pages)):
            out = mod.fetch_news()
        self.assertTrue(all(s["ok"] for s in out["sources"].values()))
        self.assertEqual(out["sources"]["codex-reset"]["posts"], 3)
        self.assertEqual(out["sources"]["willreset"]["posts"], 3)
        self.assertEqual(out["sources"]["opentherank"]["posts"], 1)
        self.assertIsNotNone(out["fetched_at"])
        # 1 + 3 + 3, minus the two X ids seen twice
        self.assertEqual(len(out["posts"]), 5)
        texts = [p["text"] for p in out["posts"]]
        ats = [p["at"] for p in out["posts"]]
        self.assertEqual(ats, sorted(ats))
        self.assertEqual(sum("Day 21" in t for t in texts), 1)
        self.assertEqual(sum("Terra is back" in t for t in texts), 1)
        # the feed's newest feature post survives even though opentherank
        # does not index it
        self.assertIn("Day 22", " | ".join(texts))

    def test_source_failure_keeps_others(self):
        pages = {"opentherank": PAGE, "willreset": WR_PAGE,
                 "codex-reset": OSError("feed down")}
        with mock.patch.object(mod, "_http_get", _fake_http(pages)):
            out = mod.fetch_news()
        self.assertFalse(out["sources"]["codex-reset"]["ok"])
        self.assertIn("feed down", out["sources"]["codex-reset"]["error"])
        self.assertTrue(out["sources"]["willreset"]["ok"])
        self.assertIsNotNone(out["fetched_at"])
        self.assertTrue(out["posts"])

    def test_all_fail(self):
        with mock.patch.object(mod, "_http_get",
                               side_effect=OSError("offline")):
            out = mod.fetch_news()
        self.assertFalse(any(s["ok"] for s in out["sources"].values()))
        self.assertIsNone(out["fetched_at"])
        self.assertEqual(out["posts"], [])
        self.assertIsNone(out["status"])


class ForecastEpochTest(unittest.TestCase):
    def test_tomorrow_anchored_to_post_date(self):
        from codex_autoresume.announcements import forecast_epoch
        # posted 10-23 23:47 UTC (= 10-23 16:47 PT); "tomorrow 10am PST"
        # = 10-24 10:00 PDT = 17:00 UTC
        e = forecast_epoch(
            "Landing tomorrow before 10am PST together with a small ship.",
            "2026-10-23T23:47:00Z")
        import datetime as dt
        u = dt.datetime.fromtimestamp(e, tz=dt.timezone.utc)
        self.assertEqual((u.month, u.day, u.hour), (10, 24, 17))

    def test_midnight_next_occurrence(self):
        from codex_autoresume.announcements import forecast_epoch
        # "By midnight PST" posted 10-24 20:40 PT (03:40 UTC): midnight
        # already passed that morning -> that night's midnight
        e = forecast_epoch("By midnight PST", "2026-10-25T03:40:04Z")
        import datetime as dt
        u = dt.datetime.fromtimestamp(e, tz=dt.timezone.utc)
        self.assertEqual((u.month, u.day, u.hour), (10, 25, 7))

    def test_no_clock_returns_none(self):
        from codex_autoresume.announcements import forecast_epoch
        self.assertIsNone(forecast_epoch("Enjoy the reset", "2026-10-25T00:00:00Z"))
        self.assertIsNone(forecast_epoch(""))


if __name__ == "__main__":
    unittest.main()
