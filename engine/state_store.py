"""Crash-safe local state: orders, positions, and realized PnL.

The file is the source of what this process believed. On startup the runner
reconciles it with the API (open orders and positions). API contract counts
win. Local rows the exchange no longer shows are marked cancelled or settled
rather than deleted, so a restart does not forget a live position.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from kalshi_money import position_count


def _dec(value: Decimal | str | int | float) -> Decimal:
    return Decimal(str(value))


def _now_iso(now: datetime | None = None) -> str:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat()


@dataclass
class TrackedOrder:
    order_id: str
    client_order_id: str
    ticker: str
    contract_side: str
    action: str
    price: str
    count: str
    remaining: str
    fair_value_yes: float | None
    placed_at: str
    strategy: str
    reason: str
    status: str
    mode: str

    @property
    def remaining_contracts(self) -> Decimal:
        return _dec(self.remaining)

    @property
    def placed_dt(self) -> datetime:
        return datetime.fromisoformat(self.placed_at)


@dataclass
class TrackedPosition:
    ticker: str
    side: str
    contracts: str
    avg_price: str

    @property
    def size(self) -> Decimal:
        return _dec(self.contracts)


@dataclass
class BotState:
    version: int = 1
    orders: list[TrackedOrder] = field(default_factory=list)
    positions: dict[str, TrackedPosition] = field(default_factory=dict)
    daily_realized_pnl: dict[str, str] = field(default_factory=dict)
    balance_dollars: str | None = None
    balance_updated_at: str | None = None
    order_group_id: str | None = None
    notes: list[str] = field(default_factory=list)

    def working_orders(self, ticker: str | None = None) -> list[TrackedOrder]:
        rows = [
            order
            for order in self.orders
            if order.status in ("resting", "paper") and order.remaining_contracts > 0
        ]
        if ticker is None:
            return rows
        return [order for order in rows if order.ticker == ticker]

    def daily_pnl(self, day: str) -> Decimal:
        return _dec(self.daily_realized_pnl.get(day, "0"))

    def contracts_on(self, ticker: str) -> Decimal:
        total = Decimal("0")
        pos = self.positions.get(ticker)
        if pos is not None:
            total += pos.size
        for order in self.working_orders(ticker):
            if order.action == "buy":
                total += order.remaining_contracts
        return total

    def contracts_total(self) -> Decimal:
        seen: set[str] = set(self.positions) | {o.ticker for o in self.working_orders()}
        return sum((self.contracts_on(ticker) for ticker in seen), Decimal("0"))

    def dollars_total(self) -> Decimal:
        total = Decimal("0")
        for pos in self.positions.values():
            total += pos.size * _dec(pos.avg_price)
        for order in self.working_orders():
            if order.action == "buy":
                total += order.remaining_contracts * _dec(order.price)
        return total


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.state = BotState()

    def load(self) -> BotState:
        if not self.path.exists():
            self.state = BotState()
            return self.state
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        orders = [TrackedOrder(**row) for row in raw.get("orders", [])]
        positions = {
            ticker: TrackedPosition(**row)
            for ticker, row in (raw.get("positions") or {}).items()
        }
        self.state = BotState(
            version=int(raw.get("version", 1)),
            orders=orders,
            positions=positions,
            daily_realized_pnl=dict(raw.get("daily_realized_pnl") or {}),
            balance_dollars=raw.get("balance_dollars"),
            balance_updated_at=raw.get("balance_updated_at"),
            order_group_id=raw.get("order_group_id"),
            notes=list(raw.get("notes") or []),
        )
        return self.state

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": self.state.version,
            "orders": [asdict(order) for order in self.state.orders],
            "positions": {
                ticker: asdict(pos) for ticker, pos in self.state.positions.items()
            },
            "daily_realized_pnl": self.state.daily_realized_pnl,
            "balance_dollars": self.state.balance_dollars,
            "balance_updated_at": self.state.balance_updated_at,
            "order_group_id": self.state.order_group_id,
            "notes": self.state.notes[-200:],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def add_realized(self, dollars: Decimal, now: datetime) -> None:
        day = now.astimezone(UTC).date().isoformat()
        current = self.state.daily_pnl(day)
        self.state.daily_realized_pnl[day] = str(current + dollars)

    def note_settlement(self, ticker: str, result: str, now: datetime) -> Decimal | None:
        """Realize a held position against a yes/no settlement and drop it."""
        pos = self.state.positions.get(ticker)
        if pos is None:
            return None
        won = result.lower() == pos.side.lower()
        price = _dec(pos.avg_price)
        size = pos.size
        pnl = size * ((Decimal("1") - price) if won else -price)
        self.add_realized(pnl, now)
        del self.state.positions[ticker]
        self.state.notes.append(
            f"{_now_iso(now)} settled {ticker} result={result} pnl={pnl}"
        )
        return pnl

    def set_balance(self, dollars: Decimal, now: datetime | None = None) -> None:
        self.state.balance_dollars = str(dollars)
        self.state.balance_updated_at = _now_iso(now)

    def reconcile(
        self,
        api_orders: list[dict],
        api_positions: list[dict],
        *,
        now: datetime | None = None,
    ) -> list[str]:
        """Align local working orders and positions with an API snapshot.

        Returns human-readable notes describing every change. Contract counts
        come from the API. Average prices stay local when the API row does
        not carry one.
        """
        notes: list[str] = []
        current = now or datetime.now(UTC)
        live_ids = set()
        for row in api_orders:
            order_id = str(row.get("order_id") or "")
            if not order_id:
                continue
            status = str(row.get("status") or "resting").lower()
            if status in ("canceled", "cancelled", "executed"):
                continue
            live_ids.add(order_id)
            known = next((o for o in self.state.orders if o.order_id == order_id), None)
            remaining = str(row.get("remaining_count") or row.get("remaining_count_fp") or row.get("count") or "0")
            if known is None:
                adopted = TrackedOrder(
                    order_id=order_id,
                    client_order_id=str(row.get("client_order_id") or ""),
                    ticker=str(row.get("ticker") or row.get("market_ticker") or ""),
                    contract_side=str(row.get("contract_side") or row.get("outcome_side") or "yes"),
                    action=str(row.get("action") or "buy"),
                    price=str(row.get("price") or "0"),
                    count=str(row.get("count") or remaining),
                    remaining=remaining,
                    fair_value_yes=None,
                    placed_at=_now_iso(current),
                    strategy="adopted",
                    reason="reconciled from API; not placed by this process",
                    status="resting",
                    mode="live",
                )
                self.state.orders.append(adopted)
                notes.append(f"adopted unknown order {order_id} on {adopted.ticker}")
            else:
                if known.remaining != remaining:
                    notes.append(
                        f"order {order_id} remaining {known.remaining} -> {remaining}"
                    )
                    known.remaining = remaining
                known.status = "resting"

        for order in self.state.orders:
            if order.status == "paper":
                continue
            if order.status == "resting" and order.order_id not in live_ids:
                order.status = "cancelled"
                order.remaining = "0"
                notes.append(f"local order {order.order_id} no longer resting; marked cancelled")

        api_pos: dict[str, TrackedPosition] = {}
        for row in api_positions:
            ticker = str(row.get("ticker") or "")
            if not ticker:
                continue
            signed = position_count(row)
            if abs(signed) < 0.01:
                continue
            side = "yes" if signed > 0 else "no"
            previous = self.state.positions.get(ticker)
            if previous is not None and previous.side == side:
                avg = previous.avg_price
            else:
                raw_avg = row.get("avg_price")
                avg = str(raw_avg) if raw_avg not in (None, "") else "0"
            api_pos[ticker] = TrackedPosition(
                ticker=ticker,
                side=side,
                contracts=str(Decimal(str(abs(signed))).quantize(Decimal("0.01"))),
                avg_price=str(avg),
            )
            if previous is None:
                notes.append(f"adopted position {ticker} {side} {api_pos[ticker].contracts}")
            elif previous.contracts != api_pos[ticker].contracts or previous.side != side:
                notes.append(
                    f"position {ticker} {previous.side} {previous.contracts} -> "
                    f"{side} {api_pos[ticker].contracts}"
                )

        for ticker in list(self.state.positions):
            if ticker not in api_pos:
                notes.append(
                    f"local position {ticker} absent from API; left in place until settlement is observed"
                )
        for ticker, pos in api_pos.items():
            self.state.positions[ticker] = pos

        self.state.notes.extend(f"{_now_iso(current)} {note}" for note in notes)
        return notes
