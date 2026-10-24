import unittest
from datetime import datetime, timedelta

from codex_autoresume.usage import add_sample, eta_epoch, window_rate


class TestUsageRate(unittest.TestCase):
    def test_rate_over_window(self):
        now = datetime.now().timestamp()
        samples = [(now - 1800, 10, 5), (now - 60, 20, 6), (now, 21, 6)]
        rate = window_rate(samples, 1, 3600, now=now)
        # (21-10) over 0.5h = 22%/h
        self.assertAlmostEqual(rate, 22.0, places=1)
        # secondary barely moved: (6-5)/0.5h = 2%/h
        rate_s = window_rate(samples, 2, 3600, now=now)
        self.assertAlmostEqual(rate_s, 2.0, places=1)

    def test_rate_ignores_older_samples(self):
        now = datetime.now().timestamp()
        samples = [(now - 7200, 90, 1), (now - 600, 20, 1), (now, 22, 1)]
        rate = window_rate(samples, 1, 3600, now=now)
        # window only spans the last two samples: (22-20)/(600/3600)=12/h
        self.assertAlmostEqual(rate, 12.0, places=1)

    def test_rate_none_when_flat_or_single(self):
        now = datetime.now().timestamp()
        self.assertIsNone(window_rate([(now, 5, 5)], 1, 3600, now=now))
        self.assertIsNone(window_rate(
            [(now - 600, 5, 5), (now, 5, 5)], 1, 3600, now=now))
        self.assertIsNone(window_rate([], 1, 3600, now=now))

    def test_eta(self):
        now = datetime.now().timestamp()
        samples = [(now - 3600, 50, 1), (now, 75, 1)]   # 25%/h
        eta, rate = eta_epoch(samples, 1, 3600, now=now)
        self.assertAlmostEqual(rate, 25.0, places=1)
        self.assertAlmostEqual((eta - now) / 3600.0, 1.0, places=1)  # 25% left

    def test_eta_full_window(self):
        now = datetime.now().timestamp()
        samples = [(now - 600, 99, 1), (now, 100, 1)]
        self.assertEqual(eta_epoch(samples, 1, 3600, now=now), (None, None))

    def test_add_sample_resets_on_rollover(self):
        now = datetime.now().timestamp()
        samples = [(now - 60, 95, 30)]
        samples = add_sample(samples, 5, 30)      # window rolled: big drop
        self.assertEqual(len(samples), 1)          # history cleared
        self.assertEqual(samples[0][1], 5)

    def test_add_sample_trims(self):
        now = datetime.now().timestamp()
        samples = [(now - 7 * 3600, 10, 1)]
        samples = add_sample(samples, 12, 1)
        self.assertEqual(len(samples), 1)


if __name__ == "__main__":
    unittest.main()
