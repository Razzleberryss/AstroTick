import unittest
import os
import tempfile
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    os.path.join(tempfile.gettempdir(), f"openclaw_stop_file_tests_{os.getpid()}"),
)

import config


class TestKalshiSdkInit(unittest.TestCase):
    def test_sdk_client_initialization_sets_host_and_keys(self):
        # Import inside test so patches apply to module globals.
        import kalshi_client as kc

        cfg_instance = MagicMock()

        def _cfg_ctor(*, host):
            cfg_instance.host = host
            return cfg_instance

        sdk_client_instance = MagicMock()

        with patch.object(kc, "_SdkConfiguration", side_effect=_cfg_ctor) as p_cfg, patch.object(
            kc, "_SdkKalshiClient", return_value=sdk_client_instance
        ) as p_client, patch.object(
            kc, "_read_private_key_pem", return_value="---PEM---"
        ):
            _ = kc.KalshiClient()

        p_cfg.assert_called_once()
        p_client.assert_called_once()
        self.assertTrue(cfg_instance.host.endswith("/trade-api/v2"))
        self.assertEqual(cfg_instance.api_key_id, config.KALSHI_API_KEY_ID)
        self.assertEqual(cfg_instance.private_key_pem, "---PEM---")


class TestOrderPayloadConstruction(unittest.TestCase):
    def setUp(self):
        import kalshi_client as kc

        self.kc = kc
        # Make a client with SDK mocked to avoid touching network / keys.
        with patch.object(kc, "_SdkConfiguration") as _p_cfg, patch.object(
            kc, "_SdkKalshiClient"
        ) as _p_client, patch.object(kc, "_read_private_key_pem", return_value="---PEM---"):
            self.client = kc.KalshiClient()

    def test_buy_yes_maps_to_bid_at_yes_price(self):
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="yes",
            action="buy",
            contracts=3,
            price_cents=55,
            client_order_id="cid",
        )
        self.assertEqual(payload["ticker"], f"{config.BTC_SERIES_TICKER}-TEST")
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["count"], "3")
        self.assertEqual(payload["price"], "0.55")
        self.assertEqual(payload["client_order_id"], "cid")
        self.assertEqual(payload["time_in_force"], "immediate_or_cancel")
        self.assertEqual(payload["self_trade_prevention_type"], "taker_at_cross")

    def test_sell_yes_maps_to_ask_at_yes_price(self):
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="yes",
            action="sell",
            contracts=2,
            price_cents=55,
            reduce_only=True,
        )
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["price"], "0.55")
        self.assertTrue(payload["reduce_only"])

    def test_buy_no_maps_to_ask_at_complementary_price(self):
        # Buying NO at 40c == selling YES at 60c on the single YES book.
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="no",
            action="buy",
            contracts=1,
            price_cents=40,
        )
        self.assertEqual(payload["side"], "ask")
        self.assertEqual(payload["price"], "0.60")

    def test_sell_no_maps_to_bid_at_complementary_price(self):
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="no",
            action="sell",
            contracts=1,
            price_cents=40,
        )
        self.assertEqual(payload["side"], "bid")
        self.assertEqual(payload["price"], "0.60")

    def test_post_only_orders_rest_gtc(self):
        payload = self.client._build_order_payload(
            ticker=f"{config.BTC_SERIES_TICKER}-TEST",
            side="yes",
            action="buy",
            contracts=1,
            price_cents=50,
            post_only=True,
        )
        self.assertEqual(payload["time_in_force"], "good_till_canceled")
        self.assertTrue(payload["post_only"])


class TestSdkCallPaths(unittest.TestCase):
    def setUp(self):
        import kalshi_client as kc

        self.kc = kc
        with patch.object(kc, "_SdkConfiguration"), patch.object(
            kc, "_SdkKalshiClient"
        ) as p_client, patch.object(kc, "_read_private_key_pem", return_value="---PEM---"):
            self.sdk = MagicMock()
            p_client.return_value = self.sdk
            self.client = kc.KalshiClient()

    def test_cancel_order_calls_sdk_cancel_v2(self):
        self.sdk.cancel_order_v2.return_value = {"ok": True}
        out = self.client.cancel_order("order123")
        self.sdk.cancel_order_v2.assert_called()
        self.assertEqual(
            self.sdk.cancel_order_v2.call_args.kwargs.get("order_id"), "order123"
        )
        self.assertIsInstance(out, dict)

    def test_place_order_uses_create_order_v2_and_reports_fill(self):
        self.sdk.create_order_v2.return_value = {
            "order_id": "ord-1",
            "client_order_id": "cid",
            "fill_count": "3.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.56",
            "average_fee_paid": "0.01",
            "ts_ms": 1,
        }
        resp = self.client.place_order(
            f"{config.BTC_SERIES_TICKER}-TEST", "yes", 3, 56, dry_run=False
        )
        self.sdk.create_order_v2.assert_called_once()
        order = resp["order"]
        self.assertEqual(order["status"], "executed")
        self.assertEqual(order["fill_count"], 3)
        self.assertEqual(order["remaining_count"], 0)
        self.assertEqual(order["average_fill_price_cents"], 56)

    def test_unfilled_ioc_order_reports_unfilled_status(self):
        self.sdk.create_order_v2.return_value = {
            "order_id": "ord-2",
            "fill_count": "0.00",
            "remaining_count": "0.00",
            "average_fill_price": None,
        }
        resp = self.client.place_order(
            f"{config.BTC_SERIES_TICKER}-TEST", "yes", 3, 56, dry_run=False
        )
        self.assertEqual(resp["order"]["status"], "unfilled")
        self.assertEqual(resp["order"]["fill_count"], 0)

    def test_no_side_fill_price_converted_from_yes_book(self):
        # A NO buy fills on the YES book; average_fill_price 0.60 == 40c NO.
        self.sdk.create_order_v2.return_value = {
            "order_id": "ord-3",
            "fill_count": "1.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.60",
        }
        resp = self.client.place_order(
            f"{config.BTC_SERIES_TICKER}-TEST", "no", 1, 40, dry_run=False
        )
        self.assertEqual(resp["order"]["average_fill_price_cents"], 40)

    def test_get_orderbook_calls_sdk_market_orderbook(self):
        self.sdk.get_market_orderbook.return_value = {"orderbook": {"yes": [], "no": []}}
        out = self.client.get_orderbook("TICKER", depth=10)
        self.sdk.get_market_orderbook.assert_called()
        self.assertEqual(out["orderbook"]["yes"], [])


if __name__ == "__main__":
    unittest.main()
