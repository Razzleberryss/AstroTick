"""
kalshi_money.py — Fixed-point dollar strings and API v2 *_dollars field helpers.

Kalshi represents many currency fields as decimal strings (e.g. "0.5500").
Use these helpers instead of deprecated integer / *_fixed fields.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Optional


def parse_dollars_to_decimal(value: Any) -> Optional[Decimal]:
    """Parse API dollar string or numeric into Decimal, or None if missing/invalid."""
    if value is None or value == "":
        return None
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    try:
        return Decimal(str(value).strip())
    except (InvalidOperation, ValueError, TypeError):
        return None


def parse_dollars_to_cents_int(value: Any) -> Optional[int]:
    """Convert a dollar string like '0.55' to whole cents (55)."""
    d = parse_dollars_to_decimal(value)
    if d is None:
        return None
    cents = (d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return int(cents)


def position_count(pos: dict) -> float:
    """
    Signed contract count for a market_positions row.

    The current API reports the fixed-point string ``position_fp`` ("3.00",
    possibly fractional and negative for NO exposure); older payloads used the
    signed integer ``position``. Positive = long YES, negative = long NO.
    """
    fp = pos.get("position_fp")
    if fp is not None:
        try:
            return float(str(fp))
        except (TypeError, ValueError):
            pass
    try:
        return float(pos.get("position", 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def position_exposure_dollars(pos: dict) -> Optional[float]:
    """Current market exposure in dollars for a market_positions row, if reported."""
    d = parse_dollars_to_decimal(pos.get("market_exposure_dollars"))
    if d is not None:
        return float(d)
    exposure_cents = pos.get("market_exposure")
    if exposure_cents is not None:
        try:
            return int(exposure_cents) / 100.0
        except (TypeError, ValueError):
            pass
    return None


def position_average_price_cents(pos: dict, default_cents: int = 99) -> int:
    """
    Average entry price in cents for a market_positions row.

    Prefer explicit *_dollars average-price fields (older payloads); the
    current API no longer sends one, so estimate from total_traded_dollars
    over the contract count before falling back to the conservative default.
    """
    for key in ("average_price_dollars", "avg_price_dollars", "avg_entry_price_dollars"):
        c = parse_dollars_to_cents_int(pos.get(key))
        if c is not None:
            return c
    ap = pos.get("average_price")
    if ap is not None:
        try:
            return int(ap)
        except (TypeError, ValueError):
            pass
    traded = parse_dollars_to_decimal(pos.get("total_traded_dollars"))
    count = abs(position_count(pos))
    if traded is not None and count > 0:
        cents = (traded / Decimal(str(count)) * 100).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        if 0 < int(cents) <= 100:
            return int(cents)
    return default_cents


def enrich_market_quotes_from_dollar_fields(market: dict) -> dict:
    """
    Populate legacy cent keys (yes_bid, yes_ask, …, last_price) from *_dollars
    fields when the API no longer sends integer quote columns on market objects.

    Mutates and returns *market* for chaining.
    """
    mapping = (
        ("yes_bid_dollars", "yes_bid"),
        ("yes_ask_dollars", "yes_ask"),
        ("no_bid_dollars", "no_bid"),
        ("no_ask_dollars", "no_ask"),
    )
    for d_key, c_key in mapping:
        cents = parse_dollars_to_cents_int(market.get(d_key))
        if cents is not None:
            market[c_key] = cents

    lp = parse_dollars_to_cents_int(market.get("last_price_dollars"))
    if lp is not None:
        market["last_price"] = lp

    yp = parse_dollars_to_cents_int(market.get("yes_price_dollars"))
    if yp is not None:
        market["yes_price"] = yp
    np_ = parse_dollars_to_cents_int(market.get("no_price_dollars"))
    if np_ is not None:
        market["no_price"] = np_

    # Mirror into best_* names used by the bot/strategy when only REST market payload exists.
    if market.get("yes_bid") is not None:
        market.setdefault("best_yes_bid", market.get("yes_bid"))
    if market.get("yes_ask") is not None:
        market.setdefault("best_yes_ask", market.get("yes_ask"))
    if market.get("no_bid") is not None:
        market.setdefault("best_no_bid", market.get("no_bid"))
    if market.get("no_ask") is not None:
        market.setdefault("best_no_ask", market.get("no_ask"))

    return market


def fmt_cents(value) -> str:
    """Format a cent-valued integer for logging; returns 'NA' when the value is None."""
    if value is None:
        return "NA"
    try:
        return f"{int(round(value))}c"
    except (TypeError, ValueError):
        return "NA"
