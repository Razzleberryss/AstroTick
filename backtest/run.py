"""CLI: python -m backtest.run --data DIR --start --end --strategy ... --fill-model ...

Reads historical files only. Does not load API keys and does not send orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from backtest.adapters import MarketAdapter, load_dataset
from backtest.fill_model import FillModel
from backtest.replay import merge_outputs, parse_date, replay
from backtest.report import summarize, write_reports
from engine.cutoff import CutoffPolicy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay KXBTC15M through the live engine")
    parser.add_argument("--data", required=True, help="Directory of markets, candles, trades, and spot")
    parser.add_argument("--start", default=None, help="First close date included (UTC, YYYY-MM-DD)")
    parser.add_argument("--end", default=None, help="Last close date included (UTC, YYYY-MM-DD)")
    parser.add_argument(
        "--strategy",
        required=True,
        choices=("extreme_fade", "favorite_maker", "all"),
    )
    parser.add_argument(
        "--fill-model",
        default="conservative",
        choices=("conservative", "optimistic"),
    )
    parser.add_argument("--out", default="backtest_out", help="Where to write decisions.jsonl and summaries")
    parser.add_argument(
        "--oos-start",
        default="2026-07-01",
        help="Close dates on or after this UTC date are the out-of-sample split",
    )
    parser.add_argument(
        "--haircut-cents",
        type=float,
        default=None,
        help="Override the maker adverse-selection haircut. Conservative default is 3",
    )
    parser.add_argument(
        "--chunk",
        type=float,
        default=None,
        help="Replay this many days at a time and aggregate. Spot and prints are loaded per chunk.",
    )
    parser.add_argument(
        "--clock-skew",
        type=float,
        default=None,
        help="Seconds added to the 50s cutoff. Default is the live engine's 2s margin. Cannot be negative.",
    )
    args = parser.parse_args(argv)
    root = Path(args.data)
    if not root.is_dir():
        print(f"data directory not found: {root}", file=sys.stderr)
        return 2
    if args.chunk is not None and args.chunk <= 0:
        print("--chunk must be a positive number of days", file=sys.stderr)
        return 2
    policy = None
    if args.clock_skew is not None:
        try:
            policy = CutoffPolicy(clock_skew_margin_seconds=args.clock_skew)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    haircut = None if args.haircut_cents is None else Decimal(str(args.haircut_cents)) / Decimal("100")
    model = FillModel.from_name(args.fill_model, haircut=haircut)
    start_ts = parse_date(args.start)
    end_ts = parse_date(args.end, end=True)
    oos_start = parse_date(args.oos_start) or parse_date("2026-07-01")
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "decisions.jsonl"
    handle = decisions_path.open("w", encoding="utf-8")

    def _write(row: dict) -> None:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def _one(window_start: float | None, window_end: float | None):
        try:
            dataset = load_dataset(root, start_ts=window_start, end_ts=window_end)
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return exc
        if not dataset.markets:
            return None
        return replay(
            dataset,
            strategy=args.strategy,
            fill_model=model,
            start_ts=window_start,
            end_ts=window_end,
            oos_start_ts=oos_start,
            policy=policy,
            decision_log=_write,
        )

    try:
        if args.chunk is None:
            result = _one(start_ts, end_ts)
            if isinstance(result, FileNotFoundError):
                return 2
            if result is None:
                print("no markets in the requested window", file=sys.stderr)
                return 2
        else:
            window_start, window_end = _chunk_bounds(root, start_ts, end_ts)
            if window_start is None or window_end is None or window_start >= window_end:
                print("could not determine a chunk window from --start/--end or markets.csv", file=sys.stderr)
                return 2
            parts = []
            cursor = window_start
            span = float(args.chunk) * 86400.0
            while cursor < window_end:
                nxt = min(window_end, cursor + span)
                if nxt <= cursor:
                    break
                part = _one(cursor, nxt)
                if isinstance(part, FileNotFoundError):
                    return 2
                if part is not None:
                    parts.append(part)
                cursor = nxt
            result = merge_outputs(parts)
            if not parts:
                print("no markets in the requested window", file=sys.stderr)
                return 2
    finally:
        handle.close()
    result.decisions_path = str(decisions_path)
    summary = summarize(result, strategy=args.strategy, fill_model=args.fill_model)
    write_reports(out_dir, summary, decisions_path)
    oos = summary["oos"]
    print(
        f"markets={summary['n_markets']} decisions={summary['n_decisions']} "
        f"orders={summary['n_orders']} oos_fills={oos['n']} "
        f"oos_edge_c={oos['edge_cents']} wrote {out_dir}"
    )
    return 0


def _chunk_bounds(root: Path, start_ts: float | None, end_ts: float | None) -> tuple[float | None, float | None]:
    """Fill a missing bound from market close times so chunks cover every market once."""
    if start_ts is not None and end_ts is not None:
        return start_ts, end_ts
    closes = [market.close_ts for market in MarketAdapter().load(root)]
    if not closes:
        return start_ts, end_ts
    if start_ts is None:
        first = datetime.fromtimestamp(min(closes), UTC)
        start_ts = datetime(first.year, first.month, first.day, tzinfo=UTC).timestamp()
    if end_ts is None:
        end_ts = max(closes) + 1.0
    return start_ts, end_ts


if __name__ == "__main__":
    raise SystemExit(main())
