"""Replay markets through the live engine.

``ExtremeFade`` / ``FavoriteMaker``, ``fair_value_yes``, ``RiskBook``, and
``OrderGateway`` are the live classes. This module only chooses causal
inputs and hands quotes to the gateway.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np

from backtest.adapters import Dataset
from backtest.features import asof_index, asof_price, blended, trailing_sigma
from backtest.fill_model import FillModel
from backtest.settlement import resolves_yes
from backtest.sim_exchange import SimExchange, SimOrder
from engine.book import Book
from engine.cutoff import CutoffPolicy, register_close, reset_registry
from engine.fair_value import fair_value_yes
from engine.orders import OrderGateway, TrackedQuote
from engine.risk import Exposure, RiskBook, RiskLimits
from engine.strategies import ExtremeFade, FavoriteMaker, MarketView

_STEP = 10.0
_SHORT_RETURNS = 90  # 15 minutes of 10s returns
_LONG_RETURNS = 360  # 60 minutes


def strategies_for(name: str) -> list:
    if name == "extreme_fade":
        return [ExtremeFade(enabled=True)]
    if name == "favorite_maker":
        return [FavoriteMaker(enabled=True)]
    if name == "all":
        return [ExtremeFade(enabled=True), FavoriteMaker(enabled=True)]
    raise ValueError(f"unknown strategy {name!r}")


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, UTC)


@dataclass
class ScoreBoard:
    """One fair-value score per market per time bucket, plus a headline at ~5 min."""

    buckets: dict[str, list[tuple[float, int]]] = field(default_factory=dict)
    headline: list[tuple[float, int]] = field(default_factory=list)

    def add(self, probability: float, yes: int, tau: float, *, headline: bool) -> None:
        name = tau_bucket(tau)
        if name is None:
            return
        self.buckets.setdefault(name, []).append((probability, yes))
        if headline:
            self.headline.append((probability, yes))


def tau_bucket(tau: float) -> str | None:
    if tau <= 50:
        return None
    if tau <= 60:
        return "50-60s"
    if tau <= 120:
        return "60-120s"
    if tau <= 300:
        return "2-5m"
    if tau <= 600:
        return "5-10m"
    if tau <= 900:
        return "10-15m"
    return "over-15m"


def price_bucket(price: float) -> str:
    if price < 0.15:
        return "0-15c"
    if price < 0.30:
        return "15-30c"
    if price < 0.50:
        return "30-50c"
    if price < 0.70:
        return "50-70c"
    if price < 0.85:
        return "70-85c"
    return "85c-1"


@dataclass
class ReplayOutput:
    fills: list[dict]
    decisions_path: str | None
    n_markets: int
    n_decisions: int
    n_orders: int
    order_times: list[float]
    n_cancels_at_cutoff: int
    max_contracts: float
    max_notional: float
    scores_train: ScoreBoard
    scores_oos: ScoreBoard
    assumptions: list[str]


def _resample_10s(ts: np.ndarray, px: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    if len(ts) == 0:
        return ts, px
    start = math.floor(float(ts[0]))
    stop = math.floor(float(ts[-1]))
    grid = np.arange(start, stop + _STEP, _STEP, dtype=np.float64)
    idx = np.searchsorted(ts, grid, side="right") - 1
    safe = np.clip(idx, 0, len(ts) - 1)
    age = grid - ts[safe]
    values = px[safe]
    ok = (idx >= 0) & (age >= 0) & (age <= _STEP) & np.isfinite(values) & (values > 0)
    return grid, np.where(ok, values, np.nan)


def _basis(dataset: Dataset, open_ts: float, strike: float) -> tuple[float, str]:
    """Strike / mean proxy over the 60s ending at open. Known at the open."""
    samples = []
    for offset in range(59, -1, -1):
        px = asof_price(dataset.spot_ts, dataset.spot_px, open_ts - offset, tolerance=2.0)
        if px is not None:
            samples.append(px)
    if len(samples) < 30:
        return 1.0, "basis_unavailable_used_1"
    mean = sum(samples) / len(samples)
    if mean <= 0:
        return 1.0, "basis_unavailable_used_1"
    return strike / mean, "basis_strike_over_open_average"


def _locked(dataset: Dataset, close_ts: float, now_ts: float, basis: float) -> list[float] | None:
    locked: list[float] = []
    for offset in range(59, -1, -1):
        sample_ts = close_ts - offset
        if sample_ts > now_ts:
            break
        px = asof_price(dataset.spot_ts, dataset.spot_px, sample_ts, tolerance=2.0)
        if px is None:
            return None
        locked.append(px * basis)
    return locked


def _quotes_at(dataset: Dataset, ticker: str, t: float) -> list[tuple[float, Book]]:
    """Every quote whose timestamp is <= t, newest last. Nothing after t."""
    found: list[tuple[float, Book]] = []
    ladder = dataset.ladder_asof(ticker, t)
    if ladder is not None and ladder.yes_bids and ladder.no_bids:
        found.append(
            (ladder.ts, Book(Decimal(str(ladder.yes_bids[0][0])), Decimal(str(ladder.no_bids[0][0]))))
        )
    snap = dataset.book_asof(ticker, t)
    if snap is not None and snap.yes_bid is not None and snap.no_bid is not None:
        found.append((snap.ts, Book(Decimal(str(snap.yes_bid)), Decimal(str(snap.no_bid)))))
    elif snap is not None and snap.yes_bid is not None and snap.yes_ask is not None:
        found.append(
            (snap.ts, Book(Decimal(str(snap.yes_bid)), Decimal("1") - Decimal(str(snap.yes_ask))))
        )
    candle = dataset.candle_asof(ticker, t)
    if candle is not None and candle.bid is not None and candle.ask is not None:
        found.append(
            (candle.end_ts, Book(Decimal(str(candle.bid)), Decimal("1") - Decimal(str(candle.ask))))
        )
    found.sort(key=lambda item: item[0])
    return found


def _book(dataset: Dataset, ticker: str, t: float) -> Book:
    quotes = _quotes_at(dataset, ticker, t)
    if not quotes:
        return Book(None, None)
    return quotes[-1][1]


def _quotes(orders: list[SimOrder]) -> list[TrackedQuote]:
    return [
        TrackedQuote(
            order_id=order.order_id,
            client_order_id=order.client_order_id,
            ticker=order.ticker,
            contract_side=order.contract_side,
            action=order.action,
            price=order.price,
            remaining=order.remaining,
            fair_value_yes=order.fair_value_yes,
            placed_at=order.placed_at,
            strategy=order.strategy,
            reason=order.reason,
        )
        for order in orders
    ]


def _decision_times(dataset: Dataset, ticker: str, open_ts: float, cutoff_ts: float) -> list[float]:
    times = {cutoff_ts}
    for candle in dataset.candles.get(ticker) or []:
        if open_ts < candle.end_ts <= cutoff_ts:
            times.add(candle.end_ts)
    for snap in dataset.books.get(ticker) or []:
        if open_ts < snap.ts <= cutoff_ts:
            times.add(snap.ts)
    for snap in dataset.ladders.get(ticker) or []:
        if open_ts < snap.ts <= cutoff_ts:
            times.add(snap.ts)
    return sorted(times)


def replay(
    dataset: Dataset,
    *,
    strategy: str,
    fill_model: FillModel,
    start_ts: float | None = None,
    end_ts: float | None = None,
    oos_start_ts: float = datetime(2026, 7, 1, tzinfo=UTC).timestamp(),
    policy: CutoffPolicy | None = None,
    decision_log=None,
) -> ReplayOutput:
    policy = policy or CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0)
    block = policy.block_when_remaining_at_most
    grid, grid_px = _resample_10s(dataset.spot_ts, dataset.spot_px)
    # Forward-fill NaN gaps with the previous causal value so a single miss
    # does not wipe the trailing window. The fill never uses a future price.
    filled = grid_px.copy()
    last = np.nan
    for i, value in enumerate(filled):
        if np.isfinite(value):
            last = value
        else:
            filled[i] = last
    sig_short = trailing_sigma(filled, _STEP, _SHORT_RETURNS)
    sig_long = trailing_sigma(filled, _STEP, _LONG_RETURNS)

    strategies = strategies_for(strategy)
    exchange = SimExchange(fill_model)
    gateway = OrderGateway(
        sender=exchange,
        risk=RiskBook(
            RiskLimits(
                max_contracts_per_market=Decimal("1"),
                max_contracts_total=Decimal("2"),
                max_dollars_total=Decimal("5"),
                max_daily_loss_dollars=Decimal("1000000"),
            )
        ),
        policy=policy,
        mode="paper",
        stale_order_seconds=10_000,
    )
    scores_train, scores_oos = ScoreBoard(), ScoreBoard()
    fills: list[dict] = []
    n_decisions = 0
    n_orders = 0
    order_times: list[float] = []
    n_cancels = 0
    max_contracts = 0.0
    max_notional = 0.0
    assumptions = [
        "maker fills only on a strict trade-through in conservative mode; queue is not advanced",
        "adverse-selection haircut is subtracted from maker settlement PnL, not observed in the prints",
        "Binance close is stored at the kline open plus 1s, when that close is known",
        "Binance (or any 1s proxy) is scaled by strike / open-minute average; it is not BRTI",
        "settlement uses expiration_value >= floor_strike (tie -> YES), not the proxy average",
        "no book and no candle means the strategy sees an empty book and does not quote",
    ]
    reset_registry()
    markets = [
        market
        for market in dataset.markets
        if (start_ts is None or market.close_ts >= start_ts)
        and (end_ts is None or market.close_ts < end_ts)
    ]
    for market in markets:
        yes = resolves_yes(market.expiration_value, market.strike, market.result)
        if yes is None:
            continue
        cutoff_ts = market.close_ts - block
        basis, basis_note = _basis(dataset, market.open_ts, market.strike)
        split = "oos" if market.close_ts >= oos_start_ts else "train"
        board = scores_oos if split == "oos" else scores_train
        register_close(market.ticker, _dt(market.close_ts))
        times = _decision_times(dataset, market.ticker, market.open_ts, cutoff_ts)
        held = Decimal("0")
        held_notional = Decimal("0")
        bucket_best: dict[str, tuple[float, float]] = {}
        headline_best: tuple[float, float] | None = None
        for index, t in enumerate(times):
            now = _dt(t)
            exchange.set_clock(now)
            tau = market.close_ts - t
            spot = dataset.spot(t, tolerance=2.0)
            sigma = None
            fv = None
            locked_n = 0
            if spot is not None:
                grid_i = asof_index(grid, t)
                if grid_i >= 0 and np.isfinite(sig_short[grid_i]):
                    long = sig_long[grid_i] if np.isfinite(sig_long[grid_i]) else None
                    short = float(sig_short[grid_i])
                    sigma = blended(short, None if long is None or not np.isfinite(long) else float(long))
                if sigma is not None and sigma > 0:
                    locked: list[float] | None = []
                    if tau < 60:
                        locked = _locked(dataset, market.close_ts, t, basis)
                        locked_n = 0 if locked is None else len(locked)
                    if locked is not None:
                        priced = fair_value_yes(
                            spot=float(spot) * basis,
                            strike=float(market.strike),
                            tau_seconds=float(tau),
                            sigma_per_sqrt_second=float(sigma),
                            locked_samples=locked,
                        )
                        if priced is not None:
                            fv = priced.probability_yes
            book = _book(dataset, market.ticker, t)
            view = MarketView(
                ticker=market.ticker,
                book=book,
                fair_value_yes=fv,
                tau_seconds=float(tau),
            )
            intents = []
            for item in strategies:
                quote = item.propose(view)
                if quote is not None:
                    intents.append(quote)
            working = exchange.working(market.ticker)
            exposure = Exposure(
                contracts_on_market=held + sum((order.remaining for order in working), Decimal("0")),
                contracts_total=held + sum((order.remaining for order in working), Decimal("0")),
                dollars_total=held_notional
                + sum((order.remaining * order.price for order in working), Decimal("0")),
                daily_realized_pnl=Decimal("0"),
                feed_age_seconds=0.0,
            )
            sync = gateway.sync(
                ticker=market.ticker,
                close_time=_dt(market.close_ts),
                now=now,
                desired=intents,
                fair_value_yes=fv,
                resting=_quotes(working),
                exposure=exposure,
            )
            for placed in sync.get("placed") or []:
                exchange.bind_gateway_meta(placed)
                n_orders += 1
                order_times.append(t)
            if tau <= block:
                n_cancels += len(sync.get("cancelled") or [])
            n_decisions += 1
            if fv is not None:
                name = tau_bucket(tau)
                if name is not None:
                    center = {
                        "50-60s": 55, "60-120s": 90, "2-5m": 210,
                        "5-10m": 450, "10-15m": 750, "over-15m": 1000,
                    }[name]
                    dist = abs(tau - center)
                    previous = bucket_best.get(name)
                    if previous is None or dist < previous[0]:
                        bucket_best[name] = (dist, fv)
                dist_h = abs(tau - 300)
                if headline_best is None or dist_h < headline_best[0]:
                    headline_best = (dist_h, fv)
            if decision_log is not None:
                decision_log(
                    {
                        "type": "decision",
                        "ticker": market.ticker,
                        "ts": now.isoformat(),
                        "tau_seconds": tau,
                        "split": split,
                        "inputs": {
                            "spot": spot,
                            "basis": basis,
                            "basis_note": basis_note,
                            "strike": market.strike,
                            "sigma_per_sqrt_second": sigma,
                            "locked_count": locked_n,
                        },
                        "fair_value_yes": fv,
                        "book": book.as_log_dict(),
                        "action": "cutoff" if tau <= block else (sync.get("action") or "hold"),
                        "reason": sync.get("reason"),
                        "strategy": strategy,
                        "intents": [
                            {
                                "strategy": item.strategy,
                                "contract_side": item.contract_side,
                                "limit_price": str(item.limit_price),
                                "count": str(item.count),
                                "reason": item.reason,
                            }
                            for item in intents
                        ],
                    }
                )
            next_t = times[index + 1] if index + 1 < len(times) else cutoff_ts
            for print_ in dataset.trades.get(market.ticker) or []:
                if not (t < print_.ts < next_t and print_.ts < cutoff_ts):
                    continue
                made = exchange.match_trade(
                    market.ticker,
                    _dt(print_.ts),
                    Decimal(str(print_.yes_price)),
                    Decimal(str(print_.qty)),
                    cutoff=_dt(cutoff_ts),
                )
                for fill in made:
                    held += fill.qty
                    held_notional += fill.qty * fill.price
                    fee = fill_model.fee(liquidity="maker", contracts=fill.qty, price=fill.price)
                    haircut = fill.qty * fill_model.haircut_per_contract
                    won = (fill.contract_side == "yes" and yes) or (fill.contract_side == "no" and not yes)
                    payout = Decimal("1") if won else Decimal("0")
                    pnl_raw = fill.qty * (payout - fill.price) - fee
                    row = {
                        "type": "fill",
                        "ticker": market.ticker,
                        "ts": fill.ts.isoformat(),
                        "ts_unix": print_.ts,
                        "close_ts": market.close_ts,
                        "split": split,
                        "strategy": fill.strategy or strategy,
                        "contract_side": fill.contract_side,
                        "price": str(fill.price),
                        "qty": str(fill.qty),
                        "liquidity": fill.liquidity,
                        "fee": str(fee),
                        "haircut": str(haircut),
                        "payout": str(payout),
                        "pnl_raw": str(pnl_raw),
                        "pnl_net": str(pnl_raw - haircut),
                        "won": won,
                        "yes_settlement": yes,
                        "fair_value_yes": fill.fair_value_yes,
                        "reason": fill.reason,
                    }
                    row["markout_10s"] = _markout(dataset, market.ticker, print_.ts, fill)
                    row["markout_60s"] = _markout(dataset, market.ticker, print_.ts, fill, horizon=60)
                    fills.append(row)
                    max_contracts = max(max_contracts, float(held))
                    max_notional = max(max_notional, float(held_notional))
                    if decision_log is not None:
                        decision_log(row)
            max_contracts = max(max_contracts, float(held))
            max_notional = max(max_notional, float(held_notional))
        label = 1 if yes else 0
        for name, (_dist, probability) in bucket_best.items():
            board.buckets.setdefault(name, []).append((probability, label))
        if headline_best is not None:
            board.headline.append((headline_best[1], label))
    return ReplayOutput(
        fills=fills,
        decisions_path=None,
        n_markets=len(markets),
        n_decisions=n_decisions,
        n_orders=n_orders,
        order_times=order_times,
        n_cancels_at_cutoff=n_cancels,
        max_contracts=max_contracts,
        max_notional=max_notional,
        scores_train=scores_train,
        scores_oos=scores_oos,
        assumptions=assumptions,
    )


def _markout(dataset: Dataset, ticker: str, fill_ts: float, fill, *, horizon: float = 10.0) -> float | None:
    """Side mid after the fill, at or before fill+horizon, minus the fill price, in cents.

    A quote from before the fill is not a post-fill markout. If nothing was
    observed in ``(fill_ts, fill_ts + horizon]``, the markout is missing.
    """
    horizon_t = fill_ts + horizon
    fresh = [
        (obs_ts, book)
        for obs_ts, book in _quotes_at(dataset, ticker, horizon_t)
        if fill_ts < obs_ts <= horizon_t and book.yes_bid is not None and book.yes_ask is not None
    ]
    if not fresh:
        return None
    _obs_ts, book = fresh[-1]
    mid = (book.yes_bid + book.yes_ask) / 2
    side_mid = mid if fill.contract_side == "yes" else Decimal("1") - mid
    return float((side_mid - fill.price) * 100)


def parse_date(value: str | None, *, end: bool = False) -> float | None:
    """UTC date or datetime. ``end`` uses the next midnight so the end day is excluded."""
    if value is None or value == "":
        return None
    text = value.strip().replace("Z", "+00:00")
    if len(text) == 10:
        dt = datetime.fromisoformat(text).replace(tzinfo=UTC)
        if end:
            dt = dt + timedelta(days=1)
        return dt.timestamp()
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()
