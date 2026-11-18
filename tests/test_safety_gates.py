"""Safety gates: production host, empty ticker, cutoff floor, legacy entrypoint."""

import os
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

import config
from engine.cutoff import CutoffBlocked, load_policy
from engine.settings import load_settings


class TestProductionHostGate(unittest.TestCase):
    def test_empty_env_resolves_to_demo(self):
        from kalshi_client import _pick_trade_api_host

        with patch.object(config, "KALSHI_ENV", ""), patch.object(
            config, "BASE_URL", "https://external-api.demo.kalshi.co/trade-api/v2"
        ):
            host = _pick_trade_api_host()
        self.assertIn("demo", host)
        self.assertNotIn("elections.kalshi.com", host)

    def test_missing_env_attribute_resolves_to_demo(self):
        from kalshi_client import _pick_trade_api_host

        with patch.object(config, "KALSHI_ENV", None), patch.object(
            config, "BASE_URL", "https://demo-api.kalshi.co/trade-api/v2"
        ):
            host = _pick_trade_api_host()
        self.assertIn("demo", host)
        self.assertNotIn("api.elections.kalshi.com", host)

    def test_prod_host_without_allow_flag_refuses_init(self):
        import kalshi_client as kc

        with patch.object(config, "KALSHI_ENV", "demo"), patch.object(
            config, "KALSHI_ALLOW_PROD", False
        ), patch.object(
            config, "BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
        ), patch.object(
            config, "KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2"
        ):
            with self.assertRaises(RuntimeError) as caught:
                kc.KalshiClient()
        self.assertIn("KALSHI_ALLOW_PROD", str(caught.exception))

    def test_explicit_prod_env_still_selects_the_elections_host(self):
        from kalshi_client import _pick_trade_api_host

        with patch.object(config, "KALSHI_ENV", "prod"):
            self.assertEqual(
                _pick_trade_api_host(),
                "https://api.elections.kalshi.com/trade-api/v2",
            )


class TestEmptyTickerFailsClosed(unittest.TestCase):
    def test_submit_without_ticker_does_not_call_the_sdk(self):
        import kalshi_client as kc

        with patch.object(kc, "_SdkConfiguration"), patch.object(
            kc, "_SdkKalshiClient"
        ) as sdk_cls, patch.object(kc, "_read_private_key_pem", return_value="---PEM---"):
            sdk = MagicMock()
            sdk_cls.return_value = sdk
            client = kc.KalshiClient()
        with self.assertRaises(CutoffBlocked):
            client._submit_order({"ticker": "", "side": "bid", "count": "1.00", "price": "0.50"}, order_side="yes")
        sdk.create_order_v2.assert_not_called()
        with self.assertRaises(CutoffBlocked):
            client._submit_order({"side": "bid"}, order_side="yes")
        sdk.create_order_v2.assert_not_called()


class TestCutoffFloor(unittest.TestCase):
    def test_cutoff_seconds_45_is_clamped_to_50(self):
        with patch.dict(os.environ, {"CUTOFF_SECONDS": "45"}, clear=False):
            policy = load_policy()
            settings = load_settings()
        self.assertGreaterEqual(policy.cutoff_seconds, 50)
        self.assertGreaterEqual(settings.cutoff.cutoff_seconds, 50)


class TestLegacyBotRefuses(unittest.TestCase):
    def test_bot_main_exits_without_legacy_flag(self):
        import bot

        env = os.environ.copy()
        env.pop("LEGACY_BOT", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as caught:
                bot.main()
        self.assertEqual(caught.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
