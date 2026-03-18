"""
test_liquidity_filters.py - Tests for orderbook depth/spread liquidity filters.

Verifies that:
1. Spread and depth are correctly computed from orderbooks
2. Liquidity filters block trades when spread is too wide
3. Liquidity filters block trades when depth is too thin
"""
import unittest
from unittest.mock import MagicMock, patch

from kalshi_client import KalshiClient
from strategy import generate_signal
import config


class TestSpreadAndDepthCalculations(unittest.TestCase):
    """Test spread and depth calculations in get_market_quotes."""

    def setUp(self):
        """Create a mock KalshiClient instance."""
        with patch.object(KalshiClient, '_load_private_key', return_value=None):
            self.client = KalshiClient()

    def test_spread_calculation(self):
        """Test that spread is correctly calculated as (ask - bid) / 100."""
        orderbook = {
            "orderbook": {
                "yes": [[52, 10]],  # YES bid at 52c
                "no": [[46, 8]],    # NO bid at 46c
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # YES ask = 100 - 46 = 54c
        # Spread = (54 - 52) / 100 = 0.02
        self.assertEqual(quotes["spread"], 0.02)

    def test_depth_calculation_near_mid(self):
        """Test that depth counts contracts within DEPTH_BAND of mid price."""
        # Save original DEPTH_BAND
        original_depth_band = config.DEPTH_BAND
        config.DEPTH_BAND = 0.05  # 5 cents

        orderbook = {
            "orderbook": {
                # best_yes_bid=53, best_no_bid=47
                # best_yes_ask=100-47=53, best_no_ask=100-53=47
                # Mid = (53+53)/2 = 53c, so depth band is 48c-58c
                "yes": [
                    [53, 20],  # Within band (53c)
                    [52, 15],  # Within band (52c)
                    [48, 10],  # Within band (48c)
                    [40, 100],  # Outside band (too low, 40c < 48c)
                ],
                "no": [
                    [47, 25],  # 47c - this is the bid, so it's the NO price
                    [46, 30],  # 46c
                    [60, 100],  # Outside band
                ],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        # Mid = (53+53)/2 = 53c
        # Depth band: 48c to 58c
        # YES depth: 53c (20) + 52c (15) + 48c (10) = 45
        # NO depth: we need to check which NO bids fall in the band
        # NO bid at 47c is within 48-58? No, it's below 48
        # Actually, let me recalculate: mid is 53, band is ±5, so 48-58
        # NO prices at 47, 46, 60: none are in 48-58 range
        self.assertEqual(quotes["yes_depth_near_mid"], 45)
        # NO: 47, 46, 60 - none in 48-58 range
        self.assertEqual(quotes["no_depth_near_mid"], 0)

        # Restore original DEPTH_BAND
        config.DEPTH_BAND = original_depth_band

    def test_empty_orderbook_depth(self):
        """Test that empty orderbook returns zero depth."""
        orderbook = {
            "orderbook": {
                "yes": [],
                "no": [],
            }
        }

        with patch.object(self.client, 'get_orderbook', return_value=orderbook):
            quotes = self.client.get_market_quotes("TEST-TICKER")

        self.assertEqual(quotes["yes_depth_near_mid"], 0)
        self.assertEqual(quotes["no_depth_near_mid"], 0)
        self.assertIsNone(quotes["spread"])


class TestLiquidityFilters(unittest.TestCase):
    """Test that liquidity filters block trades appropriately."""

    def setUp(self):
        """Save original config values."""
        self.original_max_spread = config.MAX_SPREAD
        self.original_min_yes_depth = config.MIN_YES_DEPTH
        self.original_min_no_depth = config.MIN_NO_DEPTH
        self.original_min_confidence = config.MIN_CONFIDENCE
        self.original_max_slippage = config.MAX_SLIPPAGE
        self.original_max_price_deviation = config.MAX_PRICE_DEVIATION

    def tearDown(self):
        """Restore original config values."""
        config.MAX_SPREAD = self.original_max_spread
        config.MIN_YES_DEPTH = self.original_min_yes_depth
        config.MIN_NO_DEPTH = self.original_min_no_depth
        config.MIN_CONFIDENCE = self.original_min_confidence
        config.MAX_SLIPPAGE = self.original_max_slippage
        config.MAX_PRICE_DEVIATION = self.original_max_price_deviation

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_wide_spread_blocks_trade(self, mock_skew, mock_momentum):
        """Test that trades are blocked when spread exceeds MAX_SPREAD."""
        # Set up mocks for a bullish signal
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        # Lower MIN_CONFIDENCE so signal passes confidence check
        config.MIN_CONFIDENCE = 0.001

        # Set MAX_SPREAD to 0.05 (5 cents)
        config.MAX_SPREAD = 0.05

        # Market with wide spread (10 cents)
        market = {
            "best_yes_bid": 45,
            "best_yes_ask": 55,
            "best_no_bid": 45,
            "best_no_ask": 55,
            "spread": 0.10,  # 10 cents - exceeds MAX_SPREAD
            "yes_depth_near_mid": 100,
            "no_depth_near_mid": 100,
        }

        orderbook = {
            "orderbook": {"yes": [[45, 100]], "no": [[45, 100]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return None because spread exceeds MAX_SPREAD
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_low_yes_depth_blocks_trade(self, mock_skew, mock_momentum):
        """Test that trades are blocked when YES depth is below MIN_YES_DEPTH."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        config.MIN_CONFIDENCE = 0.001
        config.MAX_SPREAD = 0.20  # Wide enough to not block
        config.MIN_YES_DEPTH = 100  # Require 100 contracts

        # Market with low YES depth
        market = {
            "best_yes_bid": 48,
            "best_yes_ask": 52,
            "best_no_bid": 48,
            "best_no_ask": 52,
            "spread": 0.04,  # Within MAX_SPREAD
            "yes_depth_near_mid": 30,  # Below MIN_YES_DEPTH
            "no_depth_near_mid": 100,
        }

        orderbook = {
            "orderbook": {"yes": [[48, 30]], "no": [[48, 100]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return None because YES depth is too low
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    def test_low_no_depth_blocks_trade(self, mock_skew, mock_momentum):
        """Test that trades are blocked when NO depth is below MIN_NO_DEPTH."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3

        config.MIN_CONFIDENCE = 0.001
        config.MAX_SPREAD = 0.20
        config.MIN_YES_DEPTH = 50
        config.MIN_NO_DEPTH = 100  # Require 100 contracts

        # Market with low NO depth
        market = {
            "best_yes_bid": 48,
            "best_yes_ask": 52,
            "best_no_bid": 48,
            "best_no_ask": 52,
            "spread": 0.04,
            "yes_depth_near_mid": 100,
            "no_depth_near_mid": 20,  # Below MIN_NO_DEPTH
        }

        orderbook = {
            "orderbook": {"yes": [[48, 100]], "no": [[48, 20]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return None because NO depth is too low
        self.assertIsNone(signal)

    @patch('strategy.get_btc_momentum')
    @patch('strategy.get_orderbook_skew')
    @patch('strategy.decide_trade_fee_aware')
    def test_good_liquidity_allows_trade(self, mock_decide, mock_skew, mock_momentum):
        """Test that trades proceed when liquidity is sufficient."""
        mock_momentum.return_value = 0.5
        mock_skew.return_value = 0.3
        mock_decide.return_value = ("BUY_YES", 5)

        config.MIN_CONFIDENCE = 0.001
        config.MAX_SPREAD = 0.10
        config.MIN_YES_DEPTH = 50
        config.MIN_NO_DEPTH = 50
        config.MAX_SLIPPAGE = 0.10  # Allow up to 10 cent spread for MAX_SLIPPAGE filter
        config.MAX_PRICE_DEVIATION = 0.50  # Allow wide deviations

        # Market with good liquidity
        market = {
            "best_yes_bid": 48,
            "best_yes_ask": 52,
            "best_no_bid": 48,
            "best_no_ask": 52,
            "spread": 0.04,  # Within MAX_SPREAD
            "yes_depth_near_mid": 100,  # Above MIN_YES_DEPTH
            "no_depth_near_mid": 100,  # Above MIN_NO_DEPTH
        }

        orderbook = {
            "orderbook": {"yes": [[48, 100]], "no": [[48, 100]]}
        }

        signal = generate_signal(market, orderbook)

        # Should return a signal (not None) because liquidity is good
        self.assertIsNotNone(signal)
        self.assertEqual(signal.side, "yes")
        self.assertEqual(signal.size, 5)


if __name__ == "__main__":
    unittest.main()
