import unittest
import json
import time
from unittest.mock import MagicMock, patch

from websocket_client import KalshiWebSocketClient


def _snapshot_msg(ticker="BTCZ-TEST", yes=None, no=None, seq=None, sid=None):
    msg = {
        "type": "orderbook_snapshot",
        "msg": {
            "market_ticker": ticker,
            "yes": yes if yes is not None else [[55, 10], [54, 5]],
            "no": no if no is not None else [[45, 8]],
        },
    }
    if seq is not None:
        msg["seq"] = seq
    if sid is not None:
        msg["sid"] = sid
    return msg


def _delta_msg(ticker="BTCZ-TEST", side="yes", price=55, delta=-1, seq=None, sid=None):
    msg = {
        "type": "orderbook_delta",
        "msg": {
            "market_ticker": ticker,
            "side": side,
            "price": price,
            "delta": delta,
        },
    }
    if seq is not None:
        msg["seq"] = seq
    if sid is not None:
        msg["sid"] = sid
    return msg


def _sent_commands(ws_mock):
    """Decode every message sent on a mocked WebSocket."""
    return [json.loads(call.args[0]) for call in ws_mock.send.call_args_list]


class TestKalshiWebSocketOrderbook(unittest.TestCase):
    def setUp(self):
        self.client = self._make_client()

    @staticmethod
    def _make_client(**kwargs):
        with patch.object(KalshiWebSocketClient, "_load_private_key", return_value=None):
            return KalshiWebSocketClient(**kwargs)

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

        def stop_after_first_run(*args, **kwargs):
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

    @patch("websocket_client.websocket.WebSocketApp")
    def test_run_websocket_enables_ping_keepalive(self, web_socket_app):
        self.client._running = True
        self.client._sign = MagicMock(return_value="signed")

        app = MagicMock()

        def stop_after_first_run(*args, **kwargs):
            self.client._running = False

        app.run_forever.side_effect = stop_after_first_run
        web_socket_app.return_value = app

        self.client._run_websocket()

        run_kwargs = app.run_forever.call_args.kwargs
        self.assertGreater(run_kwargs.get("ping_interval", 0), 0)
        self.assertGreater(run_kwargs.get("ping_timeout", 0), 0)

    # ------------------------------------------------------------------
    # Reconnect behavior: resubscribe + stale-book invalidation
    # ------------------------------------------------------------------

    def test_disconnect_invalidates_cached_books(self):
        self.client._connected = True
        self.client.ws = MagicMock()
        self.client.subscribe_to_market("BTCZ-TEST")
        self.client._handle_orderbook_update(_snapshot_msg())
        self.assertIsNotNone(self.client.get_latest_orderbook("BTCZ-TEST"))

        self.client._on_close(self.client.ws, 1006, "abnormal closure")

        self.assertFalse(self.client.is_connected())
        # The frozen pre-disconnect book must not be served.
        self.assertIsNone(self.client.get_latest_orderbook("BTCZ-TEST"))
        self.assertFalse(self.client.has_orderbook("BTCZ-TEST"))

    def test_reconnect_resubscribes_previously_subscribed_markets(self):
        self.client._connected = True
        self.client.ws = MagicMock()
        self.client.subscribe_to_market("BTCZ-TEST")
        self.client._handle_orderbook_update(_snapshot_msg())

        # Connection drops, then a new socket opens.
        self.client._on_close(self.client.ws, 1006, "abnormal closure")
        new_ws = MagicMock()
        self.client.ws = new_ws
        self.client._on_open(new_ws)

        subscribes = [m for m in _sent_commands(new_ws) if m.get("cmd") == "subscribe"]
        self.assertEqual(len(subscribes), 1)
        self.assertEqual(subscribes[0]["params"]["market_ticker"], "BTCZ-TEST")

        # Still no book until a fresh snapshot arrives...
        self.assertIsNone(self.client.get_latest_orderbook("BTCZ-TEST"))

        # ...and the fresh snapshot repopulates it.
        self.client._handle_orderbook_update(_snapshot_msg(yes=[[60, 3]], no=[[40, 2]]))
        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest, {"yes": [[60, 3]], "no": [[40, 2]]})

    # ------------------------------------------------------------------
    # Staleness guard
    # ------------------------------------------------------------------

    def test_fresh_messages_keep_orderbook_available(self):
        client = self._make_client(stale_after_seconds=30.0)
        client._connected = True
        client.ws = MagicMock()
        client._on_message(client.ws, json.dumps(_snapshot_msg()))

        self.assertIsNotNone(client.get_latest_orderbook("BTCZ-TEST"))
        client.ws.close.assert_not_called()

    def test_stale_connection_returns_none_and_forces_reconnect(self):
        client = self._make_client(stale_after_seconds=5.0)
        client._connected = True
        client.ws = MagicMock()
        client._on_message(client.ws, json.dumps(_snapshot_msg()))

        # Simulate silence beyond the staleness window on a half-open socket.
        client._last_message_time = time.monotonic() - 6.0

        self.assertIsNone(client.get_latest_orderbook("BTCZ-TEST"))
        client.ws.close.assert_called_once()
        self.assertFalse(client.is_connected())
        # The stale book stays invalidated until a fresh snapshot arrives.
        self.assertFalse(client.has_orderbook("BTCZ-TEST"))

    def test_on_message_refreshes_last_message_time(self):
        client = self._make_client(stale_after_seconds=5.0)
        client._connected = True
        client.ws = MagicMock()
        client._last_message_time = time.monotonic() - 100.0

        client._on_message(client.ws, json.dumps(_snapshot_msg()))

        self.assertFalse(client._is_stale())
        self.assertIsNotNone(client.get_latest_orderbook("BTCZ-TEST"))

    # ------------------------------------------------------------------
    # Sequence-gap handling
    # ------------------------------------------------------------------

    def test_in_order_sequence_deltas_are_applied(self):
        self.client._connected = True
        self.client.ws = MagicMock()
        self.client._handle_orderbook_update(_snapshot_msg(seq=1, sid=7))
        self.client._handle_orderbook_update(_delta_msg(price=55, delta=-3, seq=2, sid=7))

        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest["yes"], [[55, 7], [54, 5]])

    def test_sequence_gap_drops_book_and_resubscribes(self):
        self.client._connected = True
        self.client.ws = MagicMock()
        self.client.subscribe_to_market("BTCZ-TEST")
        self.client._handle_orderbook_update(_snapshot_msg(seq=1, sid=7))
        self.client._handle_orderbook_update(_delta_msg(price=55, delta=-3, seq=2, sid=7))

        # seq=4 arrives: seq=3 was dropped, so the book is corrupt.
        self.client._handle_orderbook_update(_delta_msg(price=54, delta=-5, seq=4, sid=7))

        # The cached book is gone until a fresh snapshot arrives.
        self.assertIsNone(self.client.get_latest_orderbook("BTCZ-TEST"))

        # The client unsubscribed (by sid) and re-subscribed for a snapshot.
        sent = _sent_commands(self.client.ws)
        unsubscribes = [m for m in sent if m.get("cmd") == "unsubscribe"]
        self.assertEqual(len(unsubscribes), 1)
        self.assertEqual(unsubscribes[0]["params"]["sids"], [7])
        subscribes = [m for m in sent if m.get("cmd") == "subscribe"]
        self.assertEqual(subscribes[-1]["params"]["market_ticker"], "BTCZ-TEST")

        # Deltas arriving before the new snapshot are ignored.
        self.client._handle_orderbook_update(_delta_msg(price=55, delta=1, seq=5, sid=8))
        self.assertIsNone(self.client.get_latest_orderbook("BTCZ-TEST"))

        # The fresh snapshot restarts the sequence and restores the book.
        self.client._handle_orderbook_update(
            _snapshot_msg(yes=[[52, 4]], no=[[48, 6]], seq=10, sid=8)
        )
        self.client._handle_orderbook_update(_delta_msg(price=52, delta=2, seq=11, sid=8))
        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest, {"yes": [[52, 6]], "no": [[48, 6]]})

    def test_deltas_without_sequence_numbers_still_apply(self):
        # Legacy payloads without seq must keep working (no gap check possible).
        self.client._handle_orderbook_update(_snapshot_msg())
        self.client._handle_orderbook_update(_delta_msg(price=55, delta=-3))

        latest = self.client.get_latest_orderbook("BTCZ-TEST")
        self.assertEqual(latest["yes"], [[55, 7], [54, 5]])

    # ------------------------------------------------------------------
    # Reconnect backoff
    # ------------------------------------------------------------------

    @patch("websocket_client.time.sleep")
    @patch("websocket_client.websocket.WebSocketApp")
    def test_backoff_grows_per_consecutive_failure(self, web_socket_app, sleep_mock):
        self.client._running = True
        self.client._sign = MagicMock(return_value="signed")

        app = MagicMock()
        attempts = {"count": 0}

        def failing_run_forever(*args, **kwargs):
            attempts["count"] += 1
            if attempts["count"] >= 4:
                self.client._running = False

        app.run_forever.side_effect = failing_run_forever
        web_socket_app.return_value = app

        self.client._run_websocket()

        delays = [call.args[0] for call in sleep_mock.call_args_list]
        self.assertEqual(delays, [1, 2, 4])

    def test_backoff_resets_only_after_successful_open(self):
        self.client._reconnect_attempts = 5
        self.client._on_open(MagicMock())
        self.assertEqual(self.client._reconnect_attempts, 0)

    # ------------------------------------------------------------------
    # Price normalization
    # ------------------------------------------------------------------

    def test_normalize_price_rounds_dollar_strings_correctly(self):
        # int(float("0.07") * 100) == 6 — the truncation artifact must not leak.
        self.assertEqual(KalshiWebSocketClient._normalize_price("0.07"), 7)
        self.assertEqual(KalshiWebSocketClient._normalize_price("0.29"), 29)
        self.assertEqual(KalshiWebSocketClient._normalize_price("0.57"), 57)
        self.assertEqual(KalshiWebSocketClient._normalize_price("0.58"), 58)
        self.assertEqual(KalshiWebSocketClient._normalize_price("0.5800"), 58)
        self.assertEqual(KalshiWebSocketClient._normalize_price("55"), 55)
        self.assertEqual(KalshiWebSocketClient._normalize_price(55), 55)
        self.assertEqual(KalshiWebSocketClient._normalize_price(0.55), 55)
        self.assertIsNone(KalshiWebSocketClient._normalize_price(None))
        self.assertIsNone(KalshiWebSocketClient._normalize_price("not-a-price"))

    # ------------------------------------------------------------------
    # Unsubscribe / pruning of expired markets
    # ------------------------------------------------------------------

    def test_unsubscribe_removes_market_and_cached_book(self):
        self.client._connected = True
        self.client.ws = MagicMock()
        self.client.subscribe_to_market("BTCZ-TEST")
        self.client._handle_orderbook_update(_snapshot_msg(seq=1, sid=7))

        self.client.unsubscribe_from_market("BTCZ-TEST")

        self.assertNotIn("BTCZ-TEST", self.client._subscribed_markets)
        self.assertIsNone(self.client.get_latest_orderbook("BTCZ-TEST"))
        self.assertFalse(self.client.has_orderbook("BTCZ-TEST"))
        unsubscribes = [
            m for m in _sent_commands(self.client.ws) if m.get("cmd") == "unsubscribe"
        ]
        self.assertEqual(len(unsubscribes), 1)
        self.assertEqual(unsubscribes[0]["params"]["sids"], [7])

    def test_expired_markets_are_pruned_when_subscribing_to_new_window(self):
        client = self._make_client(prune_after_seconds=60.0)
        client._connected = True
        client.ws = MagicMock()

        client.subscribe_to_market("BTCZ-OLD")
        client._handle_orderbook_update(_snapshot_msg(ticker="BTCZ-OLD", sid=3))

        # The old 15-minute window expires: nothing touches it for a while.
        client._last_access["BTCZ-OLD"] = time.monotonic() - 120.0

        client.subscribe_to_market("BTCZ-NEW")

        self.assertNotIn("BTCZ-OLD", client._subscribed_markets)
        self.assertIn("BTCZ-NEW", client._subscribed_markets)
        self.assertFalse(client.has_orderbook("BTCZ-OLD"))

    def test_recently_accessed_markets_are_not_pruned(self):
        client = self._make_client(prune_after_seconds=60.0)
        client._connected = True
        client.ws = MagicMock()

        client.subscribe_to_market("BTCZ-OLD")
        client.subscribe_to_market("BTCZ-NEW")

        self.assertIn("BTCZ-OLD", client._subscribed_markets)
        self.assertIn("BTCZ-NEW", client._subscribed_markets)


if __name__ == "__main__":
    unittest.main()
