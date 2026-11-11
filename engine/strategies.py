"""Candidate strategies. Both default off.

Neither strategy is claimed to have an out-of-sample edge. The fade study
found no retail-fade edge that survived fees. These two were the least-bad
leads and exist so they can be paper-tracked, one contract at a time, with
the decision log. They quote a single post-only price they are willing to
trade. They do not stack levels, cross the book, or cancel in the same
breath they quote.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from engine.book import Book
from engine.orders import QuoteIntent, floor_to_tick


@dataclass(frozen=True)
class MarketView:
    ticker: str
    book: Book
    fair_value_yes: float | None
    tau_seconds: float


@dataclass
class ExtremeFade:
    """Buy the cheap side when the other side is priced very rich vs the model.

    Trigger: YES (or NO) price >= ``extreme_price`` and model fair value of
    that side is at least ``min_gap`` lower. The order is a post-only bid on
    the other side, joined at that side's best bid when the bid is at or
    below the model's fair value. Disabled unless ``enabled`` is set.
    """

    enabled: bool = False
    extreme_price: Decimal = Decimal("0.85")
    min_gap: Decimal = Decimal("0.10")
    contracts: Decimal = Decimal("1")
    name: str = "extreme_fade"

    def propose(self, view: MarketView) -> QuoteIntent | None:
        if not self.enabled or view.fair_value_yes is None or view.book.crossed:
            return None
        fair_yes = Decimal(str(view.fair_value_yes))
        fair_no = Decimal("1") - fair_yes
        yes_price = view.book.yes_bid
        no_price = view.book.no_bid
        # Rich YES → buy NO, but only by joining an existing NO bid we
        # actually want filled (at or below fair NO, and not through the ask).
        if (
            yes_price is not None
            and yes_price >= self.extreme_price
            and fair_yes <= yes_price - self.min_gap
            and no_price is not None
            and view.book.no_ask is not None
            and no_price < view.book.no_ask
            and no_price <= fair_no
        ):
            limit = floor_to_tick(no_price)
            if limit <= 0 or limit >= 1:
                return None
            return QuoteIntent(
                strategy=self.name,
                ticker=view.ticker,
                contract_side="no",
                action="buy",
                limit_price=limit,
                count=self.contracts,
                reason=(
                    f"extreme fade: YES bid {yes_price} >= {self.extreme_price} and "
                    f"fair YES {fair_yes} is >= {self.min_gap} lower; joining NO bid {limit}"
                ),
                fair_value_yes=view.fair_value_yes,
            )
        if (
            no_price is not None
            and no_price >= self.extreme_price
            and fair_no <= no_price - self.min_gap
            and yes_price is not None
            and view.book.yes_ask is not None
            and yes_price < view.book.yes_ask
            and yes_price <= fair_yes
        ):
            limit = floor_to_tick(yes_price)
            if limit <= 0 or limit >= 1:
                return None
            return QuoteIntent(
                strategy=self.name,
                ticker=view.ticker,
                contract_side="yes",
                action="buy",
                limit_price=limit,
                count=self.contracts,
                reason=(
                    f"extreme fade: NO bid {no_price} >= {self.extreme_price} and "
                    f"fair NO {fair_no} is >= {self.min_gap} lower; joining YES bid {limit}"
                ),
                fair_value_yes=view.fair_value_yes,
            )
        return None


@dataclass
class FavoriteMaker:
    """Rest a single post-only bid on the favorite when it is priced 70-90c.

    One level, joined at the current best bid, only while that bid sits in
    the band. The model-agreement filter is off unless configured: in the
    fade study, requiring the model made the sweep markouts worse. Disabled
    unless ``enabled`` is set.
    """

    enabled: bool = False
    low: Decimal = Decimal("0.70")
    high: Decimal = Decimal("0.90")
    contracts: Decimal = Decimal("1")
    require_model: bool = False
    model_cushion: Decimal = Decimal("0.02")
    name: str = "favorite_maker"

    def propose(self, view: MarketView) -> QuoteIntent | None:
        if not self.enabled or view.book.crossed:
            return None
        for side, bid, ask, fair_side in (
            (
                "yes",
                view.book.yes_bid,
                view.book.yes_ask,
                None if view.fair_value_yes is None else Decimal(str(view.fair_value_yes)),
            ),
            (
                "no",
                view.book.no_bid,
                view.book.no_ask,
                None
                if view.fair_value_yes is None
                else Decimal("1") - Decimal(str(view.fair_value_yes)),
            ),
        ):
            if bid is None or ask is None:
                continue
            if not (self.low <= bid <= self.high):
                continue
            if bid >= ask:
                continue
            if self.require_model:
                if fair_side is None or fair_side < bid + self.model_cushion:
                    continue
            limit = floor_to_tick(bid)
            if limit < self.low or limit > self.high:
                continue
            fair = view.fair_value_yes if view.fair_value_yes is not None else 0.5
            return QuoteIntent(
                strategy=self.name,
                ticker=view.ticker,
                contract_side=side,
                action="buy",
                limit_price=limit,
                count=self.contracts,
                reason=(
                    f"favorite maker: resting bid on {side} at {limit} "
                    f"inside [{self.low}, {self.high}]"
                ),
                fair_value_yes=float(fair),
            )
        return None
