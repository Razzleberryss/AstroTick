import unittest
import json
from unittest.mock import MagicMock, patch

from websocket_client import KalshiWebSocketClient


class TestKalshiWebSocketOrderbook(unittest.TestCase):
    def setUp(self):
        with patch.object(KalshiWebSocketClient, "_load_private_key", return_value=None):
            self.client = KalshiWebSocketClient()

    def test_on_open_marks_connection_without_sending_auth_message(self):
        ws = MagicMock()
        self.client._on_open(ws)
        self.assertTrue(self.client.is_connected())
        ws.send.assert_not_called()

    def test_subscribe_to_market_uses_documented_orderbook_delta_command(self):
        self.client._connected = True
        self.client.ws = MagicMock()

        self.client.subscribe_to_market("BTCZ-TEST")

        self.client.ws.send.assert_called_once()
        message = json.loads(self.client.ws.send.call_args[0][0])
        self.assertEqual(message["params"]["channels"], ["orderbook_delta"])
        self.assertEqual(message["params"]["market_ticker"], "BTCZ-TEST")

    def test_snapshot_msg_payload_is_normalized_for_reads(self):
        self.client._handle_orderbook_update(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "BTCZ-TEST",
                    "yes_dollars": [["0.55", "10"], ["0.54", "4"]],
                    "no_dollars": [[45, 8]],
                },
            }
        )

        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest, {"yes": [[55, 10], [54, 4]], "no": [[45, 8]]})

    def test_delta_msg_updates_existing_level_without_discarding_book(self):
        self.client._handle_orderbook_update(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "BTCZ-TEST",
                    "yes": [[55, 10], [54, 5]],
                    "no": [[45, 8]],
                },
            }
        )

        self.client._handle_orderbook_update(
            {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "BTCZ-TEST",
                    "side": "yes",
                    "price": 55,
                    "delta": -3,
                },
            }
        )

        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest["yes"], [[55, 7], [54, 5]])
        self.assertEqual(latest["no"], [[45, 8]])

    def test_delta_msg_supports_documented_price_dollars_and_delta_fp_fields(self):
        self.client._handle_orderbook_update(
            {
                "type": "orderbook_snapshot",
                "msg": {
                    "market_ticker": "BTCZ-TEST",
                    "yes": [[55, 10]],
                    "no": [[45, 8]],
                },
            }
        )

        self.client._handle_orderbook_update(
            {
                "type": "orderbook_delta",
                "msg": {
                    "market_ticker": "BTCZ-TEST",
                    "side": "yes",
                    "price_dollars": "0.55",
                    "delta_fp": "-10",
                },
            }
        )

        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest["yes"], [])
        self.assertEqual(latest["no"], [[45, 8]])

    @patch("websocket_client.websocket.WebSocketApp")
    def test_run_websocket_passes_auth_in_handshake_headers(self, web_socket_app):
        self.client._running = True
        self.client._sign = MagicMock(return_value="signed")

        app = MagicMock()

        def stop_after_first_run():
            self.client._running = False

        app.run_forever.side_effect = stop_after_first_run
        web_socket_app.return_value = app

        self.client._run_websocket()

        kwargs = web_socket_app.call_args.kwargs
        self.assertIn("header", kwargs)
        headers = kwargs["header"]
        self.assertTrue(any(h.startswith("KALSHI-ACCESS-KEY:") for h in headers))
        self.assertTrue(any(h.startswith("KALSHI-ACCESS-SIGNATURE:") for h in headers))
        self.assertTrue(any(h.startswith("KALSHI-ACCESS-TIMESTAMP:") for h in headers))


if __name__ == "__main__":
    unittest.main()
