"""Pluggable loaders. See ``backtest.schemas`` for the columns.

CSV is always supported. Parquet is supported when pandas is installed
(the fade-study files are parquet). Nothing here talks to Kalshi.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from backtest.features import asof_price


def _f(value: str | None) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "" or text.lower() in {"nan", "none", "null"}:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _ts(value: str | float | int | None) -> float | None:
    """Unix seconds. Milliseconds and ISO-8601 are accepted."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    else:
        text = str(value).strip()
        if text == "":
            return None
        try:
            number = float(text)
        except ValueError:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.timestamp()
    if number > 1e14:
        number /= 1_000_000.0
    elif number > 1e11:
        number /= 1000.0
    return number


def _iter_rows(path: Path):
    if path.suffix == ".parquet":
        frame = _read_parquet(path)
        for record in frame.to_dict(orient="records"):
            yield {str(k): record[k] for k in record}
        return
    with path.open(newline="", encoding="utf-8") as handle:
        yield from csv.DictReader(handle)


def _read_parquet(path: Path):
    try:
        import pandas as pd
    except ImportError as exc:
        raise RuntimeError(
            f"{path} is parquet. Install pandas and pyarrow to read it."
        ) from exc
    return pd.read_parquet(path)


def _files(root: Path, patterns: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for pattern in patterns:
        found.extend(sorted(root.glob(pattern)))
    # Stable, unique.
    seen: set[Path] = set()
    out: list[Path] = []
    for path in found:
        if path.is_file() and path not in seen:
            seen.add(path)
            out.append(path)
    return out


@dataclass
class MarketRow:
    ticker: str
    open_ts: float
    close_ts: float
    strike: float
    expiration_value: float | None
    result: str | None


@dataclass
class Candle:
    end_ts: float
    bid: float | None
    ask: float | None
    price: float | None


@dataclass
class TradePrint:
    ts: float
    yes_price: float
    qty: float


@dataclass
class BookSnap:
    ts: float
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_sz: float | None
    yes_ask_sz: float | None


@dataclass
class LadderSnap:
    ts: float
    yes_bids: list[tuple[float, float]]
    no_bids: list[tuple[float, float]]


@dataclass
class Dataset:
    """Everything the replay can see. Lookups are as-of and never look ahead."""

    markets: list[MarketRow]
    candles: dict[str, list[Candle]] = field(default_factory=dict)
    trades: dict[str, list[TradePrint]] = field(default_factory=dict)
    spot_ts: np.ndarray = field(default_factory=lambda: np.zeros(0))
    spot_px: np.ndarray = field(default_factory=lambda: np.zeros(0))
    books: dict[str, list[BookSnap]] = field(default_factory=dict)
    ladders: dict[str, list[LadderSnap]] = field(default_factory=dict)

    def spot(self, t: float, *, tolerance: float = 2.0) -> float | None:
        return asof_price(self.spot_ts, self.spot_px, t, tolerance=tolerance)

    def candle_asof(self, ticker: str, t: float) -> Candle | None:
        rows = self.candles.get(ticker) or []
        chosen = None
        for row in rows:
            if row.end_ts <= t:
                chosen = row
            else:
                break
        return chosen

    def book_asof(self, ticker: str, t: float) -> BookSnap | None:
        rows = self.books.get(ticker) or []
        chosen = None
        for row in rows:
            if row.ts <= t:
                chosen = row
            else:
                break
        return chosen

    def ladder_asof(self, ticker: str, t: float) -> LadderSnap | None:
        rows = self.ladders.get(ticker) or []
        chosen = None
        for row in rows:
            if row.ts <= t:
                chosen = row
            else:
                break
        return chosen


class MarketAdapter:
    patterns = ("markets.csv", "markets.parquet")

    def load(self, root: Path) -> list[MarketRow]:
        rows: list[MarketRow] = []
        for path in _files(root, self.patterns):
            for raw in _iter_rows(path):
                ticker = str(raw.get("ticker") or "").strip()
                open_ts = _ts(raw.get("open_time"))
                close_ts = _ts(raw.get("close_time"))
                strike = _f(raw.get("floor_strike"))
                if not ticker or open_ts is None or close_ts is None or strike is None:
                    continue
                result = raw.get("result")
                result_s = str(result).strip().lower() if result not in (None, "") else None
                if result_s in {"nan", "none"}:
                    result_s = None
                rows.append(
                    MarketRow(
                        ticker=ticker,
                        open_ts=float(open_ts),
                        close_ts=float(close_ts),
                        strike=float(strike),
                        expiration_value=_f(raw.get("expiration_value")),
                        result=result_s,
                    )
                )
        rows.sort(key=lambda item: (item.close_ts, item.ticker))
        return rows


class CandleAdapter:
    """1-minute Kalshi candles. ``end_ts`` is the period end (known at that time)."""

    patterns = ("candles.csv", "candles.parquet", "candles/*.csv", "candles/*.parquet")

    def load(self, root: Path) -> dict[str, list[Candle]]:
        grouped: dict[str, list[Candle]] = {}
        for path in _files(root, self.patterns):
            for raw in _iter_rows(path):
                ticker = str(raw.get("ticker") or "").strip()
                end_ts = _ts(raw.get("end_ts", raw.get("end_period_ts")))
                if not ticker or end_ts is None:
                    continue
                grouped.setdefault(ticker, []).append(
                    Candle(
                        end_ts=float(end_ts),
                        bid=_f(raw.get("bid_c")),
                        ask=_f(raw.get("ask_c")),
                        price=_f(raw.get("p_c")),
                    )
                )
        for rows in grouped.values():
            rows.sort(key=lambda item: item.end_ts)
        return grouped


class TradeAdapter:
    """Trade prints. ``p`` / ``yes_price`` is the YES price in dollars."""

    patterns = ("trades.csv", "trades.parquet", "trades/*.csv", "trades/*.parquet")

    def load(self, root: Path) -> dict[str, list[TradePrint]]:
        grouped: dict[str, list[TradePrint]] = {}
        for path in _files(root, self.patterns):
            ticker_from_name = path.stem if path.parent.name == "trades" else ""
            for raw in _iter_rows(path):
                ticker = str(raw.get("ticker") or ticker_from_name).strip()
                ts = _ts(raw.get("ts", raw.get("created_time")))
                price = _f(raw.get("p", raw.get("yes_price", raw.get("yes_price_dollars"))))
                qty = _f(raw.get("q", raw.get("count", raw.get("count_fp"))))
                if not ticker or ts is None or price is None or qty is None or qty <= 0:
                    continue
                grouped.setdefault(ticker, []).append(
                    TradePrint(ts=float(ts), yes_price=float(price), qty=float(qty))
                )
        for rows in grouped.values():
            rows.sort(key=lambda item: item.ts)
        return grouped


class SpotAdapter:
    """BTC 1-second proxy. Binance columns ``ts`` and ``c`` (kline close)."""

    patterns = (
        "spot.csv",
        "spot.parquet",
        "binance/*.csv",
        "binance/*.parquet",
        "btc1s_*.parquet",
        "btc1s_*.csv",
    )

    def load(self, root: Path) -> tuple[np.ndarray, np.ndarray]:
        stamps: list[float] = []
        prices: list[float] = []
        for path in _files(root, self.patterns):
            for raw in _iter_rows(path):
                ts = _ts(raw.get("ts"))
                px = _f(raw.get("c", raw.get("close")))
                if ts is None or px is None or px <= 0:
                    continue
                stamps.append(float(ts))
                prices.append(float(px))
        if not stamps:
            return np.zeros(0), np.zeros(0)
        order = np.argsort(np.asarray(stamps))
        ts_arr = np.asarray(stamps, dtype=np.float64)[order]
        px_arr = np.asarray(prices, dtype=np.float64)[order]
        # Drop exact duplicate timestamps, keeping the last print at that second.
        if len(ts_arr) > 1:
            keep = np.ones(len(ts_arr), dtype=bool)
            keep[:-1] = ts_arr[:-1] != ts_arr[1:]
            ts_arr = ts_arr[keep]
            px_arr = px_arr[keep]
        return ts_arr, px_arr


class BookAdapter:
    """Optional top-of-book snapshots (~5s). Used only when the file covers the window."""

    patterns = ("book.csv", "book_*.csv", "books/*.csv")

    def load(self, root: Path) -> dict[str, list[BookSnap]]:
        grouped: dict[str, list[BookSnap]] = {}
        for path in _files(root, self.patterns):
            for raw in _iter_rows(path):
                ticker = str(raw.get("ticker") or "").strip()
                ts = _ts(raw.get("ts_utc", raw.get("ts")))
                if not ticker or ts is None:
                    continue
                grouped.setdefault(ticker, []).append(
                    BookSnap(
                        ts=float(ts),
                        yes_bid=_f(raw.get("yes_bid")),
                        yes_ask=_f(raw.get("yes_ask")),
                        yes_bid_sz=_f(raw.get("yes_bid_sz_top")),
                        yes_ask_sz=_f(raw.get("yes_ask_sz_top")),
                    )
                )
        for rows in grouped.values():
            rows.sort(key=lambda item: item.ts)
        return grouped


class LadderAdapter:
    """Optional full ladder, one JSON object per line, same timestamps as the book CSV."""

    patterns = ("ladder_*.jsonl", "ladders/*.jsonl")

    def load(self, root: Path) -> dict[str, list[LadderSnap]]:
        grouped: dict[str, list[LadderSnap]] = {}
        for path in _files(root, self.patterns):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    raw = json.loads(line)
                    ticker = str(raw.get("ticker") or "").strip()
                    ts = _ts(raw.get("ts_utc", raw.get("ts")))
                    if not ticker or ts is None:
                        continue
                    grouped.setdefault(ticker, []).append(
                        LadderSnap(
                            ts=float(ts),
                            yes_bids=_levels(raw.get("yes_bids")),
                            no_bids=_levels(raw.get("no_bids")),
                        )
                    )
        for rows in grouped.values():
            rows.sort(key=lambda item: item.ts)
        return grouped


def _levels(raw) -> list[tuple[float, float]]:
    out: list[tuple[float, float]] = []
    for row in raw or []:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            continue
        price, size = _f(row[0]), _f(row[1])
        if price is None or size is None or size <= 0:
            continue
        out.append((float(price), float(size)))
    return out


def load_dataset(root: Path) -> Dataset:
    markets = MarketAdapter().load(root)
    if not markets:
        raise FileNotFoundError(
            f"no markets.csv or markets.parquet with ticker/open/close/strike in {root}"
        )
    spot_ts, spot_px = SpotAdapter().load(root)
    if len(spot_ts) == 0:
        raise FileNotFoundError(
            f"no spot series (spot.csv or binance/btc1s_*.parquet with ts,c) in {root}"
        )
    return Dataset(
        markets=markets,
        candles=CandleAdapter().load(root),
        trades=TradeAdapter().load(root),
        spot_ts=spot_ts,
        spot_px=spot_px,
        books=BookAdapter().load(root),
        ladders=LadderAdapter().load(root),
    )
