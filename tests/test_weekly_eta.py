"""Weekly/5h ETA: the user's exact formulas.

Weekly: i = remaining_weekly / (100t) block-equivalents, each costing its
5h window; eta = now + 5h * i (i fractional). No rate, no reset logic.
5h: pure projection (100 - P) / measured_rate; the reset is NOT part of
the semantics. No fallbacks anywhere — unmeasurable means "--".
"""
import unittest

from codex_autoresume.usage import simulate_weekly_eta, simulate_primary_eta

H = 3600.0
NOW = 1_700_000_000.0
WIN = 5 * H


class SimulateWeeklyEtaTest(unittest.TestCase):
    def test_fractional_blocks(self):
        # t=0.2: each block consumes 20 weekly; 50 remaining -> 2.5 blocks
        eta = simulate_weekly_eta(NOW, 50, 0.2, WIN)
        self.assertAlmostEqual(eta, NOW + 2.5 * 5 * H, delta=5)

    def test_small_remainder(self):
        # S=95 -> 5 remaining -> 0.25 blocks -> 1.25h
        eta = simulate_weekly_eta(NOW, 95, 0.2, WIN)
        self.assertAlmostEqual(eta, NOW + 1.25 * H, delta=5)

    def test_no_t_is_none(self):
        self.assertIsNone(simulate_weekly_eta(NOW, 50, None, WIN))

    def test_already_exhausted(self):
        self.assertIsNone(simulate_weekly_eta(NOW, 100, 0.2, WIN))

    def test_t_observed_value(self):
        # observed on 2026-10-25: t ~ 8/54 in-block
        eta = simulate_weekly_eta(NOW, 10, 8 / 54, WIN)
        expected = NOW + (90 / (100 * 8 / 54)) * 5 * H
        self.assertAlmostEqual(eta, expected, delta=5)


class SimulatePrimaryEtaTest(unittest.TestCase):
    def test_pure_projection(self):
        # P=2 at 20%/h -> +4.9h even though the block refills sooner:
        # the reset is NOT part of this answer's semantics
        eta = simulate_primary_eta(NOW, 2, 20.0, NOW + H, WIN)
        self.assertAlmostEqual(eta, NOW + 4.9 * H, delta=5)

    def test_no_rate_is_none(self):
        self.assertIsNone(simulate_primary_eta(NOW, 22, None, NOW + H, WIN))

    def test_already_exhausted(self):
        self.assertIsNone(simulate_primary_eta(NOW, 100, 20.0, NOW + H, WIN))


if __name__ == "__main__":
    unittest.main()
