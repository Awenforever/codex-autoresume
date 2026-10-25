"""Weekly-exhaustion ETA: step through 5h windows, per the hard-gate model.

Model (user-specified): the 5h window caps primary burn per window; the
weekly quota rises `ratio` percent per primary percent. Walk current
partial window -> reset -> fresh windows until weekly crosses 100%.
"""
import unittest

from codex_autoresume.usage import simulate_weekly_eta

H = 3600.0
NOW = 1_700_000_000.0
WIN = 5 * H


class SimulateWeeklyEtaTest(unittest.TestCase):
    def test_exhaust_within_current_window(self):
        # P=50 S=50, rate 20%/h, ratio 1, reset in 2.5h: remaining weekly
        # 50 needs 50 primary-% = 2.5h at 20%/h, available = min(50, 50) -> ok
        eta = simulate_weekly_eta(NOW, 50, 50, 20.0, 1.0,
                                  NOW + 2.5 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 2.5 * H, delta=5)

    def test_spills_into_next_window(self):
        # S=90 -> need 10 primary-%; current window has 50 available, so it
        # still fits now: eta = now + 0.5h
        eta = simulate_weekly_eta(NOW, 50, 90, 20.0, 1.0,
                                  NOW + 2.5 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 0.5 * H, delta=5)

    def test_needs_second_window(self):
        # S=95 -> need 5; current window drains only 2 (100-P=2): after
        # partial window S=97, need 3 more; fresh window supplies it at
        # 20%/h -> eta = reset + 0.15h
        eta = simulate_weekly_eta(NOW, 98, 95, 20.0, 1.0,
                                  NOW + H, WIN)
        self.assertAlmostEqual(eta, NOW + H + 0.15 * H, delta=5)

    def test_ratio_above_one_burns_weekly_faster(self):
        # ratio 2: need = 50/2 = 25 primary-% -> 1.25h
        eta = simulate_weekly_eta(NOW, 50, 50, 20.0, 2.0,
                                  NOW + 2.5 * H, WIN)
        self.assertAlmostEqual(eta, NOW + 1.25 * H, delta=5)

    def test_projects_past_weekly_reset(self):
        # need 50 primary-%; partial window supplies 20, the remaining 30
        # land in the first fresh window -> answer spans past where the
        # weekly reset would be (no special-casing: always give a time)
        eta = simulate_weekly_eta(NOW, 0, 50, 20.0, 1.0, NOW + H, WIN)
        self.assertAlmostEqual(eta, NOW + H + 1.5 * H, delta=5)

    def test_already_exhausted_or_missing_inputs(self):
        self.assertIsNone(simulate_weekly_eta(NOW, 0, 100, 20, 1, NOW + H, WIN))
        self.assertIsNone(simulate_weekly_eta(NOW, 0, 50, 0, 1, NOW + H, WIN))
        self.assertIsNone(simulate_weekly_eta(NOW, 0, 50, 20, 1, None, WIN))

    def test_rate_limited_window_carries_over(self):
        # slow burn 5%/h: each 5h window uses only 25 primary-%; S=0 ->
        # need 100 -> windows: 4 x 25 => eta after 3 full windows + tail
        # t=now: reset at +1h gives 5% (S=5); windows of 25%: need 95
        # w1 (+1h..+6h): 25 -> need 70; w2: 25 -> 45; w3: 25 -> 20;
        # w4: 20 needed <= 25 -> eta = now+1h+15h + 20/5h = +20h
        eta = simulate_weekly_eta(NOW, 0, 0, 5.0, 1.0, NOW + H, WIN)
        self.assertAlmostEqual(eta, NOW + H + 3 * WIN + 4 * H, delta=5)


if __name__ == "__main__":
    unittest.main()
