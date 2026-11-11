"""One decision cycle and the demo/paper process entrypoint.

``python -m engine.runner --once --paper`` loads config, checks that a key
file exists without printing it, reads the public demo book, and writes a
decision line. It does not send an order. Live sends require
``KALSHI_TRADING_LIVE=1`` and, for production, ``KALSHI_ENV=prod`` together
with ``KALSHI_ALLOW_PROD=1``.

The crypto shard balance is checked before any live send. Unknown or zero
balance refuses orders. The balance endpoint is called at startup and then
no more often than ``BALANCE_RECONCILE_SECONDS`` (the endpoint's own bucket
is about one request per second). Fills and settlements move a local copy
between reconciles.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import config
from engine.balance import allow_trading, primary_shard_balance
from engine.book import Book, parse_orderbook
from engine.clock_watch import BookCache
from engine.cutoff import CutoffBlocked, register_close, trading_blocked
from engine.decision_log import DecisionLog
from engine.discovery import discover
from engine.fair_value import fair_value_yes
from engine.fees import schedule_from_api
from engine.index_series import extract_index_series, slice_lookback
from engine.orders import OrderGateway, QuoteIntent, RecordingSender, TrackedQuote
from engine.public_read import DEMO_REST_HOSTS, PROD_REST_HOSTS, PublicRead, PublicReadError
from engine.rate_limit import RateLimiter
from engine.risk import Exposure, RiskBook, RiskLimits
from engine.settings import EngineSettings, load_settings
from engine.state_store import StateStore, TrackedOrder
from engine.strategies import ExtremeFade, FavoriteMaker, MarketView
from engine.vol import blend_sigma, realized_sigma

log = logging.getLogger("engine.runner")


def _parse_time(value: str) -> datetime:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def _strike(market: dict) -> Decimal | None:
    raw = market.get("floor_strike")
    if raw is None or raw == "":
        return None
    try:
        return Decimal(str(raw))
    except Exception:
        return None


def _quotes_from_state(store: StateStore, ticker: str) -> list[TrackedQuote]:
    quotes: list[TrackedQuote] = []
    for order in store.state.working_orders(ticker):
        quotes.append(
            TrackedQuote(
                order_id=order.order_id,
                client_order_id=order.client_order_id,
                ticker=order.ticker,
                contract_side=order.contract_side,
                action=order.action,
                price=Decimal(order.price),
                remaining=Decimal(order.remaining),
                fair_value_yes=order.fair_value_yes,
                placed_at=order.placed_dt,
                strategy=order.strategy,
                reason=order.reason,
            )
        )
    return quotes


def _apply_sync(store: StateStore, ticker: str, sync: dict, mode: str, now: datetime) -> None:
    cancelled_ids = {row["order_id"] for row in sync.get("cancelled", [])}
    decreased = {row["order_id"]: row["reduce_to"] for row in sync.get("decreased", [])}
    for order in store.state.orders:
        if order.ticker != ticker:
            continue
        if order.order_id in cancelled_ids:
            order.status = "cancelled"
            order.remaining = "0"
        elif order.order_id in decreased:
            order.remaining = str(decreased[order.order_id])
    for placed in sync.get("placed", []):
        result = placed.get("result") or {}
        order_id = str(result.get("order_id") or placed.get("client_order_id"))
        payload = placed["payload"]
        # payload price is the YES-book price. Recover the contract-side price
        # from the reason's intent via the stored fair-value record: the
        # sender payload is what we sent, and contract side is on the result
        # only if we kept it. The decision record's placed entry includes
        # strategy; the state row uses the YES price when side is bid/ask.
        contract_side = str(placed.get("contract_side") or "yes")
        action = str(placed.get("order_action") or "buy")
        price = Decimal(str(placed.get("limit_price") or payload["price"]))
        store.state.orders.append(
            TrackedOrder(
                order_id=order_id,
                client_order_id=str(placed.get("client_order_id") or ""),
                ticker=ticker,
                contract_side=contract_side,
                action=action,
                price=str(price),
                count=str(payload["count"]),
                remaining=str(payload["count"]),
                fair_value_yes=placed.get("fair_value_yes"),
                placed_at=now.astimezone(UTC).isoformat(),
                strategy=str(placed.get("strategy") or ""),
                reason=str(placed.get("reason") or ""),
                status="paper" if mode == "paper" else "resting",
                mode=mode,
            )
        )


def handle_stream_event(cache: BookCache, gateway: OrderGateway, resting: list[TrackedQuote], event: dict) -> str:
    """Apply one websocket event.

    A disconnect cancels every resting order. A backwards ``clock`` clears
    the cache and asks the caller to fetch a fresh snapshot. Deltas before
    that snapshot are ignored.
    """
    if event.get("type") == "disconnect" or event.get("event") == "disconnect":
        cache.on_disconnect()
        gateway.cancel_all(resting, reason="websocket_disconnect")
        return "cancelled"
    outcome = cache.on_message(event)
    if outcome == "reset":
        return "resnapshot"
    return outcome


@dataclass
class CycleData:
    now: datetime
    markets: list[dict]
    books: dict[str, Book]
    indexes: dict[str, object]
    fee_type: str | None
    fee_multiplier: str | None
    feed_age_seconds: float | None
    feed_disconnected: bool
    discovery_source: str
    shard_ok: bool
    shard_reason: str


def run_cycle(
    data: CycleData,
    *,
    settings: EngineSettings,
    store: StateStore,
    gateway: OrderGateway,
    decisions: DecisionLog,
) -> list[dict]:
    """Log fair value against the book and submit only through the gateway."""
    records: list[dict] = []
    strategies = [
        ExtremeFade(
            enabled=settings.enable_extreme_fade,
            extreme_price=settings.extreme_price,
            min_gap=settings.extreme_gap,
            contracts=settings.extreme_contracts,
        ),
        FavoriteMaker(
            enabled=settings.enable_favorite_maker,
            low=settings.favorite_low,
            high=settings.favorite_high,
            contracts=settings.favorite_contracts,
            require_model=settings.favorite_require_model,
        ),
    ]
    enabled = [item for item in strategies if item.enabled]
    schedule = schedule_from_api(
        data.fee_type,
        data.fee_multiplier,
        unknown_fee_type=settings.unknown_fee_type,
        taker_rate=settings.taker_rate,
        maker_rate=settings.maker_rate,
        model_quantum=settings.fee_model_quantum,
        balance_quantum=settings.fee_balance_quantum,
    )
    if not data.markets:
        record = decisions.write(
            {
                "ticker": None,
                "action": "observe",
                "reason": f"no_market:{data.discovery_source}",
                "fair_value_yes": None,
                "book": None,
                "inputs": {"discovery": data.discovery_source},
                "shard_ok": data.shard_ok,
                "shard_reason": data.shard_reason,
            }
        )
        return [record]

    day = data.now.astimezone(UTC).date().isoformat()
    daily_pnl = store.state.daily_pnl(day)
    killed = daily_pnl <= -settings.max_daily_loss_dollars

    for market in data.markets:
        ticker = str(market.get("ticker") or "")
        close_raw = market.get("close_time")
        if not ticker or not close_raw:
            continue
        close = _parse_time(str(close_raw))
        register_close(ticker, close)
        tau = (close - data.now).total_seconds()
        book = data.books.get(ticker) or Book(None, None)
        index = data.indexes.get(ticker)
        strike = _strike(market)
        spot = None
        sigma = None
        locked: list[float] | None = []
        fv = None
        fv_error = None
        if index is not None and index.latest() is not None:
            _ts, spot = index.latest()
            short = realized_sigma(
                slice_lookback(index, settings.vol_short_seconds) or index.prices,
                index.step_seconds,
            )
            long_prices = slice_lookback(index, settings.vol_long_seconds)
            long = realized_sigma(long_prices, index.step_seconds) if len(long_prices) >= 3 else None
            sigma = blend_sigma(
                short,
                long,
                short_weight=settings.vol_short_weight,
                long_weight=settings.vol_long_weight,
            )
        if strike is None:
            fv_error = "no_strike"
        elif spot is None or sigma is None:
            fv_error = "no_index"
        elif tau < 60 and (index is None or not index.per_second):
            fv_error = "final_minute_without_per_second_samples"
        else:
            if tau < 60:
                close_ms = int(close.timestamp() * 1000)
                now_ms = int(data.now.timestamp() * 1000)
                locked = index.locked_prices(close_ms, now_ms, settings.sample_count)
                if locked is None:
                    fv_error = "missing_locked_samples"
            if fv_error is None:
                computed = fair_value_yes(
                    spot=float(spot),
                    strike=float(strike),
                    tau_seconds=tau,
                    sigma_per_sqrt_second=float(sigma),
                    locked_samples=locked or [],
                    sample_count=settings.sample_count,
                    close_included=settings.close_included,
                    vol_scale=settings.vol_scale,
                    vol_floor=settings.vol_floor,
                )
                if computed is None:
                    fv_error = "fair_value_undefined"
                else:
                    fv = computed
        resting = _quotes_from_state(store, ticker)
        view = MarketView(
            ticker=ticker,
            book=book,
            fair_value_yes=None if fv is None else fv.probability_yes,
            tau_seconds=tau,
        )
        intents: list[QuoteIntent] = []
        if enabled and fv is not None and not killed and not data.feed_disconnected:
            for strategy in enabled:
                quote = strategy.propose(view)
                if quote is not None:
                    intents.append(quote)
        exposure = Exposure(
            contracts_on_market=store.state.contracts_on(ticker),
            contracts_total=store.state.contracts_total(),
            dollars_total=store.state.dollars_total(),
            daily_realized_pnl=daily_pnl,
            feed_age_seconds=data.feed_age_seconds,
        )
        blocked_window = trading_blocked(close, data.now, settings.cutoff)
        if data.feed_disconnected or killed or (
            data.feed_age_seconds is None
            or data.feed_age_seconds > settings.stale_feed_seconds
        ):
            if resting and (data.feed_disconnected or killed or data.feed_age_seconds is None or data.feed_age_seconds > settings.stale_feed_seconds):
                reason = "websocket_or_feed_down" if data.feed_disconnected else (
                    "daily_loss_kill" if killed else "stale_feed"
                )
                gateway.cancel_all(resting, reason=reason)
                for order in store.state.orders:
                    if order.ticker == ticker and order.status in ("resting", "paper"):
                        order.status = "cancelled"
                        order.remaining = "0"
                resting = []
        try:
            sync = gateway.sync(
                ticker=ticker,
                close_time=close,
                now=data.now,
                desired=intents,
                fair_value_yes=None if fv is None else fv.probability_yes,
                resting=resting,
                exposure=exposure,
            )
        except CutoffBlocked as exc:
            gateway.cancel_all(resting, reason="cutoff")
            sync = {"action": "cutoff", "reason": str(exc), "placed": [], "cancelled": [], "decreased": [], "kept": []}
        _apply_sync(store, ticker, sync, gateway.mode, data.now)
        if blocked_window:
            action = "cutoff"
            reason = sync.get("reason") or "cutoff"
        elif data.feed_disconnected:
            action = "cancel"
            reason = "feed_disconnect"
        elif killed:
            action = "kill"
            reason = "daily_loss_kill"
        elif not enabled:
            action = "observe"
            reason = "strategies_disabled"
        elif fv is None:
            action = "observe"
            reason = fv_error or "no_fair_value"
        else:
            action = sync.get("action") or "hold"
            reason = sync.get("reason") or action
        record = decisions.write(
            {
                "ticker": ticker,
                "tau_seconds": tau,
                "inputs": {
                    "spot": spot,
                    "strike": None if strike is None else str(strike),
                    "sigma_per_sqrt_second": sigma,
                    "locked_samples": locked if isinstance(locked, list) else None,
                    "sample_count": settings.sample_count,
                    "close_included": settings.close_included,
                    "vol_scale": settings.vol_scale,
                    "vol_floor": settings.vol_floor,
                    "fee_type": schedule.fee_type,
                    "maker_fee_applies": schedule.maker_fee_applies,
                    "discovery": data.discovery_source,
                    "fair_value": None if fv is None else fv.as_log_dict(),
                },
                "fair_value_yes": None if fv is None else fv.probability_yes,
                "book": book.as_log_dict(),
                "action": action,
                "reason": reason,
                "strategy": intents[0].strategy if intents else None,
                "intents": [
                    {
                        "strategy": item.strategy,
                        "contract_side": item.contract_side,
                        "action": item.action,
                        "limit_price": str(item.limit_price),
                        "count": str(item.count),
                        "reason": item.reason,
                    }
                    for item in intents
                ],
                "gateway": {
                    "action": sync.get("action"),
                    "placed": len(sync.get("placed") or []),
                    "cancelled": len(sync.get("cancelled") or []),
                    "decreased": len(sync.get("decreased") or []),
                },
                "shard_ok": data.shard_ok,
                "shard_reason": data.shard_reason,
                "mode": gateway.mode,
            }
        )
        records.append(record)
    store.save()
    return records


def _hosts(env: str) -> tuple[str, ...]:
    override = os.getenv("KALSHI_BASE_URL", "").strip()
    if override:
        return (override.rstrip("/"),)
    if env == "prod":
        return PROD_REST_HOSTS
    return DEMO_REST_HOSTS


def _fetch_cycle(settings: EngineSettings, public: PublicRead, now: datetime) -> CycleData:
    horizon = int(settings.discovery_horizon_seconds)
    now_ts = int(now.timestamp())
    try:
        primary = public.get(
            "/markets",
            {
                "series_ticker": settings.series,
                "min_close_ts": now_ts,
                "max_close_ts": now_ts + horizon,
                "limit": 20,
            },
        )
        markets_raw = primary.get("markets") or []
        source_markets = markets_raw
        events_raw = None
        if not markets_raw:
            events_raw = public.get(
                "/events",
                {
                    "series_ticker": settings.series,
                    "with_nested_markets": "true",
                    "limit": 20,
                },
            )
        markets, source = discover(
            source_markets,
            (events_raw or {}).get("events") if isinstance(events_raw, dict) else None,
            now=now,
            series=settings.series,
            horizon_seconds=settings.discovery_horizon_seconds,
        )
    except PublicReadError as exc:
        log.warning("market discovery failed: %s", exc)
        return CycleData(
            now=now,
            markets=[],
            books={},
            indexes={},
            fee_type=None,
            fee_multiplier=None,
            feed_age_seconds=None,
            feed_disconnected=True,
            discovery_source="error",
            shard_ok=False,
            shard_reason="not_checked",
        )
    fee_type = None
    fee_multiplier = None
    try:
        series_body = public.get(f"/series/{settings.series}")
        series = series_body.get("series", series_body)
        if isinstance(series, dict):
            fee_type = series.get("fee_type")
            fee_multiplier = series.get("fee_multiplier")
    except PublicReadError as exc:
        log.warning("series fee_type unread (%s); using FEE_TYPE_IF_UNKNOWN", exc)
    books: dict[str, Book] = {}
    indexes = {}
    feed_failed = False
    for market in markets:
        ticker = market.get("ticker")
        if not ticker:
            continue
        try:
            books[ticker] = parse_orderbook(public.get(f"/markets/{ticker}/orderbook"))
        except PublicReadError as exc:
            log.warning("orderbook fetch failed for %s: %s", ticker, exc)
            feed_failed = True
            books[ticker] = Book(None, None)
        event_ticker = market.get("event_ticker")
        if event_ticker:
            try:
                indexes[ticker] = extract_index_series(
                    public.get(f"/live_data/events/{event_ticker}")
                )
            except PublicReadError as exc:
                log.warning("index fetch failed for %s: %s", event_ticker, exc)
                indexes[ticker] = None
                feed_failed = True
    return CycleData(
        now=now,
        markets=markets,
        books=books,
        indexes=indexes,
        fee_type=str(fee_type) if fee_type else None,
        fee_multiplier=str(fee_multiplier) if fee_multiplier is not None else None,
        feed_age_seconds=0.0 if not feed_failed else None,
        feed_disconnected=feed_failed,
        discovery_source=source,
        shard_ok=False,
        shard_reason="pending",
    )


def _check_shard(settings: EngineSettings) -> tuple[bool, str, Decimal | None]:
    """Read the crypto shard. Never log key material. Fail closed."""
    try:
        from kalshi_client import KalshiClient
    except Exception as exc:
        log.warning("Kalshi client unavailable (%s); shard balance unknown", exc.__class__.__name__)
        return False, "shard_balance_unknown", None
    try:
        client = KalshiClient()
    except Exception as exc:
        log.warning("could not initialize Kalshi client (%s); not trading", exc.__class__.__name__)
        return False, "shard_balance_unknown", None
    payload = None
    scoped = False
    try:
        # This call is the startup check. Later reconciles go through the
        # balance bucket (about 1 request/second); the runner does not poll it.
        raw = client._sdk.get_subaccount_balances(_request_timeout=config.REQUEST_TIMEOUT_SECONDS)
        payload = raw.to_dict() if hasattr(raw, "to_dict") else raw
    except Exception as exc:
        log.warning("subaccount balances unread (%s); trying scoped balance", exc.__class__.__name__)
        try:
            raw = client._sdk.get_balance(
                exchange_index=settings.exchange_index,
                _request_timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
            payload = raw.to_dict() if hasattr(raw, "to_dict") else raw
            scoped = True
        except Exception as exc2:
            log.warning("scoped balance unread (%s)", exc2.__class__.__name__)
            return False, "shard_balance_unknown", None
    if not isinstance(payload, dict):
        payload = {"value": payload}
    balance = primary_shard_balance(
        payload,
        settings.exchange_index,
        subaccount=settings.subaccount,
        request_was_scoped=scoped,
    )
    ok, reason = allow_trading(balance, settings.min_shard_balance)
    if ok:
        log.info(
            "crypto shard exchange_index=%s balance=%s (minimum %s)",
            settings.exchange_index,
            balance,
            settings.min_shard_balance,
        )
    else:
        log.warning(
            "refusing live orders: %s (exchange_index=%s balance=%s)",
            reason,
            settings.exchange_index,
            balance,
        )
    return ok, reason, balance


def _order_group_id(settings: EngineSettings) -> str | None:
    if settings.paper:
        return None
    try:
        from kalshi_client import KalshiClient

        client = KalshiClient()
        raw = client._sdk.create_order_group(
            contracts_limit=int(settings.order_group_contracts_limit),
            exchange_index=settings.exchange_index,
            _request_timeout=config.REQUEST_TIMEOUT_SECONDS,
        )
        data = raw.to_dict() if hasattr(raw, "to_dict") else raw
        group_id = data.get("order_group_id") if isinstance(data, dict) else None
        if group_id:
            log.info("order group %s armed as a secondary 15s contract breaker", group_id)
        return group_id
    except Exception as exc:
        log.warning(
            "order group was not created (%s); client-side caps still apply",
            exc.__class__.__name__,
        )
        return None


def build_gateway(settings: EngineSettings, *, shard_ok: bool) -> OrderGateway:
    mode = "paper" if settings.paper or not shard_ok else "live"
    gateway = OrderGateway(
        sender=RecordingSender() if mode == "paper" else _LiveSender(settings),
        risk=RiskBook(
            RiskLimits(
                max_contracts_per_market=settings.max_contracts_per_market,
                max_contracts_total=settings.max_contracts_total,
                max_dollars_total=settings.max_dollars_total,
                max_daily_loss_dollars=settings.max_daily_loss_dollars,
                stale_feed_seconds=settings.stale_feed_seconds,
            )
        ),
        policy=settings.cutoff,
        reprice_threshold=settings.reprice_threshold,
        stale_order_seconds=settings.stale_order_seconds,
        exchange_index=settings.exchange_index,
        self_trade_prevention_type=settings.stp_type,
        order_group_id=None,
        mode=mode,
    )
    gateway.trading_enabled = True
    if mode == "live":
        gateway.order_group_id = _order_group_id(settings)
    return gateway


class _LiveSender:
    """SDK submit path. ``KalshiClient._submit_order`` re-checks the cutoff."""

    def __init__(self, settings: EngineSettings):
        from kalshi_client import KalshiClient

        self.settings = settings
        self.client = KalshiClient()

    def create(self, payload: dict, *, mode: str) -> dict:
        del mode
        result = self.client.submit_v2(payload)
        order = result.get("order") or {}
        return {"order_id": order.get("order_id"), "status": order.get("status")}

    def cancel(self, order_id: str, ticker: str) -> None:
        self.client.cancel_order(order_id)

    def decrease(self, order_id: str, ticker: str, reduce_to: Decimal) -> None:
        self.client.decrease_order(
            order_id,
            ticker,
            format(reduce_to, "f"),
            exchange_index=self.settings.exchange_index,
        )


def _configure_logging() -> None:
    logging.basicConfig(
        level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AstroTick demo/paper runner for KXBTC15M")
    parser.add_argument("--once", action="store_true", help="Run a single cycle and exit")
    parser.add_argument(
        "--paper",
        action="store_true",
        help="Do not send orders. Log the decision and any quote that would rest.",
    )
    args = parser.parse_args(argv)
    if args.paper:
        os.environ["PAPER"] = "1"
    _configure_logging()
    key_state = "set" if os.getenv("KALSHI_API_KEY_ID") else "missing"
    log.info("API key id is %s (value not logged)", key_state)
    try:
        config.validate()
    except OSError as exc:
        log.error("config rejected: %s", exc)
        return 2
    if config.KALSHI_ENV == "prod":
        log.critical("=" * 72)
        log.critical("PRODUCTION MODE. KALSHI_ENV=prod and KALSHI_ALLOW_PROD=1.")
        log.critical("Orders can spend real money. Hosts: %s", ", ".join(PROD_REST_HOSTS))
        log.critical("=" * 72)
    else:
        log.info("environment=demo (production requires KALSHI_ENV=prod and KALSHI_ALLOW_PROD=1)")
    try:
        settings = load_settings()
    except ValueError as exc:
        log.error("engine settings rejected: %s", exc)
        return 2
    if args.paper:
        settings = EngineSettings(**{**settings.__dict__, "paper": True})
    log.info(
        "mode=%s series=%s extreme_fade=%s favorite_maker=%s cutoff=%ss skew=%ss",
        "paper" if settings.paper else "live",
        settings.series,
        settings.enable_extreme_fade,
        settings.enable_favorite_maker,
        settings.cutoff.cutoff_seconds,
        settings.cutoff.clock_skew_margin_seconds,
    )
    if not settings.enable_extreme_fade and not settings.enable_favorite_maker:
        log.info("both candidate strategies are off; this cycle will log fair value and not quote")
    limiter = RateLimiter(
        read_per_second=settings.read_per_second,
        write_per_second=settings.write_per_second,
        order_cost=settings.order_token_cost,
        balance_per_second=settings.balance_per_second,
    )
    public = PublicRead(_hosts(config.KALSHI_ENV), limiter)
    store = StateStore(settings.state_path)
    store.load()
    decisions = DecisionLog(settings.decision_log_path)
    shard_ok, shard_reason, balance = _check_shard(settings)
    if balance is not None:
        store.set_balance(balance)
        store.save()
    if not settings.paper and not shard_ok:
        log.error("live mode refused: %s", shard_reason)
        decisions.write(
            {
                "ticker": None,
                "action": "refused",
                "reason": shard_reason,
                "fair_value_yes": None,
                "book": None,
                "inputs": {},
            }
        )
        return 2
    gateway = build_gateway(settings, shard_ok=shard_ok)
    now = datetime.now(UTC)
    try:
        data = _fetch_cycle(settings, public, now)
    except Exception as exc:
        log.error("cycle fetch failed: %s", exc)
        if settings.paper:
            decisions.write(
                {
                    "ticker": None,
                    "action": "observe",
                    "reason": f"fetch_failed:{exc.__class__.__name__}",
                    "fair_value_yes": None,
                    "book": None,
                    "inputs": {},
                }
            )
            return 0
        return 1
    data.shard_ok = shard_ok
    data.shard_reason = shard_reason
    while True:
        try:
            records = run_cycle(
                data,
                settings=settings,
                store=store,
                gateway=gateway,
                decisions=decisions,
            )
        except Exception as exc:
            log.error("cycle failed: %s", exc.__class__.__name__)
            if not settings.paper:
                return 1
            records = []
        log.info("cycle wrote %d decision line(s) to %s", len(records), decisions.path)
        if args.once:
            return 0
        time.sleep(max(1, int(getattr(config, "POLL_INTERVAL_SECONDS", 10))))
        now = datetime.now(UTC)
        try:
            data = _fetch_cycle(settings, public, now)
        except Exception as exc:
            log.error("cycle fetch failed: %s", exc.__class__.__name__)
            if not settings.paper:
                return 1
        data.shard_ok = shard_ok
        data.shard_reason = shard_reason


if __name__ == "__main__":
    sys.exit(main())
