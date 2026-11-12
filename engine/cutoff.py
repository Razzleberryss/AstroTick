"""Hard stop on order submission near a market's close.

Owner rule: never trade in the last 45 seconds of a window. This module
implements that as a block on every create, amend, and size decrease once
the time remaining is at or below ``cutoff_seconds`` (floor 50) plus a
clock-skew margin. Cancels stay allowed so resting orders can be pulled.
Positions are held to settlement; nothing here flattens them.

The guard is process-wide so ``KalshiClient`` can enforce it at the SDK
submit path. A strategy that does not know the close time cannot submit:
missing close times fail closed.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime

log = logging.getLogger(__name__)

# The owner's rule is "no trades in the last 45 seconds". The operating
# point is T-50s and cannot be configured lower.
CUTOFF_FLOOR_SECONDS = 50.0

_LOCK = threading.Lock()
_CLOSES: dict[str, datetime] = {}


class CutoffBlocked(Exception):
    """Raised when an order mutation must not be sent."""


@dataclass(frozen=True)
class CutoffPolicy:
    cutoff_seconds: float = CUTOFF_FLOOR_SECONDS
    clock_skew_margin_seconds: float = 2.0

    def __post_init__(self) -> None:
        if self.cutoff_seconds < CUTOFF_FLOOR_SECONDS:
            raise ValueError(
                f"cutoff_seconds must be >= {CUTOFF_FLOOR_SECONDS} "
                "(owner rule: no trades in the last 45s, enforced from T-50s)"
            )
        if self.clock_skew_margin_seconds < 0:
            raise ValueError("clock_skew_margin_seconds must be >= 0")

    @property
    def block_when_remaining_at_most(self) -> float:
        """Orders are blocked when seconds-to-close is at or below this."""
        return self.cutoff_seconds + self.clock_skew_margin_seconds


def load_policy() -> CutoffPolicy:
    """Read the policy from the environment, refusing to weaken the floor."""
    raw_cutoff = os.getenv("CUTOFF_SECONDS", str(CUTOFF_FLOOR_SECONDS))
    raw_margin = os.getenv("CUTOFF_CLOCK_SKEW_SECONDS", "2")
    try:
        cutoff = float(raw_cutoff)
    except (TypeError, ValueError):
        cutoff = CUTOFF_FLOOR_SECONDS
    try:
        margin = float(raw_margin)
    except (TypeError, ValueError):
        margin = 2.0
    if cutoff < CUTOFF_FLOOR_SECONDS:
        log.warning(
            "CUTOFF_SECONDS=%s is below the 50s floor; using 50", raw_cutoff
        )
        cutoff = CUTOFF_FLOOR_SECONDS
    if margin < 0:
        log.warning("CUTOFF_CLOCK_SKEW_SECONDS=%s is negative; using 0", raw_margin)
        margin = 0.0
    return CutoffPolicy(cutoff_seconds=cutoff, clock_skew_margin_seconds=margin)


def parse_time(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        dt = value
    else:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def seconds_to_close(close: datetime | str, now: datetime) -> float:
    return (parse_time(close) - parse_time(now)).total_seconds()


def trading_blocked(
    close: datetime | str,
    now: datetime,
    policy: CutoffPolicy | None = None,
) -> bool:
    """True when new orders and amends for this close must be refused."""
    policy = policy or load_policy()
    return seconds_to_close(close, now) <= policy.block_when_remaining_at_most


def register_close(ticker: str, close: datetime | str) -> None:
    with _LOCK:
        _CLOSES[ticker] = parse_time(close)


def registered_close(ticker: str) -> datetime | None:
    with _LOCK:
        return _CLOSES.get(ticker)


def reset_registry() -> None:
    """Test helper. Production code does not need to clear the registry."""
    with _LOCK:
        _CLOSES.clear()


def assert_submission_allowed(
    ticker: str,
    kind: str,
    *,
    close_time: datetime | str | None = None,
    now: datetime | None = None,
    policy: CutoffPolicy | None = None,
) -> None:
    """Refuse creates, amends, and decreases inside the blocked window.

    ``kind="cancel"`` always returns. Every other kind requires a close
    time, either passed in or previously registered for ``ticker``.
    """
    if kind == "cancel":
        return
    if kind not in ("create", "amend", "decrease"):
        raise CutoffBlocked(f"unknown order mutation {kind!r} for {ticker}")
    if close_time is not None:
        register_close(ticker, close_time)
    close = registered_close(ticker)
    if close is None:
        raise CutoffBlocked(
            f"refusing {kind} on {ticker}: no close time registered "
            "(fail closed so a strategy cannot skip the T-50 cutoff)"
        )
    policy = policy or load_policy()
    current = parse_time(now) if now is not None else datetime.now(UTC)
    remaining = seconds_to_close(close, current)
    if remaining <= policy.block_when_remaining_at_most:
        raise CutoffBlocked(
            f"refusing {kind} on {ticker}: {remaining:.3f}s to close is inside "
            f"the T-{policy.cutoff_seconds:.0f}s cutoff "
            f"(clock-skew margin {policy.clock_skew_margin_seconds:.3f}s). "
            "Resting orders must be cancelled; the position is held to settlement."
        )
