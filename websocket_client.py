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

    Reliability behavior:
    - Reconnects with exponential backoff that grows per consecutive failure
      and resets only after a successful handshake. On reconnect it re-sends
      subscribe commands for every previously subscribed market, and no cached
      book is served until a fresh orderbook_snapshot arrives.
    - Detects half-open connections with transport-level ping/pong plus a
      staleness guard: when no message has arrived within stale_after_seconds,
      get_latest_orderbook() returns None (so callers fall back to REST) and a
      reconnect is forced.
    - Tracks the per-market message sequence number (seq); on a gap the cached
      book is dropped and the market re-subscribed to force a fresh snapshot.
    - Prunes subscriptions and cached books for markets that have not been
      accessed recently (expired 15-minute markets).
    """

    # Transport-level keepalive: run_forever() sends a ping every
    # PING_INTERVAL_SECONDS and tears the connection down when no pong arrives
    # within PING_TIMEOUT_SECONDS, so half-open TCP connections are detected
    # and the reconnect loop takes over instead of blocking on recv forever.
    PING_INTERVAL_SECONDS = 10
    PING_TIMEOUT_SECONDS = 5

    def __init__(self, stale_after_seconds: float = 30.0, prune_after_seconds: float = 1800.0):
        """
        Initialize the WebSocket client.

        Args:
            stale_after_seconds: If no WebSocket message arrives within this
                window while connected, cached orderbooks are treated as stale:
                get_latest_orderbook() returns None (callers fall back to REST)
                and a reconnect is forced.
            prune_after_seconds: Markets neither subscribed to nor read for this
                long (e.g. expired 15-minute markets) are unsubscribed and their
                cached books dropped.
        """
        self.base_url = config.BASE_URL
        self.api_key_id = config.KALSHI_API_KEY_ID
        self._private_key = self._load_private_key(config.KALSHI_PRIVATE_KEY_PATH)

        # Determine WebSocket URL based on environment
        if config.KALSHI_ENV == "prod":
            self.ws_url = "wss://api.elections.kalshi.com/trade-api/ws/v2"
        else:
            self.ws_url = "wss://demo-api.kalshi.co/trade-api/ws/v2"

        self.ws = None
        self.ws_thread = None
        self._running = False
        self._connected = False

        # Staleness / pruning configuration
        self.stale_after_seconds = stale_after_seconds
        self.prune_after_seconds = prune_after_seconds

        # Thread-safe storage for orderbook snapshots
        # Key: market ticker, Value: orderbook dict with yes/no arrays
        self._orderbooks = {}
        self._lock = threading.Lock()

        # Per-ticker last-seen message sequence number (protected by _lock,
        # alongside _orderbooks) used to detect dropped deltas.
        self._sequences = {}

        # Per-ticker subscription id (sid) reported by the server, needed to
        # send unsubscribe commands. Written only on the WS callback thread.
        self._sids = {}

        # Per-ticker last access time (subscribe or read), used to prune
        # expired markets that nothing asks about any more.
        self._last_access = {}

        # time.monotonic() of the last message received on the current
        # connection; None until the first connection opens.
        self._last_message_time = None

        # Consecutive failed connection attempts. Reset only in _on_open, after
        # a successful handshake, so the reconnect backoff actually grows.
        self._reconnect_attempts = 0

        # Signalled by _on_open so start() can wait without a busy-poll loop
        self._connected_event = threading.Event()

        # Track subscribed markets
        self._subscribed_markets = set()
        self._message_id = 1

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

        # Clear any stale signal from a previous run so the wait below always
        # reflects the outcome of THIS handshake attempt, not a prior one.
        self._connected_event.clear()

        self._running = True
        self.ws_thread = threading.Thread(target=self._run_websocket, daemon=True)
        self.ws_thread.start()

        # Block until the WebSocket handshake completes or the timeout expires.
        # threading.Event.wait() releases the GIL while sleeping, so this is
        # far cheaper than the previous busy-poll (50 × time.sleep(0.1)).
        if self._connected_event.wait(timeout=5.0):
            log.info("WebSocket client connected successfully")
        else:
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
                ts = str(int(datetime.datetime.now().timestamp() * 1000))
                signature = self._sign(ts, "GET", "/trade-api/ws/v2")
                headers = [
                    f"KALSHI-ACCESS-KEY: {self.api_key_id}",
                    f"KALSHI-ACCESS-SIGNATURE: {signature}",
                    f"KALSHI-ACCESS-TIMESTAMP: {ts}",
                ]
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    header=headers,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(
                    ping_interval=self.PING_INTERVAL_SECONDS,
                    ping_timeout=self.PING_TIMEOUT_SECONDS,
                )
            except Exception as exc:
                log.error("WebSocket connection error: %s", exc)

            if self._running:
                self._connected = False
                # _reconnect_attempts is reset only in _on_open after a
                # successful handshake, so the backoff grows across
                # consecutive failures instead of being pinned at 1s.
                backoff = min(2 ** self._reconnect_attempts, 60)
                self._reconnect_attempts += 1
                log.info("Reconnecting WebSocket in %ds...", backoff)
                time.sleep(backoff)

    def _on_open(self, ws):
        """Called when WebSocket connection is opened."""
        log.info("WebSocket connection opened")
        self._reconnect_attempts = 0
        self._last_message_time = time.monotonic()
        self._connected = True
        self._connected_event.set()
        # Re-subscribe to every market that was subscribed before the
        # disconnect; the server replies with fresh orderbook_snapshots that
        # repopulate the books cleared in _on_close.
        self._resubscribe_all()

    def _resubscribe_all(self):
        """Re-send subscribe commands for all previously subscribed markets."""
        for ticker in sorted(self._subscribed_markets):
            try:
                self._send_subscribe(ticker)
                log.info("Re-subscribed to orderbook updates for %s", ticker)
            except Exception as exc:
                log.error("Error re-subscribing to %s: %s", ticker, exc)

    def _on_message(self, ws, message):
        """Called when a message is received from the WebSocket."""
        self._last_message_time = time.monotonic()
        try:
            data = json.loads(message)

            # Handle orderbook update messages
            if data.get("type") == "orderbook_snapshot" or data.get("type") == "orderbook_delta":
                self._handle_orderbook_update(data)
            elif data.get("type") == "subscribed":
                channel = data.get("msg", {}).get("channel")
                log.info("Successfully subscribed to channel: %s", channel)
            elif data.get("type") == "error":
                log.error("WebSocket error message: %s", data.get("msg"))
        except json.JSONDecodeError as exc:
            log.error("Failed to decode WebSocket message: %s", exc)
        except Exception as exc:
            log.error("Error processing WebSocket message: %s", exc)

    @staticmethod
    def _coerce_seq(value) -> Optional[int]:
        """Coerce a sequence number into an int, or None when absent/invalid."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _handle_orderbook_update(self, data):
        """Process an orderbook update and update the internal snapshot."""
        try:
            payload = data.get("msg", {})
            ticker = payload.get("market_ticker")
            if not ticker:
                return

            seq = self._coerce_seq(data.get("seq", payload.get("seq")))
            sid = data.get("sid", payload.get("sid"))
            if sid is not None:
                self._sids[ticker] = sid

            # For snapshot messages, replace the entire orderbook
            if data.get("type") == "orderbook_snapshot":
                orderbook_data = payload
                # All WebSocket callbacks fire on a single background thread
                # (the websocket-client library is single-threaded per connection),
                # so there is no concurrent writer for the same ticker here.
                # We normalize outside the lock to keep lock-hold time minimal:
                # only the final dict-swap is protected, allowing the main thread
                # to read the previous snapshot without blocking during parsing.
                normalized = self._normalize_orderbook(orderbook_data)
                with self._lock:
                    self._orderbooks[ticker] = normalized
                    if seq is not None:
                        self._sequences[ticker] = seq
                    else:
                        self._sequences.pop(ticker, None)
                log.debug("Received orderbook snapshot for %s", ticker)

            # For delta messages, apply the incremental update
            elif data.get("type") == "orderbook_delta":
                delta = payload
                expected_seq = None
                with self._lock:
                    if ticker not in self._orderbooks:
                        log.warning(
                            "Received delta without snapshot for %s; waiting for orderbook_snapshot",
                            ticker,
                        )
                        return

                    last_seq = self._sequences.get(ticker)
                    if seq is not None and last_seq is not None and seq != last_seq + 1:
                        # A sequence gap means deltas were dropped: the cached
                        # book can no longer be trusted. Drop it and force a
                        # fresh snapshot by re-subscribing (outside the lock).
                        expected_seq = last_seq + 1
                        self._orderbooks.pop(ticker, None)
                        self._sequences.pop(ticker, None)
                    else:
                        if seq is not None:
                            self._sequences[ticker] = seq
                        current = self._orderbooks[ticker]
                        self._orderbooks[ticker] = self._apply_delta(current, delta)
                        log.debug("Applied orderbook delta for %s", ticker)

                if expected_seq is not None:
                    log.warning(
                        "Orderbook sequence gap for %s (expected seq %s, got %s); "
                        "dropped cached book, re-requesting snapshot",
                        ticker,
                        expected_seq,
                        seq,
                    )
                    self._resync_market(ticker)

        except Exception as exc:
            log.error("Error handling orderbook update: %s", exc)

    def _resync_market(self, ticker: str):
        """Force a fresh orderbook_snapshot for a market by re-subscribing."""
        if not self._connected or not self.ws:
            return
        try:
            self._send_unsubscribe(ticker)
            self._send_subscribe(ticker)
            log.info("Re-subscribed to %s to recover a fresh orderbook snapshot", ticker)
        except Exception as exc:
            log.error("Error re-subscribing to %s: %s", ticker, exc)

    def _on_error(self, ws, error):
        """Called when a WebSocket error occurs."""
        log.error("WebSocket error: %s", error)

    def _on_close(self, ws, close_status_code, close_msg):
        """Called when WebSocket connection is closed."""
        log.info("WebSocket connection closed (code=%s, msg=%s)", close_status_code, close_msg)
        self._connected = False
        self._connected_event.clear()
        # Cached books stop updating the moment the connection drops: clear
        # them so readers fall back to REST until fresh snapshots arrive after
        # the reconnect re-subscribes (see _on_open/_resubscribe_all).
        with self._lock:
            self._orderbooks.clear()
            self._sequences.clear()
        self._sids.clear()

    def subscribe_to_market(self, ticker: str):
        """
        Subscribe to orderbook updates for a specific market.

        Args:
            ticker: Market ticker to subscribe to (e.g., "BTCZ-25DEC3100-T3PM")
        """
        now = time.monotonic()
        self._last_access[ticker] = now

        if ticker in self._subscribed_markets:
            log.debug("Already subscribed to %s", ticker)
            self._prune_stale_markets(now)
            return

        if not self._connected:
            log.warning("Cannot subscribe to %s: WebSocket not connected", ticker)
            return

        try:
            self._send_subscribe(ticker)
            self._subscribed_markets.add(ticker)
            log.info("Subscribed to orderbook updates for %s", ticker)
        except Exception as exc:
            log.error("Error subscribing to %s: %s", ticker, exc)
            return

        self._prune_stale_markets(now)

    def unsubscribe_from_market(self, ticker: str):
        """
        Unsubscribe from orderbook updates for a market and drop its cached
        state (book, sequence, access record).

        Args:
            ticker: Market ticker to unsubscribe from
        """
        if self._connected and self.ws:
            try:
                self._send_unsubscribe(ticker)
                log.info("Unsubscribed from orderbook updates for %s", ticker)
            except Exception as exc:
                log.error("Error unsubscribing from %s: %s", ticker, exc)
        self._subscribed_markets.discard(ticker)
        self._sids.pop(ticker, None)
        self._last_access.pop(ticker, None)
        with self._lock:
            self._orderbooks.pop(ticker, None)
            self._sequences.pop(ticker, None)

    def _send_subscribe(self, ticker: str):
        """Send the subscribe command for a market over the current socket."""
        subscribe_msg = {
            "id": self._next_message_id(),
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_ticker": ticker,
            }
        }
        self.ws.send(json.dumps(subscribe_msg))

    def _send_unsubscribe(self, ticker: str):
        """Send the unsubscribe command for a market, if its sid is known."""
        sid = self._sids.pop(ticker, None)
        if sid is None:
            return
        unsubscribe_msg = {
            "id": self._next_message_id(),
            "cmd": "unsubscribe",
            "params": {
                "sids": [sid],
            }
        }
        self.ws.send(json.dumps(unsubscribe_msg))

    def _prune_stale_markets(self, now: Optional[float] = None):
        """Unsubscribe markets that have not been accessed recently.

        bot.py subscribes to a new 15-minute market every window and only ever
        reads the active one, so anything untouched for prune_after_seconds is
        an expired market whose deltas just waste bandwidth and memory.
        """
        if now is None:
            now = time.monotonic()
        for ticker in sorted(self._subscribed_markets):
            last_access = self._last_access.get(ticker)
            if last_access is not None and (now - last_access) <= self.prune_after_seconds:
                continue
            log.info("Pruning expired market subscription: %s", ticker)
            self.unsubscribe_from_market(ticker)

    def get_latest_orderbook(self, ticker: str) -> Optional[dict]:
        """
        Get the latest orderbook snapshot for a market.

        Args:
            ticker: Market ticker

        Returns:
            Orderbook dict with 'yes' and 'no' arrays, or None if not available
            or the connection has gone stale (no message within
            stale_after_seconds) — in the stale case a reconnect is forced and
            the caller should fall back to REST.
            Format matches the REST API orderbook response.
        """
        self._last_access[ticker] = time.monotonic()

        if self._connected and self._is_stale():
            log.warning(
                "WebSocket silent for over %.0fs; treating orderbooks as stale "
                "and forcing a reconnect",
                self.stale_after_seconds,
            )
            self._force_reconnect()
            return None

        with self._lock:
            orderbook = self._orderbooks.get(ticker)
            if orderbook:
                yes = orderbook.get("yes")
                no = orderbook.get("no")
                return {
                    "yes": list(yes) if yes else [],
                    "no": list(no) if no else [],
                }
            return None

    def _is_stale(self) -> bool:
        """True when no message has arrived within stale_after_seconds."""
        if self._last_message_time is None:
            return False
        return (time.monotonic() - self._last_message_time) > self.stale_after_seconds

    def _force_reconnect(self):
        """Tear down the current socket so _run_websocket reconnects.

        Invalidates all cached books immediately (a fresh snapshot is required
        after the reconnect) rather than waiting for the _on_close callback.
        """
        self._connected = False
        with self._lock:
            self._orderbooks.clear()
            self._sequences.clear()
        ws = self.ws
        if ws is None:
            return
        try:
            ws.close()
        except Exception as exc:
            log.debug("Error closing stale WebSocket: %s", exc)

    @staticmethod
    def _normalize_price(price) -> Optional[int]:
        """Normalize a price into integer cents."""
        if price is None:
            return None
        try:
            if isinstance(price, str):
                # round() before int() avoids the float artifact where e.g.
                # int(float("0.07") * 100) == 6 truncates by one cent.
                return int(round(float(price) * 100)) if "." in price else int(price)
            if isinstance(price, float) and 0 <= price <= 1:
                return int(round(price * 100))
            return int(price)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _normalize_levels(cls, levels) -> list[list[int]]:
        """Normalize a side of the book into [[price_cents, size], ...]."""
        if not levels:
            return []

        if isinstance(levels, dict):
            levels = levels.get("bids") or levels.get("levels") or []

        normalized = []
        for level in levels:
            price = None
            size = None

            if isinstance(level, dict):
                price = level.get("price")
                if price is None:
                    price = level.get("price_dollars")
                size = level.get("size")
                if size is None:
                    size = level.get("count")
                if size is None and level.get("count_fp") is not None:
                    try:
                        size = int(float(level["count_fp"]))
                    except (TypeError, ValueError):
                        size = None
            elif isinstance(level, (list, tuple)) and len(level) >= 2:
                price, size = level[0], level[1]

            price_cents = cls._normalize_price(price)
            if price_cents is None:
                continue

            try:
                size_int = int(size)
            except (TypeError, ValueError):
                continue

            if size_int > 0:
                normalized.append([price_cents, size_int])

        if len(normalized) > 1:
            normalized.sort(key=lambda item: item[0], reverse=True)
        return normalized

    @classmethod
    def _normalize_orderbook(cls, orderbook) -> dict:
        """Normalize orderbook payloads into a consistent yes/no list format."""
        if not isinstance(orderbook, dict):
            return {"yes": [], "no": []}

        yes_levels = (
            orderbook.get("yes")
            or orderbook.get("yes_dollars")
            or orderbook.get("yes_dollars_fp")
            or []
        )
        no_levels = (
            orderbook.get("no")
            or orderbook.get("no_dollars")
            or orderbook.get("no_dollars_fp")
            or []
        )

        return {
            "yes": cls._normalize_levels(yes_levels),
            "no": cls._normalize_levels(no_levels),
        }

    @classmethod
    def _apply_side_delta(cls, current_levels, side_delta) -> list[list[int]]:
        """Apply a delta payload to a single orderbook side."""
        current_map = {price: size for price, size in cls._normalize_levels(current_levels)}

        # Full replacement delta: {"yes": [[55, 10], ...]} or side list payload
        if isinstance(side_delta, (list, tuple)):
            return cls._normalize_levels(side_delta)
        if isinstance(side_delta, dict) and (
            side_delta.get("bids") is not None or side_delta.get("levels") is not None
        ):
            return cls._normalize_levels(side_delta)

        updates = side_delta if isinstance(side_delta, list) else [side_delta]
        for update in updates:
            if not isinstance(update, dict):
                continue
            price_cents = cls._normalize_price(update.get("price"))
            if price_cents is None:
                price_cents = cls._normalize_price(update.get("price_dollars"))
            if price_cents is None:
                continue

            raw_size = update.get("size")
            if raw_size is None:
                raw_size = update.get("count")
            if raw_size is None:
                raw_size = update.get("quantity")
            if raw_size is None and update.get("count_fp") is not None:
                try:
                    raw_size = int(float(update["count_fp"]))
                except (TypeError, ValueError):
                    raw_size = None

            if raw_size is not None:
                try:
                    size_int = int(raw_size)
                except (TypeError, ValueError):
                    continue
                if size_int <= 0:
                    current_map.pop(price_cents, None)
                else:
                    current_map[price_cents] = size_int
                continue

            raw_delta = update.get("delta")
            if raw_delta is None:
                raw_delta = update.get("delta_fp")
            if raw_delta is not None:
                try:
                    delta_int = int(float(raw_delta))
                except (TypeError, ValueError):
                    continue
                new_size = current_map.get(price_cents, 0) + delta_int
                if new_size <= 0:
                    current_map.pop(price_cents, None)
                else:
                    current_map[price_cents] = new_size

        return [[price, size] for price, size in sorted(current_map.items(), reverse=True)]

    @classmethod
    def _apply_delta(cls, current_orderbook, delta) -> dict:
        """Apply a delta payload without discarding the existing snapshot."""
        current = cls._normalize_orderbook(current_orderbook)

        if not isinstance(delta, dict):
            return current

        # Some payloads send a full yes/no replacement inside delta.
        if "yes" in delta or "no" in delta:
            return {
                "yes": cls._apply_side_delta(current.get("yes", []), delta.get("yes", [])),
                "no": cls._apply_side_delta(current.get("no", []), delta.get("no", [])),
            }

        # Other payloads send a single side/price update.
        side = delta.get("side")
        if side in ("yes", "no"):
            updated = dict(current)
            updated[side] = cls._apply_side_delta(current.get(side, []), delta)
            return updated

        return current

    def _next_message_id(self) -> int:
        """Return the next client message id for websocket commands."""
        message_id = self._message_id
        self._message_id += 1
        return message_id

    def is_connected(self) -> bool:
        """Check if the WebSocket is currently connected."""
        return self._connected

    def has_orderbook(self, ticker: str) -> bool:
        """Check if we have an orderbook snapshot for the given market."""
        with self._lock:
            return ticker in self._orderbooks
