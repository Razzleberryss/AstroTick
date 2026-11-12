"""Quadratic fees. Maker treatment follows the series ``fee_type``.

KXBTC15M is ``quadratic`` today, so makers pay 0 and takers pay
``ceil(multiplier * 0.07 * C * P * (1-P))``. The coefficients and the
rounding quantum are arguments: whether the account rounds to $0.0001
(direct member) is UNVERIFIED, and a future ``quadratic_with_maker_fees``
value must not keep assuming a zero maker fee.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

MAKER_FEE_TYPES = frozenset(
    {
        "quadratic_with_maker_fees",
        "quadratic_with_combo_maker_fees",
    }
)
# When the series payload could not be read, assume maker fees exist so a
# paper log does not pretend the maker rebate/zero is known.
UNKNOWN_FEE_TYPE = "quadratic_with_maker_fees"


def _ceil_to(amount: Decimal, quantum: Decimal) -> Decimal:
    if amount <= 0:
        return Decimal("0")
    if quantum <= 0:
        raise ValueError("quantum must be > 0")
    steps = (amount / quantum).to_integral_value(rounding=ROUND_CEILING)
    return steps * quantum


@dataclass(frozen=True)
class FeeSchedule:
    fee_type: str
    fee_multiplier: Decimal = Decimal("1")
    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    model_quantum: Decimal = Decimal("0.000001")
    balance_quantum: Decimal = Decimal("0.0001")
    flat_taker_dollars: Decimal = Decimal("0")
    flat_maker_dollars: Decimal = Decimal("0")

    @property
    def maker_fee_applies(self) -> bool:
        return self.fee_type in MAKER_FEE_TYPES or self.fee_type == "flat"

    def taker_fee(self, contracts: Decimal, price: Decimal) -> Decimal:
        return self._quadratic(contracts, price, self.taker_rate, self.flat_taker_dollars)

    def maker_fee(self, contracts: Decimal, price: Decimal) -> Decimal:
        if self.fee_type == "quadratic":
            return Decimal("0")
        if self.fee_type == "flat":
            return _ceil_to(self.flat_maker_dollars * contracts, self.balance_quantum)
        if self.fee_type in MAKER_FEE_TYPES:
            return self._quadratic(contracts, price, self.maker_rate, Decimal("0"))
        # Unknown fee_type: charge the maker schedule rather than assume zero.
        return self._quadratic(contracts, price, self.maker_rate, Decimal("0"))

    def _quadratic(
        self,
        contracts: Decimal,
        price: Decimal,
        rate: Decimal,
        flat: Decimal,
    ) -> Decimal:
        if contracts < 0 or price < 0 or price > 1:
            raise ValueError("contracts must be >= 0 and price must be in [0, 1]")
        raw = self.fee_multiplier * rate * contracts * price * (Decimal("1") - price)
        raw += flat
        modeled = _ceil_to(raw, self.model_quantum)
        return _ceil_to(modeled, self.balance_quantum)


def schedule_from_api(
    fee_type: str | None,
    fee_multiplier: str | float | Decimal | None = None,
    *,
    unknown_fee_type: str = UNKNOWN_FEE_TYPE,
    taker_rate: Decimal = Decimal("0.07"),
    maker_rate: Decimal = Decimal("0.0175"),
    model_quantum: Decimal = Decimal("0.000001"),
    balance_quantum: Decimal = Decimal("0.0001"),
) -> FeeSchedule:
    """Build a schedule from a series payload. Missing ``fee_type`` is not quadratic."""
    chosen = (fee_type or "").strip().lower() or unknown_fee_type
    mult = Decimal(str(fee_multiplier if fee_multiplier not in (None, "") else "1"))
    return FeeSchedule(
        fee_type=chosen,
        fee_multiplier=mult,
        taker_rate=taker_rate,
        maker_rate=maker_rate,
        model_quantum=model_quantum,
        balance_quantum=balance_quantum,
    )
