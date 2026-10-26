import unittest
from datetime import datetime, timedelta

from codex_autoresume.usage import (WINDOW_EPOCH_TOLERANCE, add_sample,
                                    estimate_ratio, eta_epoch, pooled_ratio,
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

    def test_add_sample_tolerates_epoch_correction(self):
        # the backend can correct resets_at by a few minutes inside one
        # block; that must not wipe the ratio samples for the block
        samples = add_sample([], 10, 5, 1_000, 9_000)
        samples = add_sample(samples, 12, 5,
                             1_000 + WINDOW_EPOCH_TOLERANCE - 60, 9_000)
        self.assertEqual(len(samples), 2)
        samples = add_sample(samples, 14, 5,
                             1_000 + WINDOW_EPOCH_TOLERANCE - 60 + 3_600,
                             9_000)
        self.assertEqual(len(samples), 1)      # real new block


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

    def test_ratio_tolerates_small_epoch_correction(self):
        # live case: resets_at corrected by ~2 minutes mid-block. Exact
        # matching kept only the post-correction samples and reported
        # 2/15=0.133; the full block is 5/32=0.156.
        now = 1_800_000_000.0
        samples = [
            (now - 3600, 0, 29, 2_000, 9_000),
            (now - 1800, 17, 32, 2_000, 9_000),
            (now - 600, 17, 32, 2_120, 9_000),
            (now, 32, 34, 2_120, 9_000),
        ]
        self.assertAlmostEqual(window_ratio(samples, 2_120), 5 / 32,
                               places=4)
        old = [s for s in samples if s[3] == 2_120]
        self.assertAlmostEqual(window_ratio(old), 2 / 15, places=4)


class PooledRatioTest(unittest.TestCase):
    """t must be stable across blocks; one block's ds is too coarse."""

    def test_weighted_pool(self):
        windows = [{"b": 1, "dp": 100, "ds": 16},
                   {"b": 2, "dp": 85, "ds": 13},
                   {"b": 3, "dp": 100, "ds": -47},   # weekly reset: BAD
                   {"b": 4, "dp": 5, "ds": 1}]       # too short
        self.assertAlmostEqual(pooled_ratio(windows), 29 / 185, places=5)

    def test_current_window_is_a_small_nudge(self):
        windows = [{"b": 1, "dp": 100, "ds": 16},
                   {"b": 2, "dp": 100, "ds": 16}]
        cur = {"b": 3, "dp": 30, "ds": 4}   # raw 0.133; weight capped at 10
        expected = (32 + 10 * 4 / 30) / 210
        self.assertAlmostEqual(pooled_ratio(windows, cur), expected, places=5)
        self.assertAlmostEqual(pooled_ratio([], cur), 4 / 30, places=5)

    def test_current_needs_minimum_points_with_pool(self):
        windows = [{"b": 1, "dp": 100, "ds": 16}]
        self.assertAlmostEqual(
            pooled_ratio(windows, {"b": 2, "dp": 4, "ds": 1}), 0.16,
            places=5)

    def test_bootstrap_needs_a_full_minimum_span(self):
        self.assertIsNone(pooled_ratio([], {"dp": 19, "ds": 3}))
        self.assertAlmostEqual(pooled_ratio([], {"dp": 20, "ds": 3}),
                               0.15, places=5)

    def test_flat_or_missing_data(self):
        self.assertIsNone(pooled_ratio([], {"dp": 40, "ds": 0}))
        self.assertIsNone(pooled_ratio([]))
        self.assertIsNone(pooled_ratio([], None))


if __name__ == "__main__":
    unittest.main()
