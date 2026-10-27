import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from codex_autoresume.daemon import Daemon
from codex_autoresume.state import WatcherState


def _iso(hours_ago):
    return (datetime.now(timezone.utc) - timedelta(hours=hours_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ")


class NewsLogTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="cxr-newslog-")
        self.d = object.__new__(Daemon)
        self.d.state = WatcherState(
            state_path=os.path.join(self.root, "state.json"))
        self.d._state_mtime = 0.0

    def test_mirrors_recent_posts_oldest_first(self):
        old = _iso(50)
        self.d.state.news_log = [{"at": old, "text": "stale",
                                  "kind": "post"}]
        self.d._log_announcement({"posts": [
            {"id": "b", "at": _iso(1), "text": "newer", "kind": "feature"},
            {"id": "a", "at": _iso(30), "text": "older kept", "kind": "reset"},
            {"id": "c", "at": old, "text": "too old", "kind": "post"},
        ]})
        self.assertEqual([e["text"] for e in self.d.state.news_log],
                         ["older kept", "newer"])
        self.assertEqual([e["key"] for e in self.d.state.news_log], ["a", "b"])
        self.assertEqual(self.d.state.news_log[1]["kind"], "feature")
        self.assertGreater(self.d._state_mtime, 0)

    def test_no_posts_leaves_log_untouched(self):
        state = [{"at": _iso(1), "text": "keep me", "kind": "post"}]
        self.d.state.news_log = list(state)
        self.d._log_announcement({"posts": []})
        self.d._log_announcement(None)
        self.assertEqual(self.d.state.news_log, state)

    def test_key_falls_back_to_at(self):
        at = _iso(2)
        self.d._log_announcement({"posts": [
            {"at": at, "text": "no id", "kind": "post"}]})
        self.assertEqual(self.d.state.news_log[0]["key"], at)


if __name__ == "__main__":
    unittest.main()
