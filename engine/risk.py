"""Client-side hard caps.

The exchange order-group cancel is a secondary breaker and can lag several
seconds, so these checks are what actually stop a new order. Caps are on
contracts per market and on contracts and dollars across every open window
the bot can see (overlapping 15-minute markets included).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskLimits:
    max_contracts_per_market: Decimal = Decimal("1")
    max_contracts_total: Decimal = Decimal("2")
    max_dollars_total: Decimal = Decimal("25")
    max_daily_loss_dollars: Decimal = Decimal("10")
    stale_feed_seconds: float = 15.0

    def __post_init__(self) -> None:
        if self.max_contracts_per_market <= 0:
            raise ValueError("max_contracts_per_market must be > 0")
        if self.max_contracts_total < self.max_contracts_per_market:
            raise ValueError("max_contracts_total must be >= max_contracts_per_market")
        if self.max_dollars_total <= 0:
            raise ValueError("max_dollars_total must be > 0")
        if self.max_daily_loss_dollars < 0:
            raise ValueError("max_daily_loss_dollars must be >= 0")
        if self.stale_feed_seconds <= 0:
            raise ValueError("stale_feed_seconds must be > 0")


@dataclass(frozen=True)
class Exposure:
    """Contracts already working, counting positions and resting orders."""

    contracts_on_market: Decimal
    contracts_total: Decimal
    dollars_total: Decimal
    daily_realized_pnl: Decimal
    feed_age_seconds: float | None


class RiskBook:
    def __init__(self, limits: RiskLimits):
        self.limits = limits

    def check_order(
        self,
        exposure: Exposure,
        *,
        add_contracts: Decimal,
        add_dollars: Decimal,
    ) -> tuple[bool, str]:
        if add_contracts <= 0:
            return False, "size_not_positive"
        if exposure.daily_realized_pnl <= -self.limits.max_daily_loss_dollars:
            return False, "daily_loss_kill"
        if (
            exposure.feed_age_seconds is None
            or exposure.feed_age_seconds > self.limits.stale_feed_seconds
        ):
            return False, "stale_feed"
        market_after = exposure.contracts_on_market + add_contracts
        if market_after > self.limits.max_contracts_per_market:
            return False, "max_contracts_per_market"
        total_after = exposure.contracts_total + add_contracts
        if total_after > self.limits.max_contracts_total:
            return False, "max_contracts_total"
        dollars_after = exposure.dollars_total + add_dollars
        if dollars_after > self.limits.max_dollars_total:
            return False, "max_dollars_total"
        return True, "ok"
