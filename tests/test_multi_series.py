"""
Tests for multi-series support: SERIES_TICKERS config, the per-series safety
check, momentum routing (settlement index first, spot fallback), and
manage_all_positions across several active markets.
"""

import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    os.path.join(tempfile.gettempdir(), f"openclaw_stop_file_tests_{os.getpid()}"),
)

import config
from bot import _momentum_for_market, manage_all_positions, manage_positions
from index_feed import spot_symbol_for_series
from kalshi_client import KalshiClient
from risk_manager import RiskManager


def _future_iso(minutes=10):
    return (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(minutes=minutes)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestSeriesConfig(unittest.TestCase):
    def test_default_is_legacy_btc_series(self):
        import importlib
        env_backup = os.environ.pop("SERIES_TICKERS", None)
        try:
            import config as _cfg
            importlib.reload(_cfg)
            self.assertEqual(_cfg.SERIES_TICKERS, [_cfg.BTC_SERIES_TICKER])
        finally:
            if env_backup is not None:
                os.environ["SERIES_TICKERS"] = env_backup
            importlib.reload(config)

    def test_list_parsing_dedupes_and_uppercases(self):
        import importlib
        env_backup = os.environ.get("SERIES_TICKERS")
        os.environ["SERIES_TICKERS"] = " kxbtc15m, KXETH15M ,KXBTC15M,KXSOL15M "
        try:
            import config as _cfg
            importlib.reload(_cfg)
            self.assertEqual(
                _cfg.SERIES_TICKERS, ["KXBTC15M", "KXETH15M", "KXSOL15M"]
            )
        finally:
            if env_backup is None:
                os.environ.pop("SERIES_TICKERS", None)
            else:
                os.environ["SERIES_TICKERS"] = env_backup
            importlib.reload(config)


class TestSeriesSafetyCheck(unittest.TestCase):
    def test_any_configured_series_is_allowed(self):
        client = object.__new__(KalshiClient)
        with patch.object(config, "SERIES_TICKERS", ["KXBTC15M", "KXETH15M"]):
            client._ensure_btc_market("KXETH15M-26OCT180000-45")
            client._ensure_btc_market("KXBTC15M-26OCT180000-45")
            with self.assertRaises(ValueError):
                client._ensure_btc_market("KXSOL15M-26OCT180000-45")
            with self.assertRaises(ValueError):
                client._ensure_btc_market("NOTCRYPTO-TEST")


class TestSpotSymbolMapping(unittest.TestCase):
    def test_known_series(self):
        self.assertEqual(spot_symbol_for_series("KXBTC15M"), "BTC-USD")
        self.assertEqual(spot_symbol_for_series("KXETH15M"), "ETH-USD")
        self.assertEqual(spot_symbol_for_series("KXSOL15M"), "SOL-USD")
        self.assertEqual(spot_symbol_for_series("KXBTC"), "BTC-USD")
        self.assertEqual(spot_symbol_for_series("KXBTCD"), "BTC-USD")

    def test_unknown_series_falls_back_to_btc(self):
        self.assertEqual(spot_symbol_for_series("KXWEIRD15M"), "BTC-USD")


class TestMomentumRouting(unittest.TestCase):
    def test_prefers_settlement_index(self):
        client = MagicMock()
        with patch("bot.get_index_momentum", return_value=0.25) as p_idx, patch(
            "bot.get_spot_momentum"
        ) as p_spot:
            m = _momentum_for_market(
                client, "KXETH15M", {"event_ticker": "KXETH15M-26OCT180000"}
            )
        self.assertEqual(m, 0.25)
        p_idx.assert_called_once_with(client, "KXETH15M-26OCT180000")
        p_spot.assert_not_called()

    def test_falls_back_to_per_coin_spot(self):
        client = MagicMock()
        with patch("bot.get_index_momentum", return_value=None), patch(
            "bot.get_spot_momentum", return_value=-0.1
        ) as p_spot:
            m = _momentum_for_market(
                client, "KXSOL15M", {"event_ticker": "KXSOL15M-26OCT180000"}
            )
        self.assertEqual(m, -0.1)
        p_spot.assert_called_once_with("SOL-USD")


class TestManageAllPositions(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        self._tmp.close()
        self._log_patch = patch.object(config, "TRADE_LOG_FILE", self._tmp.name)
        self._log_patch.start()
        self._dry_patch = patch.object(config, "DRY_RUN", True)
        self._dry_patch.start()

    def tearDown(self):
        self._log_patch.stop()
        self._dry_patch.stop()
        os.unlink(self._tmp.name)

    def _market(self, ticker, yes_bid, no_bid, minutes=10):
        return {
            "ticker": ticker,
            "best_yes_bid": yes_bid,
            "best_no_bid": no_bid,
            "yes_bid": yes_bid,
            "no_bid": no_bid,
            "close_time": _future_iso(minutes),
        }

    def test_positions_across_series_managed_in_one_pass(self):
        """A stop-loss on the BTC market and a take-profit on the ETH market
        are both exited in a single manage_all_positions sweep."""
        client = MagicMock()
        risk = RiskManager()
        btc = "KXBTC15M-26OCT180000-45"
        eth = "KXETH15M-26OCT180000-45"
        risk.record_open_position(btc, "yes", 2, 60)  # stop-loss at <=40
        risk.record_open_position(eth, "yes", 2, 40)  # take-profit at >=70

        markets = {
            btc: self._market(btc, yes_bid=30, no_bid=69),
            eth: self._market(eth, yes_bid=85, no_bid=14),
        }
        with patch.object(config, "STOP_LOSS_CENTS", 20), patch.object(
            config, "TAKE_PROFIT_CENTS", 30
        ):
            closed = list(manage_all_positions(client, markets, risk, None))

        reasons = {c["market"]: c["exit_reason"] for c in closed}
        self.assertEqual(reasons[btc], "stop_loss")
        self.assertEqual(reasons[eth], "take_profit")

    def test_single_market_wrapper_only_uses_prefetched_data_for_that_market(self):
        """The legacy manage_positions wrapper still works and routes other
        tracked tickers through the stale-market path."""
        client = MagicMock()
        risk = RiskManager()
        btc = "KXBTC15M-26OCT180000-45"
        other = "KXETH15M-26OCT174500-45"
        risk.record_open_position(btc, "yes", 1, 50)
        risk.record_open_position(other, "yes", 1, 50)

        # The stale ETH market comes back settled YES -> reconciled at 100c.
        client.get_market.return_value = {
            "market": {"ticker": other, "status": "settled", "result": "yes"}
        }
        market = self._market(btc, yes_bid=50, no_bid=49)
        with patch.object(config, "STOP_LOSS_CENTS", 20), patch.object(
            config, "TAKE_PROFIT_CENTS", 30
        ):
            closed = list(manage_positions(client, market, risk))

        self.assertEqual(len(closed), 1)
        self.assertEqual(closed[0]["market"], other)
        self.assertEqual(closed[0]["exit_reason"], "settled")
        self.assertEqual(closed[0]["exit_price"], 100)


if __name__ == "__main__":
    unittest.main()
