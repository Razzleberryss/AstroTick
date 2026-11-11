"""Driftless fair value for a 60-second settlement average.

YES resolves when the simple average of the settlement samples is >= the
strike (ties at the rounded average go YES). For tau >= 60s no sample is
locked yet, and the variance of that average under driftless Brownian
motion sampled once per second is sigma^2 * (tau - 39.5) to a very close
approximation. The implementation uses the exact pairwise sum

    Var(A) = (S * sigma * scale)^2 * Σ_{i,j} min(d_i, d_j) / N^2

which is the discrete form behind Phi((S-K) / (sigma * sqrt(tau-40))).

Inside the final minute, samples with delay <= 0 are locked at the prices
the caller observed. Missing locked prices return None rather than guessing.
The settlement window (close included, start excluded, 60 one-second
samples) matches Kalshi's websocket field and is UNVERIFIED as the official
settlement sampler, so ``sample_count`` and ``close_included`` stay arguments.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


def normal_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def sum_pairwise_min(delays: list[float]) -> float:
    """Σ_{i,j} min(d_i, d_j) for nonnegative delays."""
    ordered = sorted(float(d) for d in delays)
    n = len(ordered)
    total = 0.0
    for index, value in enumerate(ordered):
        weight = 2 * (n - index) - 1
        total += value * weight
    return total


def sample_delays(
    tau_seconds: float,
    *,
    sample_count: int = 60,
    close_included: bool = True,
) -> list[float]:
    """Seconds from ``now`` until each settlement sample.

    With ``close_included`` the samples sit at close-59s, ..., close
    (the documented WS window (close-60s, close]). A non-positive delay
    means that sample is already locked.
    """
    if sample_count < 1:
        raise ValueError("sample_count must be >= 1")
    if close_included:
        offsets = range(sample_count - 1, -1, -1)  # 59 .. 0
    else:
        offsets = range(sample_count, 0, -1)  # 60 .. 1
    return [float(tau_seconds) - float(off) for off in offsets]


@dataclass(frozen=True)
class FairValue:
    probability_yes: float
    expected_average: float
    std_average: float
    locked_count: int
    future_count: int
    variance_seconds: float
    spot: float
    strike: float
    sigma_per_sqrt_second: float

    def as_log_dict(self) -> dict:
        return {
            "probability_yes": self.probability_yes,
            "expected_average": self.expected_average,
            "std_average": self.std_average,
            "locked_count": self.locked_count,
            "future_count": self.future_count,
            "variance_seconds": self.variance_seconds,
            "spot": self.spot,
            "strike": self.strike,
            "sigma_per_sqrt_second": self.sigma_per_sqrt_second,
        }


def fair_value_yes(
    *,
    spot: float,
    strike: float,
    tau_seconds: float,
    sigma_per_sqrt_second: float,
    locked_samples: list[float] | None = None,
    sample_count: int = 60,
    close_included: bool = True,
    vol_scale: float = 1.0,
    vol_floor: float = 0.0,
) -> FairValue | None:
    """P(average >= strike). None when the inputs cannot support a price.

    ``sigma_per_sqrt_second`` is the log-volatility per sqrt(second) of the
    index. Dollar volatility is ``spot * sigma * vol_scale``. ``vol_scale``
    and ``vol_floor`` default to 1 and 0; the fade study's fitted 0.96 and
    0.64bp floor are not baked in.
    """
    if sample_count < 1 or spot <= 0 or strike < 0:
        return None
    if vol_scale < 0 or vol_floor < 0:
        return None
    delays = sample_delays(
        tau_seconds, sample_count=sample_count, close_included=close_included
    )
    future = [d for d in delays if d > 0]
    locked_needed = sample_count - len(future)
    observed = list(locked_samples or [])
    if len(observed) != locked_needed:
        return None
    if any(price <= 0 for price in observed):
        return None

    sigma = max(float(sigma_per_sqrt_second), float(vol_floor)) * float(vol_scale)
    expected = (sum(observed) + float(spot) * len(future)) / sample_count

    if not future:
        # Fully locked. Ties (average == strike, including exact equality
        # before the exchange's 2dp rounding) resolve YES.
        probability = 1.0 if expected >= float(strike) else 0.0
        return FairValue(
            probability_yes=probability,
            expected_average=expected,
            std_average=0.0,
            locked_count=locked_needed,
            future_count=0,
            variance_seconds=0.0,
            spot=float(spot),
            strike=float(strike),
            sigma_per_sqrt_second=sigma,
        )

    if sigma <= 0:
        # A zero forecast of future movement would collapse the contract to
        # 0 or 1. Refuse instead of trading that.
        return None

    pair_sum = sum_pairwise_min(future)
    variance_seconds = pair_sum / float(sample_count) ** 2
    std = float(spot) * sigma * math.sqrt(variance_seconds)
    if std <= 0:
        return None
    probability = normal_cdf((expected - float(strike)) / std)
    probability = min(1.0, max(0.0, probability))
    return FairValue(
        probability_yes=probability,
        expected_average=expected,
        std_average=std,
        locked_count=locked_needed,
        future_count=len(future),
        variance_seconds=variance_seconds,
        spot=float(spot),
        strike=float(strike),
        sigma_per_sqrt_second=sigma,
    )
