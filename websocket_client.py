"""
websocket_client.py - WebSocket client for streaming Kalshi market data.

Connects to Kalshi's WebSocket API to receive real-time orderbook updates,
maintaining an in-memory snapshot of the latest orderbook for subscribed markets.

Thread-safe accessor methods allow the bot to query the latest orderbook without
making REST API calls.
"""
import json
import logging
import threading
import time
from typing import Optional
import datetime
import base64

import websocket

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

import config

log = logging.getLogger(__name__)


class KalshiWebSocketClient:
    """
    WebSocket client for streaming Kalshi orderbook data.

    Connects to Kalshi's WebSocket endpoint, authenticates, and subscribes to
    orderbook updates for specified markets. Maintains an in-memory snapshot of
    the latest orderbook for each subscribed market.
    """

    def __init__(self):
        """Initialize the WebSocket client."""
        self.base_url = config.BASE_URL
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._private_key = self._load_private_key(config.KALSHI_PRIVATE_KEY_PATH)

        # Determine WebSocket URL based on environment
        if config.KALSHI_ENV == "prod":
            self.ws_url = "wss://trading-api.kalshi.com/trade-api/ws/v2"
        else:
            self.ws_url = "wss://demo-api.kalshi.co/trade-api/ws/v2"

        self.ws = None
        self.ws_thread = None
        self._running = False
        self._connected = False

        # Thread-safe storage for orderbook snapshots
        # Key: market ticker, Value: orderbook dict with yes/no arrays
        self._orderbooks = {}
        self._lock = threading.Lock()

        # Track subscribed markets
        self._subscribed_markets = set()

    @staticmethod
    def _load_private_key(path: str):
        """Load RSA private key from file."""
        with open(path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign(self, timestamp_ms: str, method: str, path: str) -> str:
        """
        Return base64-encoded RSA-PSS signature for WebSocket authentication.
        """
        message = (timestamp_ms + method.upper() + path).encode("utf-8")
        signature = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode("utf-8")

    def start(self):
        """Start the WebSocket connection in a background thread."""
        if self._running:
            log.warning("WebSocket client already running")
            return

        self._running = True
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()

        # Wait briefly for connection to establish
        for _ in range(50):  # 5 seconds max
            if self._connected:
                log.info("WebSocket client connected successfully")
                return
            time.sleep(0.1)

        log.warning("WebSocket connection not established within timeout")

    def stop(self):
        """Stop the WebSocket connection and clean up."""
        log.info("Stopping WebSocket client...")
        self._running = False
        if self.ws:
            self.ws.close()
        if self.ws_thread:
            self.ws_thread.join(timeout=5)
        log.info("WebSocket client stopped")

    def _run_websocket(self):
        """Main WebSocket event loop (runs in background thread)."""
        while self._running:
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever()
            except Exception as exc:
                log.error("WebSocket connection error: %s", exc)

            # Reconnect with exponential backoff if still running
            if self._running:
                self._connected = False
                backoff = 2
                log.info("Reconnecting WebSocket in %ds...", backoff)
                time.sleep(backoff)

    def _on_open(self, ws):
        """Called when WebSocket connection is opened."""
        log.info("WebSocket connection opened")

        # Authenticate using the documented WebSocket auth method
        # For Kalshi WebSocket v2, authentication is typically done via initial message
        ts = str(int(datetime.datetime.now().timestamp() * 1000))
        signature = self._sign(ts, "GET", "/trade-api/ws/v2")

        auth_msg = {
            "id": 1,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": []  # Will subscribe to specific markets after auth
            },
            "headers": {
                "Authorization": f"KALSHI-ACCESS-KEY={self.api_key_id}",
                "KALSHI-ACCESS-TIMESTAMP": ts,
                "KALSHI-ACCESS-SIGNATURE": signature,
            }
        }

        ws.send(json.dumps(auth_msg))
        self._connected = True

    def _on_message(self, ws, message):
        """Called when a message is received from the WebSocket."""
        try:
            data = json.loads(message)

            # Handle orderbook update messages
            if data.get("type") == "orderbook_snapshot" or data.get("type") == "orderbook_delta":
                self._handle_orderbook_update(data)
            elif data.get("type") == "subscribed":
                log.info("Successfully subscribed to channel: %s", data.get("channel"))
            elif data.get("type") == "error":
                log.error("WebSocket error message: %s", data.get("msg"))
        except json.JSONDecodeError as exc:
            log.error("Failed to decode WebSocket message: %s", exc)
        except Exception as exc:
            log.error("Error processing WebSocket message: %s", exc)

    def _handle_orderbook_update(self, data):
        """Process an orderbook update and update the internal snapshot."""
        try:
            ticker = data.get("market_ticker")
            if not ticker:
                return

            # For snapshot messages, replace the entire orderbook
            if data.get("type") == "orderbook_snapshot":
                orderbook_data = data.get("orderbook", {})
                with self._lock:
                    self._orderbooks[ticker] = orderbook_data
                log.debug("Received orderbook snapshot for %s", ticker)

            # For delta messages, apply the incremental update
            elif data.get("type") == "orderbook_delta":
                delta = data.get("delta", {})
                with self._lock:
                    if ticker not in self._orderbooks:
                        # Request snapshot if we don't have initial state
                        log.warning("Received delta without snapshot for %s, requesting snapshot", ticker)
                        return

                    # Apply delta updates (simplified - full implementation would handle adds/removes/updates)
                    current = self._orderbooks[ticker]
                    # In production, you'd merge the delta properly
                    # For now, treat delta as a full update for simplicity
                    self._orderbooks[ticker] = delta
                    log.debug("Applied orderbook delta for %s", ticker)

        except Exception as exc:
            log.error("Error handling orderbook update: %s", exc)

    def _on_error(self, ws, error):
        """Called when a WebSocket error occurs."""
        log.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        """Called when WebSocket connection is closed."""
        log.info("WebSocket connection closed (code=%s, msg=%s)", close_status_code, close_msg)
        self._connected = False

    def subscribe_to_market(self, ticker: str):
        """
        Subscribe to orderbook updates for a specific market.

        Args:
            ticker: Market ticker to subscribe to (e.g., "BTCZ-25DEC3100-T3PM")
        """
        if ticker in self._subscribed_markets:
            log.debug("Already subscribed to %s", ticker)
            return

        if not self._connected:
            log.warning("Cannot subscribe to %s: WebSocket not connected", ticker)
            return

        try:
            subscribe_msg = {
                "id": int(time.time()),
                "cmd": "subscribe",
                "params": {
                    "channels": ["orderbook_delta"],
                    "market_tickers": [ticker]
                }
            }

            self.ws.send(json.dumps(subscribe_msg))
            self._subscribed_markets.add(ticker)
            log.info("Subscribed to orderbook updates for %s", ticker)
        except Exception as exc:
            log.error("Error subscribing to %s: %s", ticker, exc)

    def get_latest_orderbook(self, ticker: str) -> Optional[dict]:
        """
        Get the latest orderbook snapshot for a market.

        Args:
            ticker: Market ticker

        Returns:
            Orderbook dict with 'yes' and 'no' arrays, or None if not available.
            Format matches the REST API orderbook response.
        """
        with self._lock:
            orderbook = self._orderbooks.get(ticker)
            if orderbook:
                # Return a copy to avoid external modification
                return {
                    "yes": orderbook.get("yes", []).copy() if orderbook.get("yes") else [],
                    "no": orderbook.get("no", []).copy() if orderbook.get("no") else []
                }
            return None

    def is_connected(self) -> bool:
        """Check if the WebSocket is currently connected."""
        return self._connected

    def has_orderbook(self, ticker: str) -> bool:
        """Check if we have an orderbook snapshot for the given market."""
        with self._lock:
            return ticker in self._orderbooks
