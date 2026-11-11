"""Websocket clock handling.

Kalshi's orderbook channel carries a ``clock`` that is comparable across
connections. A backwards step means the session clock reset; deltas after
that are not a continuation of the previous book, so the cache must be
dropped and a fresh snapshot fetched. A disconnect is a separate event:
the runner cancels resting orders rather than quoting off a dead socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field


class ClockWatch:
    def __init__(self) -> None:
        self.last: int | None = None

    def observe(self, clock: int) -> str:
        """Return ``ok`` or ``reset`` (clock moved backwards)."""
        value = int(clock)
        if self.last is not None and value < self.last:
            self.last = value
            return "reset"
        self.last = value
        return "ok"


@dataclass
class BookCache:
    """Holds the latest book and forces a resnapshot after a clock reset."""

    watch: ClockWatch = field(default_factory=ClockWatch)
    books: dict = field(default_factory=dict)
    needs_snapshot: bool = False
    connected: bool = False

    def on_disconnect(self) -> None:
        self.connected = False
        self.books.clear()
        self.needs_snapshot = True

    def on_message(self, message: dict) -> str:
        """Apply one websocket payload.

        Returns ``reset`` when the clock went backwards (cache cleared),
        ``snapshot`` when a snapshot was stored, ``delta`` when a delta was
        stored, or ``ignored`` when a delta arrived before the required
        snapshot.
        """
        self.connected = True
        clock = message.get("clock")
        if clock is not None and self.watch.observe(int(clock)) == "reset":
            self.books.clear()
            self.needs_snapshot = True
            # The message that revealed the reset is not applied.
            return "reset"
        kind = message.get("type") or message.get("msg_type")
        ticker = message.get("market_ticker") or message.get("ticker")
        if self.needs_snapshot and kind != "orderbook_snapshot":
            return "ignored"
        if kind == "orderbook_snapshot" and ticker:
            self.books[ticker] = message
            self.needs_snapshot = False
            return "snapshot"
        if kind == "orderbook_delta" and ticker and ticker in self.books:
            self.books[ticker] = message
            return "delta"
        if self.needs_snapshot:
            return "ignored"
        return "ignored"
