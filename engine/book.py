"""Bids-only Kalshi books. No invented asks.

YES ask = 1 - best NO bid, and the reverse, but only when that bid exists.
A one-sided book stays one-sided. The legacy ``ask = bid + 1`` inference is
not used.
"""

from __future__ import annotations

from decimal import Decimal


def _levels(raw) -> list[tuple[Decimal, Decimal]]:
    levels: list[tuple[Decimal, Decimal]] = []
    if not raw:
        return levels
    for row in raw:
        if isinstance(row, dict):
            price = row.get("price_dollars", row.get("price"))
            count = row.get("count_fp", row.get("count", row.get("size")))
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price, count = row[0], row[1]
        else:
            continue
        try:
            price_d = Decimal(str(price))
            count_d = Decimal(str(count))
        except Exception:
            continue
        # Integer cent quotes (1-99) show up on older fixtures.
        if price_d > 1:
            price_d = price_d / Decimal("100")
        if count_d <= 0 or price_d <= 0 or price_d >= 1:
            continue
        levels.append((price_d, count_d))
    return levels


def best_bid(levels: list[tuple[Decimal, Decimal]]) -> Decimal | None:
    if not levels:
        return None
    return max(price for price, _count in levels)


class Book:
    def __init__(
        self,
        yes_bid: Decimal | None,
        no_bid: Decimal | None,
        *,
        yes_bid_size: Decimal | None = None,
        no_bid_size: Decimal | None = None,
    ):
        self.yes_bid = yes_bid
        self.no_bid = no_bid
        self.yes_bid_size = yes_bid_size
        self.no_bid_size = no_bid_size

    @property
    def yes_ask(self) -> Decimal | None:
        if self.no_bid is None:
            return None
        return Decimal("1") - self.no_bid

    @property
    def no_ask(self) -> Decimal | None:
        if self.yes_bid is None:
            return None
        return Decimal("1") - self.yes_bid

    @property
    def crossed(self) -> bool:
        if self.yes_bid is None or self.yes_ask is None:
            return False
        return self.yes_bid >= self.yes_ask

    def as_log_dict(self) -> dict:
        def show(value: Decimal | None) -> str | None:
            return None if value is None else format(value, "f")

        return {
            "yes_bid": show(self.yes_bid),
            "yes_ask": show(self.yes_ask),
            "no_bid": show(self.no_bid),
            "no_ask": show(self.no_ask),
            "crossed": self.crossed,
        }


def parse_orderbook(payload: dict | None) -> Book:
    if not isinstance(payload, dict):
        return Book(None, None)
    book = payload.get("orderbook_fp") or payload.get("orderbook") or payload
    yes_raw = (
        book.get("yes_dollars")
        or book.get("yes")
        or payload.get("yes_dollars")
        or []
    )
    no_raw = (
        book.get("no_dollars")
        or book.get("no")
        or payload.get("no_dollars")
        or []
    )
    yes_levels = _levels(yes_raw)
    no_levels = _levels(no_raw)
    yes_bid = best_bid(yes_levels)
    no_bid = best_bid(no_levels)
    yes_size = None
    no_size = None
    if yes_bid is not None:
        yes_size = sum(count for price, count in yes_levels if price == yes_bid)
    if no_bid is not None:
        no_size = sum(count for price, count in no_levels if price == no_bid)
    return Book(yes_bid, no_bid, yes_bid_size=yes_size, no_bid_size=no_size)
