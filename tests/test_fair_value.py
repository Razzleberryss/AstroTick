"""Settlement-average fair value, including the final minute."""

import math
import os
import unittest

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from engine.fair_value import (
    fair_value_yes,
    sample_delays,
    sum_pairwise_min,
)


class TestPairwiseVariance(unittest.TestCase):
    def test_sum_matches_double_loop(self):
        delays = [float(i) for i in range(1, 61)]
        slow = sum(min(a, b) for a in delays for b in delays)
        self.assertAlmostEqual(sum_pairwise_min(delays), slow)

    def test_tau_at_least_60_matches_tau_minus_39_5(self):
        # Discrete samples at close-59 .. close. For tau >= 60 the variance
        # factor is tau - 39.5 within a few thousandths (the "sqrt(tau-40)" form).
        for tau in (60, 90, 120, 600):
            delays = [d for d in sample_delays(tau) if d > 0]
            factor = sum_pairwise_min(delays) / 3600.0
            self.assertAlmostEqual(factor, tau - 39.5, delta=0.01)
            self.assertEqual(len(delays), 60)


class TestFairValue(unittest.TestCase):
    def test_atm_is_one_half_before_the_final_minute(self):
        fv = fair_value_yes(
            spot=100_000,
            strike=100_000,
            tau_seconds=600,
            sigma_per_sqrt_second=1e-4,
            locked_samples=[],
        )
        self.assertIsNotNone(fv)
        self.assertAlmostEqual(fv.probability_yes, 0.5, places=6)
        self.assertEqual(fv.locked_count, 0)
        self.assertEqual(fv.future_count, 60)
        self.assertAlmostEqual(fv.expected_average, 100_000)

    def test_spot_above_strike_is_high_and_monotonic(self):
        low = fair_value_yes(
            spot=100_000, strike=100_000, tau_seconds=300,
            sigma_per_sqrt_second=1e-5, locked_samples=[],
        )
        high = fair_value_yes(
            spot=100_200, strike=100_000, tau_seconds=300,
            sigma_per_sqrt_second=1e-5, locked_samples=[],
        )
        self.assertGreater(high.probability_yes, 0.9)
        self.assertGreater(high.probability_yes, low.probability_yes)

    def test_missing_locked_samples_refuse(self):
        # tau=30s needs 30 locked prints. Passing none is not a guess.
        self.assertIsNone(
            fair_value_yes(
                spot=100_000,
                strike=100_000,
                tau_seconds=30,
                sigma_per_sqrt_second=1e-4,
                locked_samples=[],
            )
        )

    def test_final_minute_locks_raise_the_expected_average(self):
        # 30 samples already printed at 110, spot now 100, strike 100.
        locked = [110.0] * 30
        fv = fair_value_yes(
            spot=100,
            strike=100,
            tau_seconds=30,
            sigma_per_sqrt_second=1e-3,
            locked_samples=locked,
        )
        self.assertIsNotNone(fv)
        self.assertEqual(fv.locked_count, 30)
        self.assertEqual(fv.future_count, 30)
        self.assertAlmostEqual(fv.expected_average, 105.0)
        self.assertGreater(fv.probability_yes, 0.5)

    def test_fully_locked_tie_is_yes_and_below_is_no(self):
        tie = fair_value_yes(
            spot=100,
            strike=100,
            tau_seconds=0,
            sigma_per_sqrt_second=0,
            locked_samples=[100.0] * 60,
        )
        self.assertEqual(tie.probability_yes, 1.0)
        self.assertEqual(tie.future_count, 0)
        below = fair_value_yes(
            spot=99,
            strike=100,
            tau_seconds=0,
            sigma_per_sqrt_second=0,
            locked_samples=[99.0] * 60,
        )
        self.assertEqual(below.probability_yes, 0.0)

    def test_zero_vol_with_time_left_is_not_a_price(self):
        self.assertIsNone(
            fair_value_yes(
                spot=100, strike=90, tau_seconds=120,
                sigma_per_sqrt_second=0, locked_samples=[],
            )
        )

    def test_probability_matches_normal_cdf(self):
        fv = fair_value_yes(
            spot=101, strike=100, tau_seconds=60,
            sigma_per_sqrt_second=1e-2, locked_samples=[],
        )
        z = (fv.expected_average - 100) / fv.std_average
        expected = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        self.assertAlmostEqual(fv.probability_yes, expected, places=8)


if __name__ == "__main__":
    unittest.main()
