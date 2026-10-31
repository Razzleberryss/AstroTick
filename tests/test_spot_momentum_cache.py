"""Spot-momentum fallback cache / background-refresh behavior."""

import time
import unittest
from unittest.mock import patch

import strategy


class TestSpotMomentumBackgroundCache(unittest.TestCase):
    def setUp(self):
        strategy._spot_momentum_cache.clear()

    def tearDown(self):
        strategy._spot_momentum_cache.clear()

    @patch("strategy._fetch_spot_momentum_sync", return_value=0.42)
    def test_cold_start_fetches_once(self, mock_fetch):
        value = strategy.get_spot_momentum("BTC-USD")
        self.assertEqual(value, 0.42)
        self.assertEqual(mock_fetch.call_count, 1)

    @patch("strategy._fetch_spot_momentum_sync", return_value=0.5)
    def test_warm_cache_does_not_block_on_fetch(self, mock_fetch):
        strategy._store_spot_momentum("ETH-USD", 0.25)
        # Make the entry look fresh.
        strategy._spot_momentum_cache["ETH-USD"]["timestamp"] = time.time()
        value = strategy.get_spot_momentum("ETH-USD")
        self.assertEqual(value, 0.25)
        mock_fetch.assert_not_called()

    @patch("strategy._maybe_start_spot_refresh")
    @patch("strategy._fetch_spot_momentum_sync", return_value=0.5)
    def test_expired_cache_returns_none_instead_of_indefinite_stale(
        self, mock_fetch, mock_refresh,
    ):
        """Failed refreshes must not keep serving momentum from prior windows."""
        strategy._store_spot_momentum("BTC-USD", 0.9)
        strategy._spot_momentum_cache["BTC-USD"]["timestamp"] = (
            time.time() - strategy._SPOT_MOMENTUM_MAX_STALE_SECONDS - 1.0
        )

        value = strategy.get_spot_momentum("BTC-USD")

        self.assertIsNone(value)
        mock_fetch.assert_not_called()  # hot path must not block on yfinance
        mock_refresh.assert_called_once_with("BTC-USD")

    @patch("strategy._maybe_start_spot_refresh")
    @patch("strategy._fetch_spot_momentum_sync", return_value=None)
    def test_value_just_inside_max_stale_is_still_served(self, mock_fetch, mock_refresh):
        strategy._store_spot_momentum("SOL-USD", -0.3)
        # Just under the hard cutoff — still usable while a refresh may run.
        strategy._spot_momentum_cache["SOL-USD"]["timestamp"] = (
            time.time() - strategy._SPOT_MOMENTUM_MAX_STALE_SECONDS + 5.0
        )

        value = strategy.get_spot_momentum("SOL-USD")

        self.assertEqual(value, -0.3)
        mock_fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
