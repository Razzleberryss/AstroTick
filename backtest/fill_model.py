"""Fill rules used by the simulated exchange.

Conservative is the default. It does not credit queue advancement: a maker
fill happens only when a print trades strictly through our price. See
``FILL_MODEL.md``. Fees go through ``engine.fees.FeeSchedule`` so the
backtest cannot drift from the live fee code.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from engine.fees import FeeSchedule

# Provisional, from 1,699 snapshot pairs on 22 markets (2026-11-18 note).
DEFAULT_HAIRCUT = Decimal("0.03")
HAIRCUT_SENSITIVITY = (
    Decimal("0"),
    Decimal("0.03"),
    Decimal("0.04"),
    Decimal("0.05"),
    Decimal("0.06"),
    Decimal("0.07"),
)


@dataclass(frozen=True)
class FillModel:
    """How the simulator turns prints and quotes into fills.

    ``name="conservative"`` fills makers only on a strict trade-through and
    subtracts ``haircut_per_contract`` (default 3c) from maker settlement PnL.
    ``name="optimistic"`` also fills when a print is exactly at our price and
    applies no haircut. Neither mode advances a queue at a fixed level.
    """

    name: str = "conservative"
    haircut_per_contract: Decimal = DEFAULT_HAIRCUT
    maker_fee_type: str = "quadratic"
    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    # When the book does not say how many contracts are visible, a taker
    # cannot assume the whole order fills. Cap is 1 contract.
    unknown_visible_cap: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if self.name not in ("conservative", "optimistic"):
            raise ValueError("fill model must be conservative or optimistic")
        if self.haircut_per_contract < 0:
            raise ValueError("haircut_per_contract must be >= 0")

    @classmethod
    def from_name(cls, name: str, *, haircut: Decimal | None = None) -> FillModel:
        if name == "optimistic":
            return cls(name="optimistic", haircut_per_contract=Decimal("0") if haircut is None else haircut)
        if name == "conservative":
            return cls(
                name="conservative",
                haircut_per_contract=DEFAULT_HAIRCUT if haircut is None else haircut,
            )
        raise ValueError(f"unknown fill model {name!r}")

    def schedule(self) -> FeeSchedule:
        return FeeSchedule(
            fee_type=self.maker_fee_type,
            taker_rate=self.taker_rate,
            maker_rate=self.maker_rate,
        )

    def maker_fills_on(self, contract_side: str, limit: Decimal, yes_trade: Decimal) -> bool:
        """True when this print reaches a resting post-only bid.

        YES bid at P is through when the YES print is strictly below P.
        NO bid at P is an offer to sell YES at ``1-P``; it is through when
        the YES print is strictly above ``1-P``. Optimistic mode also
        accepts a print exactly at that price. A print at the price does
        not mean we were filled: the queue is assumed to sit ahead of us,
        and this model does not shrink that queue over time.
        """
        through, touch = _through_and_touch(contract_side, limit, yes_trade)
        if self.name == "optimistic":
            return through or touch
        return through

    def taker_price(self, contract_side: str, yes_bid: Decimal | None, yes_ask: Decimal | None) -> Decimal | None:
        """Cross to the ask of the side we are buying. None if that ask is unknown."""
        if contract_side == "yes":
            return yes_ask
        if yes_bid is None:
            return None
        return Decimal("1") - yes_bid

    def taker_size(self, want: Decimal, visible: Decimal | None) -> Decimal:
        if want <= 0:
            return Decimal("0")
        cap = self.unknown_visible_cap if visible is None else visible
        if cap < 0:
            cap = Decimal("0")
        return min(want, cap)

    def fee(self, *, liquidity: str, contracts: Decimal, price: Decimal) -> Decimal:
        schedule = self.schedule()
        if liquidity == "maker":
            return schedule.maker_fee(contracts, price)
        if liquidity == "taker":
            return schedule.taker_fee(contracts, price)
        raise ValueError(f"liquidity must be maker or taker, got {liquidity!r}")


def _through_and_touch(contract_side: str, limit: Decimal, yes_trade: Decimal) -> tuple[bool, bool]:
    if contract_side == "yes":
        through = yes_trade < limit
        touch = yes_trade == limit
    elif contract_side == "no":
        boundary = Decimal("1") - limit
        through = yes_trade > boundary
        touch = yes_trade == boundary
    else:
        raise ValueError("contract_side must be yes or no")
    return through, touch
