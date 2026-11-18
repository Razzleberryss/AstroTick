"""Pluggable loaders. See ``backtest.schemas`` for the columns.

CSV is always supported. Parquet is supported when pandas is installed
(the fade-study files are parquet). Nothing here talks to Kalshi.
"""

from __future__ import annotations

import csv
import json
from bisect import bisect_right
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
class TradeColumns:
    """One ticker's prints, sorted by time. Arrays stay columnar so millions of rows fit."""

    ts: np.ndarray
    yes_price: np.ndarray
    qty: np.ndarray

    def between(self, lo: float, hi: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Prints with ``lo < ts < hi``. ``hi`` is exclusive."""
        if len(self.ts) == 0:
            empty = np.zeros(0)
            return empty, empty, empty
        start = int(np.searchsorted(self.ts, lo, side="right"))
        stop = int(np.searchsorted(self.ts, hi, side="left"))
        return self.ts[start:stop], self.yes_price[start:stop], self.qty[start:stop]


@dataclass
class BookSnap:
    ts: float
    yes_bid: float | None
    yes_ask: float | None
    yes_bid_sz: float | None
    yes_ask_sz: float | None
    no_bid: float | None = None


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
    trades: dict[str, TradeColumns] = field(default_factory=dict)
    spot_ts: np.ndarray = field(default_factory=lambda: np.zeros(0))
    spot_px: np.ndarray = field(default_factory=lambda: np.zeros(0))
    books: dict[str, list[BookSnap]] = field(default_factory=dict)
    ladders: dict[str, list[LadderSnap]] = field(default_factory=dict)

    def spot(self, t: float, *, tolerance: float = 2.0) -> float | None:
        return asof_price(self.spot_ts, self.spot_px, t, tolerance=tolerance)

    def candle_asof(self, ticker: str, t: float) -> Candle | None:
        rows = self.candles.get(ticker) or []
        index = bisect_right(rows, t, key=lambda row: row.end_ts) - 1
        if index < 0:
            return None
        return rows[index]

    def book_asof(self, ticker: str, t: float) -> BookSnap | None:
        rows = self.books.get(ticker) or []
        index = bisect_right(rows, t, key=lambda row: row.ts) - 1
        if index < 0:
            return None
        return rows[index]

    def ladder_asof(self, ticker: str, t: float) -> LadderSnap | None:
        rows = self.ladders.get(ticker) or []
        index = bisect_right(rows, t, key=lambda row: row.ts) - 1
        if index < 0:
            return None
        return rows[index]


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

    def load(
        self,
        root: Path,
        *,
        tickers: set[str] | None = None,
        start_ts: float | None = None,
        until_ts: float | None = None,
    ) -> dict[str, list[Candle]]:
        grouped: dict[str, list[Candle]] = {}
        for path in _files(root, self.patterns):
            for raw in _iter_rows(path):
                ticker = str(raw.get("ticker") or "").strip()
                end_ts = _ts(raw.get("end_ts", raw.get("end_period_ts")))
                if not ticker or end_ts is None:
                    continue
                if tickers is not None and ticker not in tickers:
                    continue
                if start_ts is not None and float(end_ts) < start_ts:
                    continue
                if until_ts is not None and float(end_ts) >= until_ts:
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
    """Trade prints. ``p`` / ``yes_price`` is the YES price in dollars.

    Rows are accumulated as numpy columns in batches so a multi-million-row
    file is never a list of dicts.
    """

    patterns = ("trades.csv", "trades.parquet", "trades/*.csv", "trades/*.parquet")

    def load(
        self,
        root: Path,
        *,
        tickers: set[str] | None = None,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> dict[str, TradeColumns]:
        parts: dict[str, list[tuple[np.ndarray, np.ndarray, np.ndarray]]] = {}
        for path in _files(root, self.patterns):
            if not _path_overlaps(path, start_ts, end_ts):
                continue
            ticker_from_name = path.stem if path.parent.name == "trades" else ""
            if tickers is not None and ticker_from_name and ticker_from_name not in tickers:
                continue
            for ticker, ts, price, qty in _iter_trades(path, ticker_from_name, start_ts, end_ts):
                if tickers is not None and ticker not in tickers:
                    continue
                parts.setdefault(ticker, []).append((ts, price, qty))
        out: dict[str, TradeColumns] = {}
        for ticker, batches in parts.items():
            ts = np.concatenate([item[0] for item in batches])
            price = np.concatenate([item[1] for item in batches])
            qty = np.concatenate([item[2] for item in batches])
            order = np.argsort(ts, kind="mergesort")
            out[ticker] = TradeColumns(ts=ts[order], yes_price=price[order], qty=qty[order])
        return out


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

    def load(
        self,
        root: Path,
        *,
        start_ts: float | None = None,
        end_ts: float | None = None,
    ) -> tuple[np.ndarray, np.ndarray]:
        stamps: list[np.ndarray] = []
        prices: list[np.ndarray] = []
        for path in _files(root, self.patterns):
            if not _path_overlaps(path, start_ts, end_ts):
                continue
            for ts, px in _iter_spot(path, start_ts, end_ts):
                # ``ts`` is the kline open. The close is known one second later.
                stamps.append(ts + 1.0)
                prices.append(px)
        if not stamps:
            return np.zeros(0), np.zeros(0)
        ts_arr = np.concatenate(stamps)
        px_arr = np.concatenate(prices)
        order = np.argsort(ts_arr, kind="mergesort")
        ts_arr = ts_arr[order]
        px_arr = px_arr[order]
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

    def load(
        self,
        root: Path,
        *,
        tickers: set[str] | None = None,
        start_ts: float | None = None,
        until_ts: float | None = None,
    ) -> dict[str, list[BookSnap]]:
        grouped: dict[str, list[BookSnap]] = {}
        for path in _files(root, self.patterns):
            if not _path_overlaps(path, start_ts, until_ts):
                continue
            for raw in _iter_rows(path):
                ticker = str(raw.get("ticker") or "").strip()
                ts = _ts(raw.get("ts_utc", raw.get("ts")))
                if not ticker or ts is None:
                    continue
                if tickers is not None and ticker not in tickers:
                    continue
                if start_ts is not None and float(ts) < start_ts:
                    continue
                if until_ts is not None and float(ts) >= until_ts:
                    continue
                grouped.setdefault(ticker, []).append(
                    BookSnap(
                        ts=float(ts),
                        yes_bid=_f(raw.get("yes_bid")),
                        yes_ask=_f(raw.get("yes_ask")),
                        yes_bid_sz=_f(raw.get("yes_bid_sz_top")),
                        yes_ask_sz=_f(raw.get("yes_ask_sz_top")),
                        no_bid=_f(raw.get("no_bid")),
                    )
                )
        for rows in grouped.values():
            rows.sort(key=lambda item: item.ts)
        return grouped


class LadderAdapter:
    """Optional full ladder, one JSON object per line, same timestamps as the book CSV."""

    patterns = ("ladder_*.jsonl", "ladders/*.jsonl")

    def load(
        self,
        root: Path,
        *,
        tickers: set[str] | None = None,
        start_ts: float | None = None,
        until_ts: float | None = None,
    ) -> dict[str, list[LadderSnap]]:
        grouped: dict[str, list[LadderSnap]] = {}
        for path in _files(root, self.patterns):
            if not _path_overlaps(path, start_ts, until_ts):
                continue
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
                    if tickers is not None and ticker not in tickers:
                        continue
                    if start_ts is not None and float(ts) < start_ts:
                        continue
                    if until_ts is not None and float(ts) >= until_ts:
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


def load_dataset(
    root: Path,
    *,
    start_ts: float | None = None,
    end_ts: float | None = None,
    spot_pad: float = 7200.0,
) -> Dataset:
    """Load one time window. ``start_ts``/``end_ts`` filter market close times.

    Spot is padded backward by ``spot_pad`` so realized vol and the open-minute
    basis for the first market in the window are still causal. Pass a window
    to avoid holding the full Binance file and every trade print at once.
    """
    markets = MarketAdapter().load(root)
    if start_ts is not None:
        markets = [market for market in markets if market.close_ts >= start_ts]
    if end_ts is not None:
        markets = [market for market in markets if market.close_ts < end_ts]
    if not markets:
        if start_ts is None and end_ts is None:
            raise FileNotFoundError(
                f"no markets.csv or markets.parquet with ticker/open/close/strike in {root}"
            )
        return Dataset(markets=[])
    tickers = {market.ticker for market in markets}
    spot_lo = min(market.open_ts for market in markets) - spot_pad
    spot_hi = max(market.close_ts for market in markets) + 1.0
    spot_ts, spot_px = SpotAdapter().load(root, start_ts=spot_lo, end_ts=spot_hi)
    if len(spot_ts) == 0:
        raise FileNotFoundError(
            f"no spot series (spot.csv or binance/btc1s_*.parquet with ts,c) in {root}"
        )
    trade_lo = min(market.open_ts for market in markets) - 1.0
    trade_hi = max(market.close_ts for market in markets) + 1.0
    # Keep one quote from just before the window so the first decision still has a book.
    quote_lo = trade_lo - 60.0
    return Dataset(
        markets=markets,
        candles=CandleAdapter().load(root, tickers=tickers, start_ts=quote_lo, until_ts=trade_hi),
        trades=TradeAdapter().load(root, tickers=tickers, start_ts=trade_lo, end_ts=trade_hi),
        spot_ts=spot_ts,
        spot_px=spot_px,
        books=BookAdapter().load(root, tickers=tickers, start_ts=quote_lo, until_ts=trade_hi),
        ladders=LadderAdapter().load(root, tickers=tickers, start_ts=quote_lo, until_ts=trade_hi),
    )


def _path_overlaps(path: Path, start_ts: float | None, end_ts: float | None) -> bool:
    """Skip dated files that sit entirely outside the window. Undated files are kept."""
    if start_ts is None and end_ts is None:
        return True
    name = path.name
    day = None
    for token in name.replace(".", "_").split("_"):
        if len(token) == 10 and token[4] == "-" and token[7] == "-":
            try:
                day = datetime.fromisoformat(token).replace(tzinfo=UTC).timestamp()
            except ValueError:
                day = None
            break
        if len(token) == 7 and token[4] == "-":
            try:
                day = datetime.fromisoformat(token + "-01").replace(tzinfo=UTC).timestamp()
                span = 32 * 86400.0
            except ValueError:
                day = None
            else:
                if start_ts is not None and day + span < start_ts:
                    return False
                if end_ts is not None and day >= end_ts:
                    return False
                return True
    if day is None:
        return True
    if start_ts is not None and day + 86400.0 < start_ts:
        return False
    if end_ts is not None and day >= end_ts:
        return False
    return True


def _as_float_array(values) -> np.ndarray:
    array = np.asarray(values, dtype=np.float64)
    if array.size and np.nanmax(array) > 1e14:
        array = array / 1_000_000.0
    elif array.size and np.nanmax(array) > 1e11:
        array = array / 1000.0
    return array


def _iter_spot(path: Path, start_ts: float | None, end_ts: float | None):
    """Yield ``(open_ts, close)`` batches. The caller shifts the timestamp."""
    yield from _iter_numeric_pairs(path, "ts", ("c", "close"), start_ts, end_ts)


def _iter_numeric_pairs(path: Path, ts_name: str, value_names: tuple[str, ...], start_ts, end_ts):
    if path.suffix == ".parquet":
        yield from _parquet_pairs(path, ts_name, value_names, start_ts, end_ts)
        return
    try:
        import pandas as pd
    except ImportError:
        pd = None
    if pd is not None:
        wanted = {ts_name, *value_names}
        for frame in pd.read_csv(path, usecols=lambda col: str(col) in wanted, chunksize=500_000):
            if ts_name not in frame.columns:
                continue
            value_col = next((name for name in value_names if name in frame.columns), None)
            if value_col is None:
                continue
            ts = _as_float_array(frame[ts_name].to_numpy())
            px = np.asarray(frame[value_col].to_numpy(), dtype=np.float64)
            mask = np.isfinite(ts) & np.isfinite(px) & (px > 0)
            if start_ts is not None:
                mask &= ts + 1.0 >= start_ts
            if end_ts is not None:
                mask &= ts + 1.0 < end_ts
            if mask.any():
                yield ts[mask], px[mask]
        return
    ts_list: list[float] = []
    px_list: list[float] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            ts = _ts(raw.get(ts_name))
            px = None
            for name in value_names:
                px = _f(raw.get(name))
                if px is not None:
                    break
            if ts is None or px is None or px <= 0:
                continue
            known = ts + 1.0
            if start_ts is not None and known < start_ts:
                continue
            if end_ts is not None and known >= end_ts:
                continue
            ts_list.append(ts)
            px_list.append(px)
    if ts_list:
        yield np.asarray(ts_list), np.asarray(px_list)


def _parquet_pairs(path: Path, ts_name: str, value_names: tuple[str, ...], start_ts, end_ts):
    import pyarrow.parquet as pq

    schema_names = set(pq.ParquetFile(path).schema_arrow.names)
    value_col = next((name for name in value_names if name in schema_names), None)
    if ts_name not in schema_names or value_col is None:
        return
    for batch in pq.ParquetFile(path).iter_batches(columns=[ts_name, value_col], batch_size=500_000):
        ts = _as_float_array(batch.column(0).to_numpy(zero_copy_only=False))
        px = np.asarray(batch.column(1).to_numpy(zero_copy_only=False), dtype=np.float64)
        mask = np.isfinite(ts) & np.isfinite(px) & (px > 0)
        if start_ts is not None:
            mask &= ts + 1.0 >= start_ts
        if end_ts is not None:
            mask &= ts + 1.0 < end_ts
        if mask.any():
            yield ts[mask], px[mask]


def _iter_trades(path: Path, ticker_from_name: str, start_ts, end_ts):
    if path.suffix == ".parquet":
        yield from _parquet_trades(path, ticker_from_name, start_ts, end_ts)
        return
    try:
        import pandas as pd
    except ImportError:
        pd = None
    if pd is not None:
        wanted = {"ticker", "ts", "created_time", "p", "yes_price", "yes_price_dollars", "q", "count", "count_fp"}
        for frame in pd.read_csv(path, usecols=lambda col: str(col) in wanted, chunksize=500_000):
            yield from _frame_trades(frame, ticker_from_name, start_ts, end_ts)
        return
    buckets: dict[str, tuple[list[float], list[float], list[float]]] = {}
    with path.open(newline="", encoding="utf-8") as handle:
        for raw in csv.DictReader(handle):
            ticker = str(raw.get("ticker") or ticker_from_name).strip()
            ts = _ts(raw.get("ts", raw.get("created_time")))
            price = _f(raw.get("p", raw.get("yes_price", raw.get("yes_price_dollars"))))
            qty = _f(raw.get("q", raw.get("count", raw.get("count_fp"))))
            if not ticker or ts is None or price is None or qty is None or qty <= 0:
                continue
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts >= end_ts:
                continue
            slot = buckets.setdefault(ticker, ([], [], []))
            slot[0].append(ts)
            slot[1].append(price)
            slot[2].append(qty)
    for ticker, (ts_list, price_list, qty_list) in buckets.items():
        yield ticker, np.asarray(ts_list), np.asarray(price_list), np.asarray(qty_list)


def _frame_trades(frame, ticker_from_name: str, start_ts, end_ts):
    ts_col = "ts" if "ts" in frame.columns else "created_time" if "created_time" in frame.columns else None
    price_col = next((name for name in ("p", "yes_price", "yes_price_dollars") if name in frame.columns), None)
    qty_col = next((name for name in ("q", "count", "count_fp") if name in frame.columns), None)
    if ts_col is None or price_col is None or qty_col is None:
        return
    ts = _as_float_array(frame[ts_col].to_numpy())
    price = np.asarray(frame[price_col].to_numpy(), dtype=np.float64)
    qty = np.asarray(frame[qty_col].to_numpy(), dtype=np.float64)
    if "ticker" in frame.columns:
        tickers = frame["ticker"].astype(str).to_numpy()
    else:
        tickers = np.full(len(frame), ticker_from_name)
    mask = np.isfinite(ts) & np.isfinite(price) & np.isfinite(qty) & (qty > 0)
    if start_ts is not None:
        mask &= ts >= start_ts
    if end_ts is not None:
        mask &= ts < end_ts
    if not mask.any():
        return
    ts, price, qty, tickers = ts[mask], price[mask], qty[mask], tickers[mask]
    for ticker in np.unique(tickers):
        take = tickers == ticker
        name = str(ticker).strip()
        if not name or name == "nan":
            continue
        yield name, ts[take], price[take], qty[take]


def _parquet_trades(path: Path, ticker_from_name: str, start_ts, end_ts):
    import pyarrow.parquet as pq

    names = set(pq.ParquetFile(path).schema_arrow.names)
    ts_name = "ts" if "ts" in names else "created_time" if "created_time" in names else None
    price_name = next((name for name in ("p", "yes_price", "yes_price_dollars") if name in names), None)
    qty_name = next((name for name in ("q", "count", "count_fp") if name in names), None)
    if ts_name is None or price_name is None or qty_name is None:
        return
    columns = [ts_name, price_name, qty_name]
    if "ticker" in names:
        columns.append("ticker")
    for batch in pq.ParquetFile(path).iter_batches(columns=columns, batch_size=500_000):
        frame = batch.to_pandas()
        yield from _frame_trades(frame, ticker_from_name, start_ts, end_ts)
