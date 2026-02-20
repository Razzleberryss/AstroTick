"""
strategy.py  -  Signal generation for the Kalshi 15-minute BTC bot.

Strategy: Momentum + Orderbook Skew
  1. Pull recent BTC spot price history (yfinance, 1-min bars).
  2. Calculate short-term momentum (% change over last N bars).
  3. Pull Kalshi orderbook to measure YES/NO liquidity skew.
  4. Combine signals to produce:
       - side: 'yes' | 'no' | None (no trade)
       - confidence: 0.0 – 1.0
       - target_price_cents: limit price to use

This is intentionally simple and rule-based — a solid foundation
you can expand with ML, cross-market arb, etc. later.
"""

import logging
from dataclasses import dataclass
from typing import Optional

import numpy as np
import yfinance as yf

import config

log = logging.getLogger(__name__)


@dataclass
class Signal:
    side: str           # 'yes' or 'no'
    confidence: float   # 0.0 to 1.0
    price_cents: int    # suggested limit price
    reason: str         # human-readable explanation


def get_btc_momentum() -> Optional[float]:
    """
    Fetch recent 1-minute BTC/USD bars and return the momentum score.
    Returns a float in roughly [-1, 1]:
      > 0  => bullish (BTC trending up)
      < 0  => bearish (BTC trending down)
    Returns None on data error.
    """
    try:
        ticker = yf.Ticker(config.BTC_TICKER)
        # Grab last 30 minutes of 1-min bars
        hist = ticker.history(period="1d", interval="1m")
        if hist.empty or len(hist) < config.MOMENTUM_LOOKBACK_BARS + 1:
            log.warning("Not enough BTC price history available")
            return None

        closes = hist["Close"].values
        recent = closes[-config.MOMENTUM_LOOKBACK_BARS:]
        baseline = closes[-(config.MOMENTUM_LOOKBACK_BARS + 1)]

        if baseline == 0:
            return None

        pct_change = (recent[-1] - baseline) / baseline  # e.g. 0.003 = +0.3%
        # Normalize: clip to [-2%, +2%] range then scale to [-1, 1]
        momentum = float(np.clip(pct_change / 0.02, -1.0, 1.0))
        log.debug("BTC momentum: %.4f (raw pct_change=%.4f%%)", momentum, pct_change * 100)
        return momentum
    except Exception as exc:
        log.error("Error fetching BTC price: %s", exc)
        return None


def get_orderbook_skew(orderbook: dict) -> float:
    """
    Compute YES orderbook skew from Kalshi orderbook data.
    Returns a float in [-1, 1]:
      > 0  => more YES bids (market leans YES)
      < 0  => more NO bids  (market leans NO)
    """
    try:
        yes_bids = orderbook.get("orderbook", {}).get("yes", [])
        no_bids = orderbook.get("orderbook", {}).get("no", [])

        # Each entry is [price_cents, size]
        yes_liquidity = sum(p * s for p, s in yes_bids) if yes_bids else 0
        no_liquidity = sum(p * s for p, s in no_bids) if no_bids else 0
        total = yes_liquidity + no_liquidity

        if total == 0:
            return 0.0

        skew = (yes_liquidity - no_liquidity) / total  # -1 to +1
        log.debug("Orderbook skew: %.3f (YES=%d, NO=%d)", skew, yes_liquidity, no_liquidity)
        return skew
    except Exception as exc:
        log.error("Error computing orderbook skew: %s", exc)
        return 0.0


def suggest_limit_price(market: dict, side: str) -> int:
    """
    Pick a conservative limit price to ensure fills without crossing the spread.
    Returns a price in cents (1-99).
    """
    if side == "yes":
        # Pay up to the current yes_ask but no more than mid + 2c
        ask = market.get("yes_ask", 50)
        bid = market.get("yes_bid", max(1, ask - 4))
        price = min(ask, bid + 2)  # slightly above best bid
    else:
        ask = market.get("no_ask", 50)
        bid = market.get("no_bid", max(1, ask - 4))
        price = min(ask, bid + 2)

    return max(config.MIN_CONTRACT_PRICE_CENTS, min(config.MAX_CONTRACT_PRICE_CENTS, price))


def generate_signal(market: dict, orderbook: dict) -> Optional[Signal]:
    """
    Main entry point.  Returns a Signal or None if no trade warranted.

    Combines:
      - BTC short-term momentum  (weight 0.6)
      - Kalshi orderbook skew    (weight 0.4)
    """
    momentum = get_btc_momentum()
    if momentum is None:
        log.warning("Could not compute momentum — skipping this cycle")
        return None

    skew = get_orderbook_skew(orderbook)

    # Weighted composite score  (-1 = strong NO, +1 = strong YES)
    composite = (0.6 * momentum) + (0.4 * skew)
    confidence = abs(composite)  # 0.0 to 1.0

    log.info(
        "Signal composite=%.3f | momentum=%.3f | skew=%.3f | confidence=%.3f",
        composite, momentum, skew, confidence,
    )

    # Only trade if we have sufficient edge
    if confidence < config.MIN_EDGE_THRESHOLD:
        log.info("Confidence %.3f below threshold %.3f — no trade", confidence, config.MIN_EDGE_THRESHOLD)
        return None

    side = "yes" if composite > 0 else "no"
    price = suggest_limit_price(market, side)

    reason = (
        f"momentum={momentum:+.3f} skew={skew:+.3f} → {side.upper()} "
        f"@ {price}c (confidence={confidence:.2%})"
    )

    return Signal(side=side, confidence=confidence, price_cents=price, reason=reason)
