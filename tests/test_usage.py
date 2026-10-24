import unittest
from datetime import datetime, timedelta

from codex_autoresume.usage import (add_sample, estimate_ratio, eta_epoch,
                                    window_rate)


class TestUsageRate(unittest.TestCase):
    def test_rate_over_window(self):
        now = datetime.now().timestamp()
        samples = [(now - 1800, 10, 5), (now - 60, 20, 6), (now, 21, 6)]
        rate = window_rate(samples, 1, 3600, now=now)
        self.assertAlmostEqual(rate, 22.0, places=1)
        rate_s = window_rate(samples, 2, 3600, now=now)
        self.assertAlmostEqual(rate_s, 2.0, places=1)

    def test_rate_ignores_older_samples(self):
        now = datetime.now().timestamp()
        samples = [(now - 7200, 90, 1), (now - 600, 20, 1), (now, 22, 1)]
        rate = window_rate(samples, 1, 3600, now=now)
        self.assertAlmostEqual(rate, 12.0, places=1)

    def test_rate_rejects_short_span(self):
        # integer-granularity guard: 1 point over 5 minutes must not
        # produce a phantom 12%/h rate
        now = datetime.now().timestamp()
        samples = [(now - 300, 54, 55), (now, 55, 56)]
        self.assertIsNone(window_rate(samples, 1, 3600, now=now))
        self.assertIsNone(eta_epoch(samples, 1, 3600, now=now)[0])

    def test_rate_none_when_flat_or_single(self):
        now = datetime.now().timestamp()
        self.assertIsNone(window_rate([(now, 5, 5)], 1, 3600, now=now))
        self.assertIsNone(window_rate(
            [(now - 600, 5, 5), (now, 5, 5)], 1, 3600, now=now))
        self.assertIsNone(window_rate([], 1, 3600, now=now))

    def test_eta(self):
        now = datetime.now().timestamp()
        samples = [(now - 3600, 50, 1), (now, 75, 1)]   # 25%/h
        eta, rate, capped = eta_epoch(samples, 1, 3600, now=now)
        self.assertAlmostEqual(rate, 25.0, places=1)
        self.assertFalse(capped)
        self.assertAlmostEqual((eta - now) / 3600.0, 1.0, places=1)

    def test_eta_cap_applies_5h_gate(self):
        # measured weekly rate 30%/h is structurally impossible (the 5h
        # window gates weekly drain at 20%/h * ratio); cap clamps it
        now = datetime.now().timestamp()
        samples = [(now - 3600, 40, 40), (now, 70, 70)]  # both +30%/h
        eta, rate, capped = eta_epoch(samples, 2, 5 * 3600, now=now,
                                      cap_pct_per_h=20.0)
        self.assertTrue(capped)
        self.assertAlmostEqual(rate, 20.0, places=1)
        self.assertAlmostEqual((eta - now) / 3600.0, 1.5, places=1)  # 30% left

    def test_ratio_estimation(self):
        now = datetime.now().timestamp()
        # primary +10, secondary +1  -> ratio 0.1 (weekly budget ~10x)
        samples = [(now - 7200, 10, 20), (now, 20, 21)]
        self.assertAlmostEqual(estimate_ratio(samples), 0.1, places=3)

    def test_ratio_needs_delta(self):
        now = datetime.now().timestamp()
        self.assertIsNone(estimate_ratio([(now, 10, 20)]))
        self.assertIsNone(estimate_ratio(
            [(now - 60, 10, 20), (now, 11, 20)]))  # granularity guard

    def test_eta_full_window(self):
        now = datetime.now().timestamp()
        samples = [(now - 600, 99, 1), (now, 100, 1)]
        self.assertEqual(eta_epoch(samples, 1, 3600, now=now),
                         (None, None, False))

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
