"""Short-horizon realized volatility from index prints.

Sigma is the square root of the per-second variance of log returns. The
15-minute / 60-minute blend weights are the fade study's defaults and are
arguments, not a Kalshi constant. One-second bounce is avoided by letting
the caller pass the sample spacing (10s in the study, 60s when only
minute candles are available).
"""

from __future__ import annotations

import math


def realized_sigma(
    prices: list[float],
    step_seconds: float,
) -> float | None:
    """Log-vol per sqrt(second). None when the sample is too short or non-positive."""
    if step_seconds <= 0 or len(prices) < 3:
        return None
    returns: list[float] = []
    for previous, current in zip(prices, prices[1:], strict=False):
        if previous <= 0 or current <= 0:
            return None
        returns.append(math.log(current / previous))
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    var = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    per_second = var / float(step_seconds)
    if per_second < 0:
        return None
    return math.sqrt(per_second)


def blend_sigma(
    short: float | None,
    long: float | None,
    *,
    short_weight: float = 0.7,
    long_weight: float = 0.3,
) -> float | None:
    if short is None and long is None:
        return None
    if short is None:
        return long
    if long is None:
        return short
    weight = short_weight + long_weight
    if weight <= 0:
        return None
    return (short_weight * short + long_weight * long) / weight
