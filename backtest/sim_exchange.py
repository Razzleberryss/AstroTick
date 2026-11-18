"""Simulated exchange behind ``OrderGateway``'s sender interface.

The gateway decides what is allowed. This object only records those
instructions and later matches them to prints with ``FillModel``. It does
not import a second copy of the cutoff or the strategy.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from backtest.fill_model import FillModel


@dataclass
class SimOrder:
    order_id: str
    client_order_id: str
    ticker: str
    contract_side: str
    action: str
    price: Decimal
    remaining: Decimal
    placed_at: datetime
    fair_value_yes: float | None
    strategy: str
    reason: str
    status: str = "working"
    cancelled_at: datetime | None = None


@dataclass
class SimFill:
    order_id: str
    ticker: str
    ts: datetime
    contract_side: str
    price: Decimal
    qty: Decimal
    liquidity: str
    strategy: str
    fair_value_yes: float | None
    reason: str


@dataclass
class SimExchange:
    fill_model: FillModel
    orders: dict[str, SimOrder] = field(default_factory=dict)
    create_times: list[datetime] = field(default_factory=list)
    cancel_times: list[tuple[datetime, str]] = field(default_factory=list)
    fills: list[SimFill] = field(default_factory=list)
    _seq: int = 0
    _now: datetime | None = None
    # Set by the replay from the gateway result, which knows the contract side.
    _pending_meta: dict[str, dict] = field(default_factory=dict)

    def set_clock(self, now: datetime) -> None:
        self._now = now

    def create(self, payload: dict, *, mode: str) -> dict:
        del mode
        if self._now is None:
            raise RuntimeError("SimExchange.set_clock before create")
        self._seq += 1
        order_id = f"sim-{self._seq}"
        book_side = str(payload.get("side") or "")
        yes_price = Decimal(str(payload.get("price") or "0"))
        if book_side == "bid":
            contract_side, action, price = "yes", "buy", yes_price
        else:
            contract_side, action, price = "no", "buy", Decimal("1") - yes_price
        order = SimOrder(
            order_id=order_id,
            client_order_id=str(payload.get("client_order_id") or ""),
            ticker=str(payload.get("ticker") or ""),
            contract_side=contract_side,
            action=action,
            price=price,
            remaining=Decimal(str(payload.get("count") or "0")),
            placed_at=self._now,
            fair_value_yes=None,
            strategy="",
            reason="",
        )
        self.orders[order_id] = order
        self.create_times.append(self._now)
        return {"order_id": order_id, "status": "resting"}

    def bind_gateway_meta(self, placed: dict) -> None:
        """Copy contract side and reason from the gateway's place result."""
        result = placed.get("result") or {}
        order_id = str(result.get("order_id") or "")
        order = self.orders.get(order_id)
        if order is None:
            return
        order.contract_side = str(placed.get("contract_side") or order.contract_side)
        order.action = str(placed.get("order_action") or order.action)
        if placed.get("limit_price") is not None:
            order.price = Decimal(str(placed["limit_price"]))
        order.strategy = str(placed.get("strategy") or "")
        order.reason = str(placed.get("reason") or "")
        fv = placed.get("fair_value_yes")
        order.fair_value_yes = None if fv is None else float(fv)

    def cancel(self, order_id: str, ticker: str) -> None:
        del ticker
        if self._now is None:
            raise RuntimeError("SimExchange.set_clock before cancel")
        order = self.orders.get(order_id)
        if order is not None and order.status == "working":
            order.status = "cancelled"
            order.cancelled_at = self._now
        self.cancel_times.append((self._now, order_id))

    def decrease(self, order_id: str, ticker: str, reduce_to: Decimal) -> None:
        del ticker
        order = self.orders.get(order_id)
        if order is not None and order.status == "working":
            order.remaining = min(order.remaining, Decimal(reduce_to))
            if order.remaining <= 0:
                order.status = "cancelled"
                order.cancelled_at = self._now

    def working(self, ticker: str) -> list[SimOrder]:
        return [
            order
            for order in self.orders.values()
            if order.ticker == ticker and order.status == "working" and order.remaining > 0
        ]

    def match_trade(self, ticker: str, ts: datetime, yes_price: Decimal, qty: Decimal, *, cutoff: datetime) -> list[SimFill]:
        """Match one print. Prints at or after ``cutoff`` never fill."""
        if ts >= cutoff:
            return []
        made: list[SimFill] = []
        for order in self.working(ticker):
            if ts <= order.placed_at:
                continue
            if not self.fill_model.maker_fills_on(order.contract_side, order.price, yes_price):
                continue
            fill_qty = min(order.remaining, qty)
            if fill_qty <= 0:
                continue
            order.remaining -= fill_qty
            if order.remaining <= 0:
                order.status = "filled"
            fill = SimFill(
                order_id=order.order_id,
                ticker=ticker,
                ts=ts,
                contract_side=order.contract_side,
                price=order.price,
                qty=fill_qty,
                liquidity="maker",
                strategy=order.strategy,
                fair_value_yes=order.fair_value_yes,
                reason=order.reason,
            )
            self.fills.append(fill)
            made.append(fill)
        return made
