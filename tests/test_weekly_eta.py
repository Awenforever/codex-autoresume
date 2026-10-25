"""Weekly-exhaustion ETA: the user's block-cap model.

t = weekly-% per 1% of 5h consumed (measured in-block). Each block
consumes at most 100t weekly (a full block of primary). Remaining weekly
covers i = rest/(100t) blocks after the current one, each costing its 5h
(i may be fractional for the partial last block).
"""
import unittest

from codex_autoresume.usage import simulate_weekly_eta, simulate_primary_eta

H = 3600.0
NOW = 1_700_000_000.0
WIN = 5 * H


class SimulateWeeklyEtaTest(unittest.TestCase):
    def test_exhausts_inside_current_block(self):
        # t=0.2, P=50 -> current block can still consume 10 weekly;
        # remaining 10 <= 10 -> in-block: (10/0.2)/20 = 2.5h
        eta = simulate_weekly_eta(NOW, 50, 90, 20.0, 0.2,
                                  NOW + 4 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 2.5 * H, delta=5)

    def test_spans_blocks_with_fractional_i(self):
        # t=0.2, P=80 -> in-block capacity 4; remaining 50 > 4 ->
        # i = 46/20 = 2.3 blocks after reset: eta = reset + 2.3*5h
        eta = simulate_weekly_eta(NOW, 80, 50, 20.0, 0.2,
                                  NOW + 2 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 2 * H + 2.3 * 5 * H, delta=5)

    def test_no_measured_t_is_none(self):
        self.assertIsNone(
            simulate_weekly_eta(NOW, 80, 50, 20.0, None, NOW + H, WIN))

    def test_in_block_without_rate_is_none(self):
        # in-block exhaustion needs the burn rate; no assumptions
        self.assertIsNone(
            simulate_weekly_eta(NOW, 50, 95, None, 0.2, NOW + H, WIN))

    def test_cross_block_does_not_need_rate(self):
        # the block-cap path is structural: reset time + t is enough
        eta = simulate_weekly_eta(NOW, 80, 50, None, 0.2, NOW + 2 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 2 * H + 2.3 * 5 * H, delta=5)

    def test_already_exhausted(self):
        self.assertIsNone(
            simulate_weekly_eta(NOW, 50, 100, 20.0, 0.2, NOW + H, WIN))


class SimulatePrimaryEtaTest(unittest.TestCase):
    def test_exhausts_before_reset(self):
        # P=22 at 20%/h -> +3.9h, reset is 3.95h away
        eta = simulate_primary_eta(NOW, 22, 20.0, NOW + 3.95 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 3.9 * H, delta=5)

    def test_crossing_reset_is_none(self):
        # P=2 at 20%/h -> +4.9h, but the block refills at +1h: no honest
        # exhaustion time exists for this block
        self.assertIsNone(
            simulate_primary_eta(NOW, 2, 20.0, NOW + H, WIN))

    def test_no_rate_is_none(self):
        self.assertIsNone(simulate_primary_eta(NOW, 22, None, NOW + H, WIN))


if __name__ == "__main__":
    unittest.main()
