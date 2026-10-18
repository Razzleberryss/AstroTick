"""
index_feed.py — momentum from Kalshi's own settlement index (CF Benchmarks).

The GET /live_data/events/{event_ticker} endpoint serves the underlying index
an event settles on — for the crypto 15-minute series that is the CF
Benchmarks real-time index (BRTI for BTC, and the ETH/SOL equivalents) — as
1-minute candlesticks plus a 1-second timeseries, with the event's coin and
maturity timestamp. Computing momentum from this feed instead of an external
spot price means the signal tracks the exact index that decides the market,
with no third-party dependency, API keys, or rate limits.

The bot's spot feed (strategy.get_spot_momentum, yfinance) remains the
fallback when this endpoint is unavailable.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np

import config

log = logging.getLogger(__name__)

# Momentum normalization: a move of this fraction over the lookback window is
# full-scale (+/-1.0). Mirrors the spot-feed normalization in strategy.py.
MOMENTUM_FULL_SCALE_PCT = 0.02

# Per-event cache. The feed updates every second but the bot polls every
# ~10 s, so a short TTL avoids refetching within one decision cycle while
# keeping the signal effectively live.
_CACHE_TTL_SECONDS = 8.0
_CACHE_MAX_EVENTS = 32
_cache: dict[str, tuple[float, float]] = {}  # event_ticker -> (monotonic_ts, momentum)


def momentum_from_closes(closes, lookback_bars: int) -> Optional[float]:
    """
    Momentum in [-1, 1] from a chronological list of closes.

    Uses the same formula as the spot feed: percent change from the bar
    *lookback_bars* back to the latest bar, normalized so that a
    MOMENTUM_FULL_SCALE_PCT move saturates at +/-1. Returns None when there is
    not enough history or the baseline is unusable.
    """
    if closes is None or len(closes) < lookback_bars + 1:
        return None
    recent = float(closes[-1])
    baseline = float(closes[-(lookback_bars + 1)])
    if baseline == 0:
        return None
    pct_change = (recent - baseline) / baseline
    return float(np.clip(pct_change / MOMENTUM_FULL_SCALE_PCT, -1.0, 1.0))


def extract_minute_closes(live_data: dict) -> list[float]:
    """
    Pull the 1-minute close series out of a /live_data/events response.

    Accepts either the full response ({"live_data": {...}}) or the inner
    object. Returns [] when the shape is unrecognized.
    """
    if not isinstance(live_data, dict):
        return []
    inner = live_data.get("live_data", live_data)
    if not isinstance(inner, dict):
        return []
    details = inner.get("details") or {}
    candles = (details.get("candlesticks") or {}).get("1M") or []
    closes: list[float] = []
    for candle in candles:
        close = candle.get("close") if isinstance(candle, dict) else None
        if close is None:
            continue
        try:
            closes.append(float(close))
        except (TypeError, ValueError):
            continue
    return closes


def extract_coin(live_data: dict) -> Optional[str]:
    """Return the event's underlying coin symbol ('BTC', 'ETH', ...) if present."""
    if not isinstance(live_data, dict):
        return None
    inner = live_data.get("live_data", live_data)
    if not isinstance(inner, dict):
        return None
    coin = (inner.get("details") or {}).get("coin")
    return str(coin).upper() if coin else None


# Coins with crypto Up/Down series on Kalshi today; used to map a series
# ticker to the yfinance symbol for the spot FALLBACK feed (the primary feed
# needs no mapping — the live_data response names its own coin).
_KNOWN_COINS = ("BTC", "ETH", "SOL", "XRP", "DOGE", "LTC")


def spot_symbol_for_series(series_ticker: str) -> str:
    """
    Map a Kalshi crypto series ticker to the yfinance spot symbol used by the
    fallback feed (KXBTC15M -> BTC-USD, KXETH15M -> ETH-USD, ...). Unknown
    series fall back to BTC-USD with a warning.
    """
    core = (series_ticker or "").upper()
    if core.startswith("KX"):
        core = core[2:]
    for coin in _KNOWN_COINS:
        if core.startswith(coin):
            return f"{coin}-USD"
    log.warning(
        "No spot-symbol mapping for series %s; falling back to BTC-USD", series_ticker
    )
    return "BTC-USD"


def get_index_momentum(client, event_ticker: str) -> Optional[float]:
    """
    Momentum in [-1, 1] computed from the event's settlement-index 1-minute
    candles, or None when the feed is unavailable (the caller then falls back
    to the spot feed).

    *client* is a KalshiClient (provides get_event_live_data).
    """
    if not event_ticker:
        return None

    now = time.monotonic()
    cached = _cache.get(event_ticker)
    if cached is not None and (now - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    try:
        data = client.get_event_live_data(event_ticker)
    except Exception as exc:
        log.warning("Settlement-index feed unavailable for %s: %s", event_ticker, exc)
        return None

    closes = extract_minute_closes(data)
    momentum = momentum_from_closes(closes, config.MOMENTUM_LOOKBACK_BARS)
    if momentum is None:
        log.warning(
            "Settlement-index feed for %s returned too little history (%d bars)",
            event_ticker, len(closes),
        )
        return None

    _cache[event_ticker] = (now, momentum)
    # Events expire every 15 minutes; drop the oldest entries so the cache
    # does not grow across a long run.
    if len(_cache) > _CACHE_MAX_EVENTS:
        for stale_key, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[
            : len(_cache) - _CACHE_MAX_EVENTS
        ]:
            _cache.pop(stale_key, None)

    log.debug("Index momentum for %s: %.4f", event_ticker, momentum)
    return momentum
