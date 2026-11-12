"""Index prints used as the spot input and the locked settlement samples.

The live_data payload shape has varied. This parser accepts a 1-second
timeseries when it can find one, and otherwise the 1-minute candles the
existing index feed already understands. Minute candles cannot lock the
final-minute samples, so the caller is told the spacing and can refuse to
price inside that minute.
"""

from __future__ import annotations

from dataclasses import dataclass

_PRICE_KEYS = ("value", "price", "close", "index", "brti", "v")
_TS_KEYS = ("ts_ms", "timestamp_ms", "ts", "time_ms", "t")


@dataclass(frozen=True)
class IndexSeries:
    points: list[tuple[int, float]]  # (timestamp_ms, price), chronological
    step_seconds: float
    per_second: bool

    @property
    def prices(self) -> list[float]:
        return [price for _ts, price in self.points]

    def latest(self) -> tuple[int, float] | None:
        if not self.points:
            return None
        return self.points[-1]

    def locked_prices(self, close_ms: int, now_ms: int, sample_count: int = 60) -> list[float] | None:
        """Prices of settlement samples at close-59s ... that are already <= now.

        Returns None if any required locked second is missing. Returns [] when
        the settlement window has not started.
        """
        if not self.per_second:
            return None
        by_second: dict[int, float] = {}
        for ts_ms, price in self.points:
            by_second[int(ts_ms // 1000)] = price
        locked: list[float] = []
        # Samples at close-59, ..., close (seconds).
        for offset in range(sample_count - 1, -1, -1):
            sample_s = int(close_ms // 1000) - offset
            if sample_s * 1000 > now_ms and sample_s > int(now_ms // 1000):
                break
            # A sample timestamp at or before now is locked.
            if sample_s > int(now_ms // 1000):
                break
            price = by_second.get(sample_s)
            if price is None:
                return None
            locked.append(price)
        return locked


def _price_of(row: dict) -> float | None:
    for key in _PRICE_KEYS:
        if key in row and row[key] is not None:
            try:
                value = float(row[key])
            except (TypeError, ValueError):
                continue
            if value > 0:
                return value
    return None


def _ts_of(row: dict) -> int | None:
    for key in _TS_KEYS:
        if key in row and row[key] is not None:
            try:
                value = int(float(row[key]))
            except (TypeError, ValueError):
                continue
            if value < 10_000_000_000:
                value *= 1000
            return value
    return None


def _from_rows(rows: list, *, step_seconds: float, per_second: bool) -> IndexSeries | None:
    points: list[tuple[int, float]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        price = _price_of(row)
        ts = _ts_of(row)
        if price is None or ts is None:
            continue
        points.append((ts, price))
    if len(points) < 2:
        return None
    points.sort(key=lambda item: item[0])
    return IndexSeries(points=points, step_seconds=step_seconds, per_second=per_second)


def extract_index_series(live_data: dict | None) -> IndexSeries | None:
    if not isinstance(live_data, dict):
        return None
    inner = live_data.get("live_data", live_data)
    if not isinstance(inner, dict):
        return None
    details = inner.get("details") if isinstance(inner.get("details"), dict) else {}
    candidates = []
    for source in (details, inner, live_data):
        if not isinstance(source, dict):
            continue
        for key in ("timeseries", "series", "prices", "ticks", "values"):
            value = source.get(key)
            if isinstance(value, list) and value:
                candidates.append(value)
        candles = source.get("candlesticks")
        if isinstance(candles, dict):
            one_s = candles.get("1S") or candles.get("1s")
            if isinstance(one_s, list) and one_s:
                parsed = _from_rows(one_s, step_seconds=1.0, per_second=True)
                if parsed:
                    return parsed
    for rows in candidates:
        parsed = _from_rows(rows, step_seconds=1.0, per_second=True)
        if parsed:
            # Treat a series as per-second only when median spacing is < 5s.
            gaps = [
                (parsed.points[i][0] - parsed.points[i - 1][0]) / 1000
                for i in range(1, len(parsed.points))
            ]
            median = sorted(gaps)[len(gaps) // 2]
            if median <= 5:
                return IndexSeries(parsed.points, step_seconds=max(median, 0.2), per_second=True)
            return IndexSeries(parsed.points, step_seconds=median, per_second=False)
    candles = (details.get("candlesticks") if isinstance(details, dict) else None) or {}
    minute = candles.get("1M") if isinstance(candles, dict) else None
    if isinstance(minute, list):
        return _from_rows(minute, step_seconds=60.0, per_second=False)
    return None


def slice_lookback(series: IndexSeries, seconds: float) -> list[float]:
    if not series.points:
        return []
    end = series.points[-1][0]
    start = end - int(seconds * 1000)
    prices = [price for ts, price in series.points if ts >= start]
    return prices
