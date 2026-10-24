import unittest
from datetime import datetime, timedelta


class TestTimeParse(unittest.TestCase):
    def test_time_only(self):
        from codex_autoresume.timeparse import parse_reset_time
        now = datetime(2026, 10, 24, 13, 21,
                       tzinfo=datetime.now().astimezone().tzinfo)
        msg = ("You've hit your usage limit. Upgrade to Pro, visit ... or "
               "try again at 1:51 PM.")
        dt = parse_reset_time(msg, now=now)
        self.assertIsNotNone(dt)
        self.assertEqual((dt.hour, dt.minute), (13, 51))
        self.assertLessEqual(dt - now, timedelta(hours=5))

    def test_time_only_rolls_forward(self):
        from codex_autoresume.timeparse import parse_reset_time
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 10, 24, 23, 30, tzinfo=tz)
        dt = parse_reset_time("try again at 1:51 AM.", now=now)
        # 1:51 AM already passed today; next occurrence is tomorrow morning,
        # only ~2h ahead -- the correct target for a 5h-window reset.
        self.assertEqual((dt.day, dt.hour, dt.minute), (25, 1, 51))
        self.assertGreater(dt, now)

    def test_dated_weekly(self):
        from codex_autoresume.timeparse import parse_reset_time
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 10, 24, 13, 21, tzinfo=tz)
        msg = ("You've hit your usage limit. ... or try again at "
               "Oct 27th, 2026 3:38 PM.")
        dt = parse_reset_time(msg, now=now)
        self.assertIsNotNone(dt)
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 10, 27))
        self.assertEqual((dt.hour, dt.minute), (15, 38))
        self.assertGreater(dt - now, timedelta(days=3))

    def test_dated_without_comma(self):
        from codex_autoresume.timeparse import parse_reset_time
        tz = datetime.now().astimezone().tzinfo
        now = datetime(2026, 10, 24, 13, 21, tzinfo=tz)
        dt = parse_reset_time("try again at Oct 27 2026 3:38 PM.", now=now)
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 10, 27))

    def test_base_anchors_window(self):
        from codex_autoresume.timeparse import parse_reset_time
        tz = datetime.now().astimezone().tzinfo
        base = datetime(2026, 10, 24, 12, 0, tzinfo=tz)
        dt = parse_reset_time("try again at 1:51 PM.", now=base, base=base)
        # 13:51 lies within (12:00, 18:00] -> today
        self.assertEqual((dt.day, dt.hour, dt.minute), (24, 13, 51))

    def test_base_rejects_out_of_window(self):
        from codex_autoresume.timeparse import parse_reset_time
        tz = datetime.now().astimezone().tzinfo
        base = datetime(2026, 10, 24, 2, 0, tzinfo=tz)
        dt = parse_reset_time("try again at 1:51 PM.", now=base, base=base)
        # 13:51 is >6h after a 02:00 failure; no candidate fits -> unknown
        self.assertIsNone(dt)

    def test_base_next_day(self):
        from codex_autoresume.timeparse import parse_reset_time
        tz = datetime.now().astimezone().tzinfo
        base = datetime(2026, 10, 24, 23, 30, tzinfo=tz)
        dt = parse_reset_time("try again at 1:51 AM.", now=base, base=base)
        # within (23:30, +6h] -> tomorrow 01:51
        self.assertEqual((dt.day, dt.hour, dt.minute), (25, 1, 51))

    def test_no_time(self):
        from codex_autoresume.timeparse import parse_reset_time
        self.assertIsNone(parse_reset_time("Selected model is at capacity."))
        self.assertIsNone(parse_reset_time(""))

    def test_classify_window(self):
        from codex_autoresume.timeparse import classify_window
        now = datetime.now().astimezone()
        self.assertEqual(classify_window(now + timedelta(hours=2), now), "primary")
        self.assertEqual(classify_window(now + timedelta(days=3), now), "weekly")
        self.assertEqual(classify_window(None, now), "unknown")


if __name__ == "__main__":
    unittest.main()
