"""Environment settings for the demo-first runner.

Anything the spec marks UNVERIFIED (settlement sampling, fee rounding, rate
limits for the account's real tier, listing horizon, which 200ms BRTI tick)
is read from the environment and has a documented default. The T-50s floor
is not one of those: ``CUTOFF_SECONDS`` cannot go below 50.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal

from engine.cutoff import CUTOFF_FLOOR_SECONDS, CutoffPolicy


def _flag(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _decimal(name: str, default: str) -> Decimal:
    raw = os.getenv(name, default)
    try:
        return Decimal(str(raw))
    except Exception as exc:
        raise ValueError(f"{name}={raw!r} is not a decimal") from exc


def _float(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}={raw!r} is not a number") from exc


def _int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name}={raw!r} is not an integer") from exc


@dataclass(frozen=True)
class EngineSettings:
    series: str = "KXBTC15M"
    exchange_index: int = 2
    cutoff: CutoffPolicy = CutoffPolicy()
    reprice_threshold: Decimal = Decimal("0.02")
    stale_order_seconds: float = 300.0
    stale_feed_seconds: float = 15.0
    max_contracts_per_market: Decimal = Decimal("1")
    max_contracts_total: Decimal = Decimal("2")
    max_dollars_total: Decimal = Decimal("25")
    max_daily_loss_dollars: Decimal = Decimal("10")
    order_group_contracts_limit: int = 2
    enable_extreme_fade: bool = False
    extreme_price: Decimal = Decimal("0.85")
    extreme_gap: Decimal = Decimal("0.10")
    extreme_contracts: Decimal = Decimal("1")
    enable_favorite_maker: bool = False
    favorite_low: Decimal = Decimal("0.70")
    favorite_high: Decimal = Decimal("0.90")
    favorite_contracts: Decimal = Decimal("1")
    favorite_require_model: bool = False
    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    fee_model_quantum: Decimal = Decimal("0.000001")
    fee_balance_quantum: Decimal = Decimal("0.0001")
    unknown_fee_type: str = "quadratic_with_maker_fees"
    sample_count: int = 60
    # UNVERIFIED: official settlement samples vs the WS (close-60s, close] window.
    close_included: bool = True
    vol_short_seconds: float = 900.0
    vol_long_seconds: float = 3600.0
    vol_short_weight: float = 0.7
    vol_long_weight: float = 0.3
    vol_scale: float = 1.0
    vol_floor: float = 0.0
    # UNVERIFIED as a listing policy; 30 minutes covers the open window and the next.
    discovery_horizon_seconds: float = 1800.0
    read_per_second: float = 200.0
    write_per_second: float = 100.0
    order_token_cost: int = 10
    balance_per_second: float = 1.0
    balance_reconcile_seconds: float = 30.0
    stp_type: str = "taker_at_cross"
    min_shard_balance: Decimal = Decimal("1")
    state_path: str = "engine_state.json"
    decision_log_path: str = "logs/decisions.jsonl"
    paper: bool = True
    subaccount: int = 0

    def validate(self) -> None:
        if self.series != "KXBTC15M" and not self.series.replace("_", "").isalnum():
            raise ValueError(f"series ticker {self.series!r} is not usable")
        if self.exchange_index < 0:
            raise ValueError("exchange_index must be >= 0")
        if self.reprice_threshold <= 0 or self.reprice_threshold > 1:
            raise ValueError("reprice_threshold must be in (0, 1]")
        if self.sample_count < 1:
            raise ValueError("sample_count must be >= 1")
        if not (Decimal("0") < self.extreme_price < Decimal("1")):
            raise ValueError("extreme_price must be in (0, 1)")
        if self.extreme_gap <= 0:
            raise ValueError("extreme_gap must be > 0")
        if not (Decimal("0") < self.favorite_low < self.favorite_high < Decimal("1")):
            raise ValueError("favorite band must satisfy 0 < low < high < 1")
        if self.stp_type not in ("taker_at_cross", "maker"):
            raise ValueError("stp_type must be taker_at_cross or maker")
        if self.min_shard_balance < 0:
            raise ValueError("min_shard_balance must be >= 0")
        if self.order_group_contracts_limit < 1:
            raise ValueError("order_group_contracts_limit must be >= 1")
        if self.balance_reconcile_seconds < 1:
            raise ValueError("balance_reconcile_seconds must be >= 1 (balance endpoint is ~1/s)")
        if self.cutoff.cutoff_seconds < CUTOFF_FLOOR_SECONDS:
            raise ValueError("cutoff below the 50s floor")


def load_settings() -> EngineSettings:
    paper_env = os.getenv("PAPER")
    if paper_env is None:
        paper = not _flag("KALSHI_TRADING_LIVE", "0")
    else:
        paper = _flag("PAPER", "1")
    # Live orders also require the explicit trading gate, even on demo.
    if _flag("KALSHI_TRADING_LIVE", "0") and paper_env is not None and not _flag("PAPER", "1"):
        paper = False
    if not _flag("KALSHI_TRADING_LIVE", "0"):
        paper = True
    settings = EngineSettings(
        series=os.getenv("BTC_SERIES_TICKER", "KXBTC15M").strip() or "KXBTC15M",
        exchange_index=_int("CRYPTO_EXCHANGE_INDEX", "2"),
        cutoff=CutoffPolicy(
            cutoff_seconds=max(CUTOFF_FLOOR_SECONDS, _float("CUTOFF_SECONDS", "50")),
            clock_skew_margin_seconds=max(0.0, _float("CUTOFF_CLOCK_SKEW_SECONDS", "2")),
        ),
        reprice_threshold=_decimal("REPRICE_THRESHOLD", "0.02"),
        stale_order_seconds=_float("STALE_ORDER_SECONDS", "300"),
        stale_feed_seconds=_float("STALE_FEED_SECONDS", "15"),
        max_contracts_per_market=_decimal("MAX_CONTRACTS_PER_MARKET", "1"),
        max_contracts_total=_decimal("MAX_CONTRACTS_TOTAL", "2"),
        max_dollars_total=_decimal("MAX_DOLLARS_TOTAL", "25"),
        max_daily_loss_dollars=_decimal("MAX_DAILY_LOSS_DOLLARS", "10"),
        order_group_contracts_limit=_int("ORDER_GROUP_CONTRACTS_LIMIT", "2"),
        enable_extreme_fade=_flag("ENABLE_EXTREME_FADE", "0"),
        extreme_price=_decimal("EXTREME_FADE_PRICE", "0.85"),
        extreme_gap=_decimal("EXTREME_FADE_GAP", "0.10"),
        extreme_contracts=_decimal("EXTREME_FADE_CONTRACTS", "1"),
        enable_favorite_maker=_flag("ENABLE_FAVORITE_MAKER", "0"),
        favorite_low=_decimal("FAVORITE_BID_LOW", "0.70"),
        favorite_high=_decimal("FAVORITE_BID_HIGH", "0.90"),
        favorite_contracts=_decimal("FAVORITE_CONTRACTS", "1"),
        favorite_require_model=_flag("FAVORITE_REQUIRE_MODEL", "0"),
        taker_rate=_decimal("TAKER_FEE_RATE", "0.07"),
        maker_rate=_decimal("MAKER_FEE_RATE", "0.0175"),
        fee_model_quantum=_decimal("FEE_MODEL_QUANTUM", "0.000001"),
        fee_balance_quantum=_decimal("FEE_BALANCE_QUANTUM", "0.0001"),
        unknown_fee_type=os.getenv("FEE_TYPE_IF_UNKNOWN", "quadratic_with_maker_fees"),
        sample_count=_int("SETTLEMENT_SAMPLE_COUNT", "60"),
        close_included=_flag("SETTLEMENT_CLOSE_INCLUDED", "1"),
        vol_short_seconds=_float("VOL_SHORT_SECONDS", "900"),
        vol_long_seconds=_float("VOL_LONG_SECONDS", "3600"),
        vol_short_weight=_float("VOL_SHORT_WEIGHT", "0.7"),
        vol_long_weight=_float("VOL_LONG_WEIGHT", "0.3"),
        vol_scale=_float("VOL_SCALE", "1"),
        vol_floor=_float("VOL_FLOOR", "0"),
        discovery_horizon_seconds=_float("DISCOVERY_HORIZON_SECONDS", "1800"),
        read_per_second=_float("READ_TOKENS_PER_SECOND", "200"),
        write_per_second=_float("WRITE_TOKENS_PER_SECOND", "100"),
        order_token_cost=_int("ORDER_TOKEN_COST", "10"),
        balance_per_second=_float("BALANCE_REQUESTS_PER_SECOND", "1"),
        balance_reconcile_seconds=_float("BALANCE_RECONCILE_SECONDS", "30"),
        stp_type=os.getenv("SELF_TRADE_PREVENTION", "taker_at_cross"),
        min_shard_balance=_decimal("MIN_SHARD_BALANCE_DOLLARS", "1"),
        state_path=os.getenv("STATE_PATH", "engine_state.json"),
        decision_log_path=os.getenv("DECISION_LOG_FILE", "logs/decisions.jsonl"),
        paper=paper,
        subaccount=_int("KALSHI_SUBACCOUNT", "0"),
    )
    settings.validate()
    return settings
