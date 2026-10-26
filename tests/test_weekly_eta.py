"""Weekly/5h ETA: the 5h window gates the weekly drain.

Weekly projection: the current block can burn only
``x0 = min(100-P, r*(block_end-now))`` more primary at the measured rate;
if that does not cover the weekly remainder, whole blocks follow (each
refilling 100 primary and burning ``min(100, r*5h)``), until a fractional
last block. The weekly reset caps the answer: past it there is no
exhaustion ETA, the weekly window simply resets first.
5h: pure projection (100 - P) / measured_rate; the reset is NOT part of
the semantics. No fallbacks anywhere — unmeasurable means "--".
"""
import unittest

from codex_autoresume.usage import simulate_primary_eta, simulate_weekly_eta

H = 3600.0
NOW = 1_700_000_000.0
WIN = 5 * H


class SimulateWeeklyEtaTest(unittest.TestCase):
    def _eta(self, p, s, r, t=0.2, b_after=1 * H, w_after=None):
        return simulate_weekly_eta(
            NOW, p, s, r, t, NOW + b_after,
            None if w_after is None else NOW + w_after, WIN)

    def test_low_rate_leaves_blocks_unused(self):
        # r=8%/h burns only 40 primary per block; 100 weekly / (0.15625*40)
        # = 16 blocks -> 80h, not the old structural 32h
        eta, capped, reason = simulate_weekly_eta(
            NOW, 0, 0, 8, 0.15625, NOW + 5 * H, None, WIN)
        self.assertFalse(capped)
        self.assertEqual(reason, "ok")
        self.assertAlmostEqual(eta, NOW + 80 * H, delta=5)

    def test_high_rate_uses_full_blocks(self):
        # r=40%/h: current block 100, then 5.4 more blocks -> 31h
        eta, capped, reason = simulate_weekly_eta(
            NOW, 0, 0, 40, 0.15625, NOW + 5 * H, None, WIN)
        self.assertFalse(capped)
        self.assertAlmostEqual(eta, NOW + 31 * H, delta=5)

    def test_current_block_covers_remainder(self):
        # P=90, 10 points still burnable; S=98 needs only 2 weekly ->
        # 2/(0.2*20) = 0.5h, well before the block reset
        eta, capped, _ = self._eta(90, 98, 20, b_after=1 * H)
        self.assertFalse(capped)
        self.assertAlmostEqual(eta, NOW + 0.5 * H, delta=5)

    def test_multi_block_projection(self):
        # current block burns 2 weekly, leaves 48 = 2 full blocks + 0.4;
        # b is 1h away -> 1h + 10h + 2h = 13h
        eta, capped, _ = self._eta(90, 50, 20, b_after=1 * H)
        self.assertFalse(capped)
        self.assertAlmostEqual(eta, NOW + 13 * H, delta=5)

    def test_capped_by_weekly_reset(self):
        # exhaustion at +13h, weekly resets at +6h: no exhaustion ETA
        eta, capped, reason = self._eta(90, 50, 20, b_after=1 * H,
                                        w_after=6 * H)
        self.assertIsNone(eta)
        self.assertTrue(capped)
        self.assertEqual(reason, "capped")

    def test_not_capped_when_reset_is_later(self):
        eta, capped, _ = self._eta(90, 50, 20, b_after=1 * H,
                                   w_after=20 * H)
        self.assertFalse(capped)
        self.assertAlmostEqual(eta, NOW + 13 * H, delta=5)

    def test_live_0127_case(self):
        # 2026-10-27 01:06:46 P=32 S=34 r=39.48 t=0.15675, block ends
        # 05:22:03 (15317s away) -> ~20.6h later
        eta, capped, _ = simulate_weekly_eta(
            NOW, 32, 34, 39.48, 0.15675, NOW + 15_317, None, WIN)
        self.assertFalse(capped)
        self.assertAlmostEqual((eta - NOW) / H, 20.598, delta=0.01)

    def test_no_rate_is_none(self):
        self.assertEqual(self._eta(50, 50, None),
                         (None, False, "insufficient"))

    def test_no_ratio_is_none(self):
        self.assertEqual(self._eta(50, 50, 20, t=None),
                         (None, False, "insufficient"))

    def test_missing_block_end_is_none(self):
        self.assertEqual(
            simulate_weekly_eta(NOW, 50, 50, 20, 0.2, None, None, WIN),
            (None, False, "insufficient"))

    def test_already_exhausted(self):
        self.assertEqual(self._eta(50, 100, 20),
                         (None, False, "exhausted"))

    def test_past_weekly_reset_does_not_cap(self):
        eta, capped, _ = self._eta(90, 50, 20, b_after=1 * H,
                                   w_after=-10)
        self.assertFalse(capped)
        self.assertAlmostEqual(eta, NOW + 13 * H, delta=5)


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
