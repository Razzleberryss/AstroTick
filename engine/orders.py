"""Order planning and the only submission path the strategies use.

Rules enforced here, before any transport call:

- From T-50s (plus clock skew) cancel resting orders and refuse creates,
  amends, and decreases. The position is held.
- Resting orders are post-only, good-till-canceled, with
  ``cancel_order_on_pause``. No ``expiration_time`` (that combination has
  been reported to 400).
- Reprice (cancel + new order, which loses queue) only when fair value has
  moved by at least the threshold and the quote price would change, or the
  order is stale.
- A smaller size uses the decrease endpoint. A larger size is a second
  order; the original stays so it keeps its queue.
- At most one working price per contract side. A second level is collapsed
  to the less aggressive price. That is the anti-layering rule: this bot
  never stacks quotes it does not intend to have filled.
- Every create carries a strategy reason. Empty reasons are refused.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import ROUND_FLOOR, ROUND_HALF_UP, Decimal

from engine.cutoff import (
    CutoffBlocked,
    CutoffPolicy,
    assert_submission_allowed,
    trading_blocked,
)
from engine.risk import Exposure, RiskBook

# KXBTC15M's live tapered grid. Used only when the market payload did not
# include price_ranges. Boundaries: 0.001 outside [0.10, 0.90], 0.01 inside.
DEFAULT_PRICE_RANGES = (
    {"start": Decimal("0"), "end": Decimal("0.10"), "step": Decimal("0.001"), "end_inclusive": False},
    {"start": Decimal("0.10"), "end": Decimal("0.90"), "step": Decimal("0.01"), "end_inclusive": True},
    {"start": Decimal("0.90"), "end": Decimal("1"), "step": Decimal("0.001"), "end_inclusive": True},
)


def floor_to_tick(price: Decimal, ranges=DEFAULT_PRICE_RANGES) -> Decimal:
    """Round a bid down so it does not become more aggressive than requested."""
    if price <= 0:
        return Decimal("0")
    if price >= 1:
        return Decimal("1")
    step = Decimal("0.01")
    for band in ranges:
        start = Decimal(str(band["start"]))
        end = Decimal(str(band["end"]))
        inclusive = bool(band.get("end_inclusive", False))
        in_band = price >= start and (price <= end if inclusive else price < end)
        if in_band:
            step = Decimal(str(band["step"]))
            break
    units = (price / step).to_integral_value(rounding=ROUND_FLOOR)
    return units * step


def count_fp(count: Decimal) -> str:
    return format(count.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), "f")


def price_fp(price: Decimal) -> str:
    return format(price.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), "f")


@dataclass(frozen=True)
class QuoteIntent:
    """A genuine bid or offer the strategy wants working. Not a submission."""

    strategy: str
    ticker: str
    contract_side: str  # yes or no, the contract being bought or sold
    action: str  # buy or sell
    limit_price: Decimal  # price of contract_side, in dollars
    count: Decimal
    reason: str
    fair_value_yes: float

    def __post_init__(self) -> None:
        if self.contract_side not in ("yes", "no"):
            raise ValueError("contract_side must be yes or no")
        if self.action not in ("buy", "sell"):
            raise ValueError("action must be buy or sell")
        if not self.reason.strip():
            raise ValueError("every order needs a reason; empty reasons are refused")
        if not self.strategy.strip():
            raise ValueError("strategy name is required")


@dataclass
class TrackedQuote:
    """Working order as the planner sees it. Mirrors StateStore's order fields."""

    order_id: str
    client_order_id: str
    ticker: str
    contract_side: str
    action: str
    price: Decimal
    remaining: Decimal
    fair_value_yes: float | None
    placed_at: datetime
    strategy: str
    reason: str


@dataclass(frozen=True)
class Place:
    intent: QuoteIntent


@dataclass(frozen=True)
class Cancel:
    order: TrackedQuote
    reason: str


@dataclass(frozen=True)
class Decrease:
    order: TrackedQuote
    reduce_to: Decimal
    reason: str


@dataclass(frozen=True)
class Keep:
    order: TrackedQuote


Action = Place | Cancel | Decrease | Keep


def _less_aggressive(candidate: QuoteIntent, current: QuoteIntent) -> QuoteIntent:
    """Prefer the quote that is easier for the market to ignore.

    For a buy that is the lower price, then the smaller size. Two prices on
    one side would be layering, so only one survives.
    """
    if candidate.action != current.action or candidate.contract_side != current.contract_side:
        return current
    if candidate.limit_price < current.limit_price:
        return candidate
    if candidate.limit_price > current.limit_price:
        return current
    if candidate.count < current.count:
        return candidate
    return current


def collapse_intents(intents: list[QuoteIntent]) -> tuple[list[QuoteIntent], list[str]]:
    """One intent per (contract side, action). Extras are dropped, not stacked."""
    chosen: dict[tuple[str, str], QuoteIntent] = {}
    notes: list[str] = []
    for intent in intents:
        key = (intent.contract_side, intent.action)
        existing = chosen.get(key)
        if existing is None:
            chosen[key] = intent
            continue
        winner = _less_aggressive(intent, existing)
        loser = existing if winner is intent else intent
        chosen[key] = winner
        notes.append(
            f"collapsed {loser.strategy} {loser.contract_side} {loser.action} "
            f"@ {loser.limit_price} into a single level"
        )
    # A buy of YES and a buy of NO in the same ticker can trade against each
    # other. Keep only the first one we saw and drop the other side.
    by_ticker: dict[str, list[QuoteIntent]] = {}
    for intent in chosen.values():
        by_ticker.setdefault(intent.ticker, []).append(intent)
    kept: list[QuoteIntent] = []
    for ticker, group in by_ticker.items():
        buys = [item for item in group if item.action == "buy"]
        sells = [item for item in group if item.action == "sell"]
        if len({item.contract_side for item in buys}) > 1:
            notes.append(
                f"refusing two-sided entry on {ticker}; keeping {buys[0].contract_side} only"
            )
            buys = [buys[0]]
        kept.extend(buys)
        kept.extend(sells)
    return kept, notes


def plan_quotes(
    resting: list[TrackedQuote],
    desired: list[QuoteIntent],
    *,
    fair_value_yes: float,
    now: datetime,
    reprice_threshold: Decimal = Decimal("0.02"),
    stale_after: timedelta = timedelta(seconds=300),
    min_price_change: Decimal = Decimal("0.001"),
) -> tuple[list[Action], list[str]]:
    """Decide keep / decrease / extra order / cancel+replace. No I/O."""
    desired, notes = collapse_intents(desired)
    actions: list[Action] = []
    sides = {(q.contract_side, q.action) for q in resting} | {
        (q.contract_side, q.action) for q in desired
    }
    for contract_side, order_action in sorted(sides):
        lives = [
            order
            for order in resting
            if order.contract_side == contract_side and order.action == order_action
        ]
        lives.sort(key=lambda order: order.placed_at)
        want = next(
            (
                item
                for item in desired
                if item.contract_side == contract_side and item.action == order_action
            ),
            None,
        )
        if want is None:
            for order in lives:
                actions.append(Cancel(order, "strategy_withdrew"))
            continue
        if not lives:
            actions.append(Place(want))
            continue
        primary, *extras = lives
        for extra in extras:
            actions.append(Cancel(extra, "single_level_only"))
        fv_at_quote = primary.fair_value_yes
        fv_move = (
            abs(Decimal(str(fair_value_yes)) - Decimal(str(fv_at_quote)))
            if fv_at_quote is not None
            else reprice_threshold
        )
        age = now - primary.placed_at
        stale = age >= stale_after
        price_changed = abs(want.limit_price - primary.price) >= min_price_change
        # Fair-value moves below the threshold do not touch the queue, even
        # if the strategy's ideal price ticked. A stale order is replaced
        # even when the price is unchanged, so a forgotten quote cannot sit
        # through the rest of the window.
        moved_enough = fv_move >= reprice_threshold
        if stale or (moved_enough and price_changed):
            reason = "stale_order" if stale else "fair_value_moved"
            actions.append(Cancel(primary, reason))
            actions.append(Place(want))
            continue
        if want.count + Decimal("0.001") < primary.remaining:
            actions.append(Decrease(primary, want.count, "scale_down"))
            continue
        if want.count > primary.remaining + Decimal("0.001"):
            actions.append(Keep(primary))
            delta = want.count - primary.remaining
            actions.append(
                Place(
                    replace(
                        want,
                        count=delta,
                        reason=want.reason + " (added size as a separate order)",
                    )
                )
            )
            continue
        actions.append(Keep(primary))
    return actions, notes


def yes_book_price(intent: QuoteIntent) -> tuple[str, Decimal]:
    """Map a contract-side order onto the V2 YES book (bid = buy YES)."""
    if intent.contract_side == "yes":
        yes_price = intent.limit_price
        book_side = "bid" if intent.action == "buy" else "ask"
    else:
        yes_price = Decimal("1") - intent.limit_price
        book_side = "ask" if intent.action == "buy" else "bid"
    return book_side, yes_price


def build_v2_payload(
    intent: QuoteIntent,
    *,
    exchange_index: int,
    order_group_id: str | None,
    self_trade_prevention_type: str,
    client_order_id: str | None = None,
) -> dict:
    """Post-only GTC order. No expiration_time. cancel_order_on_pause set.

    ``reduce_only`` is not set: the API rejects it unless time_in_force is
    immediate_or_cancel, which contradicts post-only.
    """
    book_side, yes_price = yes_book_price(intent)
    yes_price = floor_to_tick(yes_price) if book_side == "bid" else yes_price
    # Asks: round the YES price up by flooring the complementary bid, so the
    # contract-side bid we are effectively showing is not more aggressive.
    if book_side == "ask":
        complement = floor_to_tick(Decimal("1") - yes_price)
        yes_price = Decimal("1") - complement
    payload = {
        "ticker": intent.ticker,
        "client_order_id": client_order_id or str(uuid.uuid4()),
        "side": book_side,
        "count": count_fp(intent.count),
        "price": price_fp(yes_price),
        "time_in_force": "good_till_canceled",
        "post_only": True,
        "cancel_order_on_pause": True,
        "self_trade_prevention_type": self_trade_prevention_type,
        "exchange_index": int(exchange_index),
    }
    if order_group_id:
        payload["order_group_id"] = order_group_id
    if "expiration_time" in payload:
        raise RuntimeError("expiration_time must not be combined with post_only GTC")
    return payload


class OrderGateway:
    """Applies the plan. The cutoff check runs before any create or decrease."""

    def __init__(
        self,
        *,
        sender,
        risk: RiskBook,
        policy: CutoffPolicy,
        reprice_threshold: Decimal = Decimal("0.02"),
        stale_order_seconds: float = 300.0,
        exchange_index: int = 2,
        self_trade_prevention_type: str = "taker_at_cross",
        order_group_id: str | None = None,
        mode: str = "paper",
    ):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        self.sender = sender
        self.risk = risk
        self.policy = policy
        self.reprice_threshold = reprice_threshold
        self.stale_order_seconds = stale_order_seconds
        self.exchange_index = exchange_index
        self.self_trade_prevention_type = self_trade_prevention_type
        self.order_group_id = order_group_id
        self.mode = mode
        self.trading_enabled = True

    def cancel_all(self, resting: list[TrackedQuote], *, reason: str) -> list[dict]:
        results = []
        for order in resting:
            self.sender.cancel(order.order_id, order.ticker)
            results.append({"action": "cancel", "order_id": order.order_id, "reason": reason})
        return results

    def sync(
        self,
        *,
        ticker: str,
        close_time: datetime,
        now: datetime,
        desired: list[QuoteIntent],
        fair_value_yes: float | None,
        resting: list[TrackedQuote],
        exposure: Exposure,
    ) -> dict:
        if trading_blocked(close_time, now, self.policy):
            cancelled = self.cancel_all(resting, reason="cutoff")
            return {
                "action": "cutoff",
                "reason": "inside T-50s window; orders cancelled, position held",
                "cancelled": cancelled,
                "placed": [],
                "decreased": [],
                "kept": [order.order_id for order in []],
            }
        if not self.trading_enabled:
            return {
                "action": "blocked",
                "reason": "trading_disabled",
                "cancelled": [],
                "placed": [],
                "decreased": [],
                "kept": [order.order_id for order in resting],
            }
        if fair_value_yes is None and any(item.action == "buy" for item in desired):
            # Entries need a model price so the decision log can be scored.
            # Exits of an existing position may still be pulled by the caller
            # passing fair_value_yes; without it we do not open risk.
            desired = [item for item in desired if item.action != "buy"]
        actions, notes = plan_quotes(
            resting,
            desired,
            fair_value_yes=fair_value_yes if fair_value_yes is not None else 0.0,
            now=now,
            reprice_threshold=self.reprice_threshold,
            stale_after=timedelta(seconds=self.stale_order_seconds),
        )
        placed: list[dict] = []
        decreased: list[dict] = []
        cancelled: list[dict] = []
        kept: list[str] = []
        # Risk is checked against a running exposure so two places in one
        # sync cannot each pass a cap the pair would break.
        running = exposure
        for action in actions:
            if isinstance(action, Keep):
                kept.append(action.order.order_id)
                continue
            if isinstance(action, Cancel):
                # Cancels are allowed through the cutoff guard.
                assert_submission_allowed(ticker, "cancel", close_time=close_time, now=now, policy=self.policy)
                self.sender.cancel(action.order.order_id, ticker)
                cancelled.append(
                    {"order_id": action.order.order_id, "reason": action.reason}
                )
                continue
            if isinstance(action, Decrease):
                assert_submission_allowed(
                    ticker, "decrease", close_time=close_time, now=now, policy=self.policy
                )
                self.sender.decrease(action.order.order_id, ticker, action.reduce_to)
                decreased.append(
                    {
                        "order_id": action.order.order_id,
                        "reduce_to": str(action.reduce_to),
                        "reason": action.reason,
                    }
                )
                continue
            if isinstance(action, Place):
                intent = action.intent
                ok, why = self.risk.check_order(
                    running,
                    add_contracts=intent.count,
                    add_dollars=intent.count * intent.limit_price,
                )
                if not ok:
                    notes.append(f"risk refused {intent.strategy}: {why}")
                    continue
                assert_submission_allowed(
                    ticker, "create", close_time=close_time, now=now, policy=self.policy
                )
                payload = build_v2_payload(
                    intent,
                    exchange_index=self.exchange_index,
                    order_group_id=self.order_group_id,
                    self_trade_prevention_type=self.self_trade_prevention_type,
                )
                # The transport must hit the same guard. A sender that calls
                # the SDK without going through assert_submission_allowed is
                # still blocked if it uses KalshiClient._submit_order.
                result = self.sender.create(payload, mode=self.mode)
                placed.append(
                    {
                        "payload": {k: v for k, v in payload.items() if k != "client_order_id"},
                        "client_order_id": payload["client_order_id"],
                        "result": result,
                        "reason": intent.reason,
                        "strategy": intent.strategy,
                        "fair_value_yes": intent.fair_value_yes,
                        "contract_side": intent.contract_side,
                        "order_action": intent.action,
                        "limit_price": str(intent.limit_price),
                    }
                )
                running = Exposure(
                    contracts_on_market=running.contracts_on_market + intent.count,
                    contracts_total=running.contracts_total + intent.count,
                    dollars_total=running.dollars_total + intent.count * intent.limit_price,
                    daily_realized_pnl=running.daily_realized_pnl,
                    feed_age_seconds=running.feed_age_seconds,
                )
        summary = "placed" if placed else ("cancelled" if cancelled else "hold")
        if notes and not placed and not cancelled and not decreased:
            summary = "hold"
        return {
            "action": summary,
            "reason": "; ".join(notes) if notes else summary,
            "notes": notes,
            "cancelled": cancelled,
            "placed": placed,
            "decreased": decreased,
            "kept": kept,
        }


class RecordingSender:
    """In-memory transport for tests and for the paper runner.

    ``create`` records the payload and returns a synthetic id. It does not
    invent a fill. Paper orders rest until the plan cancels or decreases them.
    """

    def __init__(self) -> None:
        self.creates: list[dict] = []
        self.cancels: list[tuple[str, str]] = []
        self.decreases: list[tuple[str, str, Decimal]] = []
        self.amends: list[dict] = []
        self._seq = 0

    def create(self, payload: dict, *, mode: str) -> dict:
        self._seq += 1
        order_id = f"{mode}-{self._seq}"
        self.creates.append({"payload": payload, "mode": mode, "order_id": order_id})
        return {"order_id": order_id, "status": "paper" if mode == "paper" else "submitted"}

    def cancel(self, order_id: str, ticker: str) -> None:
        self.cancels.append((order_id, ticker))

    def decrease(self, order_id: str, ticker: str, reduce_to: Decimal) -> None:
        self.decreases.append((order_id, ticker, reduce_to))

    def amend(self, payload: dict) -> None:
        # Present so a test can prove the gateway does not amend to add size.
        self.amends.append(payload)
        raise CutoffBlocked("amend is not used for size increases")


def utcnow() -> datetime:
    return datetime.now(UTC)
