"""Causal feature lookups. Every read uses timestamps <= t.

Sigma's batch form is checked against ``engine.vol.realized_sigma`` in
tests. Fair value itself is always ``engine.fair_value.fair_value_yes``.
"""

from __future__ import annotations

import math

import numpy as np

from engine.vol import blend_sigma, realized_sigma


def asof_index(timestamps: np.ndarray, t: float) -> int:
    """Index of the last sample with timestamp <= t, or -1."""
    if len(timestamps) == 0:
        return -1
    return int(np.searchsorted(timestamps, t, side="right") - 1)


def asof_price(
    timestamps: np.ndarray,
    prices: np.ndarray,
    t: float,
    *,
    tolerance: float = 1.0,
) -> float | None:
    """Price of the last print at or before t, if it is not older than tolerance."""
    idx = asof_index(timestamps, t)
    if idx < 0:
        return None
    if t - float(timestamps[idx]) > tolerance:
        return None
    value = float(prices[idx])
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def vectorized_sigma(prices: np.ndarray, step_seconds: float) -> np.ndarray:
    """Trailing realized sigma, one value per index, using only prices[:i+1].

    ``out[i]`` matches ``realized_sigma(prices[i-n+1:i+1], step)`` once the
    window holds at least three prices. Earlier entries are NaN. The window
    is the whole prefix here; callers slice to the lookback first.
    """
    n = len(prices)
    out = np.full(n, np.nan)
    if n < 3 or step_seconds <= 0:
        return out
    log_px = np.log(prices.astype(float))
    returns = np.diff(log_px)
    # Sample variance of returns[i-w+1:i+1] via prefix sums.
    c1 = np.concatenate([[0.0], np.cumsum(returns)])
    c2 = np.concatenate([[0.0], np.cumsum(returns * returns)])
    for i in range(2, n):
        # returns that end at price i use returns[0:i] (i returns) when the
        # window is the whole prefix. Match realized_sigma(prices[:i+1]).
        count = i  # number of returns in prices[:i+1]
        if count < 2:
            continue
        sum_r = c1[i] - c1[0]
        sum_r2 = c2[i] - c2[0]
        var = (sum_r2 - (sum_r * sum_r) / count) / (count - 1)
        if var < 0:
            var = 0.0
        out[i] = math.sqrt(var / step_seconds)
    return out


def trailing_sigma(prices: np.ndarray, step_seconds: float, window: int) -> np.ndarray:
    """``out[i]`` matches ``realized_sigma(prices[i-window:i+1], step_seconds)``.

    ``window`` is the number of returns (so the slice has ``window + 1``
    prices). Entries before the window is full are NaN. Only prices at
    indexes ``<= i`` are used.
    """
    n = len(prices)
    out = np.full(n, np.nan)
    if window < 2 or n < window + 1 or step_seconds <= 0:
        return out
    log_px = np.log(np.asarray(prices, dtype=float))
    returns = np.diff(log_px)
    c1 = np.concatenate([[0.0], np.cumsum(returns)])
    c2 = np.concatenate([[0.0], np.cumsum(returns * returns)])
    count = window
    for i in range(window, n):
        sum_r = c1[i] - c1[i - window]
        sum_r2 = c2[i] - c2[i - window]
        var = (sum_r2 - (sum_r * sum_r) / count) / (count - 1)
        if var < 0:
            var = 0.0
        out[i] = math.sqrt(var / step_seconds)
    return out


def sigma_matching_engine(prices: list[float], step_seconds: float) -> float | None:
    """The live estimator. Backtest call sites that need one window use this."""
    return realized_sigma(prices, step_seconds)


def blended(short: float | None, long: float | None, short_w: float = 0.7, long_w: float = 0.3) -> float | None:
    return blend_sigma(short, long, short_weight=short_w, long_weight=long_w)
