"""
Tests for index_feed — momentum from Kalshi's /live_data/events endpoint
(the CF Benchmarks settlement index the crypto 15-minute markets settle on).

REAL_LIVE_DATA is a trimmed copy of a real production response captured on
2026-10-18 for a live KXBTC15M event; if parsing of this shape regresses, the
bot silently falls back to the spot feed on every cycle.
"""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

import index_feed
from index_feed import (
    extract_coin,
    extract_minute_closes,
    get_index_momentum,
    momentum_from_closes,
)

REAL_LIVE_DATA = {
    "live_data": {
        "default_range": "15min",
        "type": "crypto",
        "is_historical": False,
        "details": {
            "coin": "BTC",
            "event_ticker": "KXBTC15M-26OCT180000",
            "maturity_ts_ms": 1792296000000,
            "candlesticks": {
                "1M": [
                    {"open_ts_ms": 1792295400000, "open": 81889.07, "high": 81890.07, "low": 81883.79, "close": 81890.06550000011},
                    {"open_ts_ms": 1792295460000, "open": 81890.29, "high": 81892.74, "low": 81890.29, "close": 81891.72433333342},
                    {"open_ts_ms": 1792295520000, "open": 81891.67, "high": 81891.67, "low": 81879.28, "close": 81879.2801666668},
                    {"open_ts_ms": 1792295580000, "open": 81878.95, "high": 81878.95, "low": 81873.41, "close": 81873.43600000005},
                    {"open_ts_ms": 1792295640000, "open": 81873.45, "high": 81874.00, "low": 81871.28, "close": 81871.28350000009},
                    {"open_ts_ms": 1792295700000, "open": 81871.24, "high": 81871.24, "low": 81857.53, "close": 81857.5308333334},
                    {"open_ts_ms": 1792295760000, "open": 81857.02, "high": 81857.02, "low": 81837.99, "close": 81837.99000000015},
                    {"open_ts_ms": 1792295820000, "open": 81838.12, "high": 81838.85, "low": 81838.12, "close": 81838.84550000013},
                ]
            },
        },
    }
}


class TestExtraction(unittest.TestCase):
    def test_extract_minute_closes_from_real_shape(self):
        closes = extract_minute_closes(REAL_LIVE_DATA)
        self.assertEqual(len(closes), 8)
        self.assertAlmostEqual(closes[0], 81890.0655, places=3)
        self.assertAlmostEqual(closes[-1], 81838.8455, places=3)

    def test_extract_accepts_inner_object_too(self):
        closes = extract_minute_closes(REAL_LIVE_DATA["live_data"])
        self.assertEqual(len(closes), 8)

    def test_extract_coin(self):
        self.assertEqual(extract_coin(REAL_LIVE_DATA), "BTC")

    def test_unrecognized_shapes_return_empty(self):
        self.assertEqual(extract_minute_closes({}), [])
        self.assertEqual(extract_minute_closes({"live_data": {"details": {}}}), [])
        self.assertEqual(extract_minute_closes(None), [])
        self.assertIsNone(extract_coin({}))


class TestMomentumFromCloses(unittest.TestCase):
    def test_matches_spot_feed_normalization(self):
        # +1% over the lookback window -> 0.5 at the 2% full scale.
        closes = [100.0] + [100.0] * 4 + [101.0]
        self.assertAlmostEqual(momentum_from_closes(closes, 5), 0.5)

    def test_clips_to_unit_range(self):
        closes = [100.0] * 5 + [110.0]
        self.assertEqual(momentum_from_closes(closes, 5), 1.0)
        closes = [100.0] * 5 + [90.0]
        self.assertEqual(momentum_from_closes(closes, 5), -1.0)

    def test_insufficient_history_returns_none(self):
        self.assertIsNone(momentum_from_closes([1.0, 2.0], 5))
        self.assertIsNone(momentum_from_closes([], 5))

    def test_real_fixture_momentum_is_bearish(self):
        closes = extract_minute_closes(REAL_LIVE_DATA)
        momentum = momentum_from_closes(closes, 5)
        # Index fell from ~81879 to ~81839 over the lookback: mildly bearish.
        self.assertIsNotNone(momentum)
        self.assertLess(momentum, 0)
        self.assertGreater(momentum, -0.2)


class TestGetIndexMomentum(unittest.TestCase):
    def setUp(self):
        index_feed._cache.clear()

    def test_computes_and_caches(self):
        client = MagicMock()
        client.get_event_live_data.return_value = REAL_LIVE_DATA
        with patch("config.MOMENTUM_LOOKBACK_BARS", 5):
            m1 = get_index_momentum(client, "KXBTC15M-26OCT180000")
            m2 = get_index_momentum(client, "KXBTC15M-26OCT180000")
        self.assertIsNotNone(m1)
        self.assertEqual(m1, m2)
        # Second call served from cache — no extra API hit.
        self.assertEqual(client.get_event_live_data.call_count, 1)

    def test_api_failure_returns_none(self):
        client = MagicMock()
        client.get_event_live_data.side_effect = RuntimeError("boom")
        self.assertIsNone(get_index_momentum(client, "KXBTC15M-TEST"))

    def test_thin_history_returns_none(self):
        client = MagicMock()
        client.get_event_live_data.return_value = {
            "live_data": {"details": {"candlesticks": {"1M": [{"close": 1.0}]}}}
        }
        self.assertIsNone(get_index_momentum(client, "KXBTC15M-TEST"))

    def test_empty_event_ticker_returns_none(self):
        client = MagicMock()
        self.assertIsNone(get_index_momentum(client, ""))
        client.get_event_live_data.assert_not_called()

    def test_cache_is_bounded(self):
        client = MagicMock()
        client.get_event_live_data.return_value = REAL_LIVE_DATA
        with patch("config.MOMENTUM_LOOKBACK_BARS", 5), patch.object(
            index_feed, "_CACHE_MAX_EVENTS", 4
        ):
            for i in range(10):
                get_index_momentum(client, f"KXBTC15M-EV{i}")
        self.assertLessEqual(len(index_feed._cache), 4)


if __name__ == "__main__":
    unittest.main()
