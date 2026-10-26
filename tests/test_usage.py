import unittest
from datetime import datetime, timedelta

from codex_autoresume.usage import (add_sample, estimate_ratio, eta_epoch,
                                    window_rate, window_ratio)


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


class WindowScopingTest(unittest.TestCase):
    """Weekly ratio samples must belong to the CURRENT 5h window."""

    def test_add_sample_clears_on_5h_window_change_without_big_drop(self):
        # light block: 8% -> 0% is below the >10 jump guard, yet it is a
        # window boundary and must not leak into the next block's ratio
        samples = add_sample([], 8, 12, 1_000, 9_000)
        samples = add_sample(samples, 0, 12, 2_000, 9_000)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][1], 0)

    def test_add_sample_clears_after_an_exhausted_block(self):
        samples = add_sample([], 100, 29, 1_000, 9_000)
        samples = add_sample(samples, 0, 29, 2_000, 9_000)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][2], 29)

    def test_add_sample_keeps_history_inside_one_window(self):
        samples = add_sample([], 10, 5, 1_000, 9_000)
        samples = add_sample(samples, 20, 6, 1_030, 9_000)  # 30s jitter
        self.assertEqual(len(samples), 2)

    def test_add_sample_clears_on_weekly_window_change(self):
        samples = add_sample([], 40, 5, 1_000, 9_000)
        samples = add_sample(samples, 42, 0, 1_000, 9_500)
        self.assertEqual(len(samples), 1)
        self.assertEqual(samples[0][2], 0)

    def test_add_sample_without_reset_epochs_keeps_legacy_behavior(self):
        samples = add_sample([], 10, 5)
        samples = add_sample(samples, 20, 6)
        self.assertEqual(len(samples), 2)

    def test_ratio_ignores_previous_block(self):
        # The reported 00:22 scenario: the previous block ends exhausted
        # (weekly flat), the new block spends 13 primary points for +2
        # weekly. Pairing old data with the new block would report 4/13.
        now = 1_800_000_000.0
        samples = [
            (now - 4 * 3600, 0, 10, 1_000, 9_000),
            (now - 2 * 3600, 8, 12, 1_000, 9_000),
            (now - 3600, 0, 12, 2_000, 9_000),   # reset at "00:22"
            (now, 13, 14, 2_000, 9_000),
        ]
        self.assertAlmostEqual(window_ratio(samples, 2_000), 2 / 13, places=4)
        # without the epoch filter a light rollover (<10 points) is missed
        self.assertAlmostEqual(window_ratio(samples), 4 / 13, places=4)

    def test_exhausted_tail_does_not_dilute_the_ratio(self):
        now = 1_800_000_000.0
        samples = [
            (now - 5 * 3600, 0, 10, 1_000, 9_000),
            (now - 3 * 3600, 100, 29, 1_000, 9_000),
            (now - 2 * 3600, 100, 29, 1_000, 9_000),  # flat exhausted tail
            (now - 3600, 0, 29, 2_000, 9_000),
            (now, 13, 31, 2_000, 9_000),
        ]
        self.assertAlmostEqual(window_ratio(samples, 2_000), 2 / 13, places=4)

    def test_ratio_none_without_enough_current_window_samples(self):
        now = 1_800_000_000.0
        samples = [(now - 3600, 0, 29, 1_000, 9_000),
                   (now, 13, 31, 2_000, 9_000)]
        self.assertIsNone(window_ratio(samples, 2_000))


if __name__ == "__main__":
    unittest.main()
