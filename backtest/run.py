"""CLI: python -m backtest.run --data DIR --start --end --strategy ... --fill-model ...

Reads historical files only. Does not load API keys and does not send orders.
"""

from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal
from pathlib import Path

from backtest.adapters import load_dataset
from backtest.fill_model import FillModel
from backtest.replay import parse_date, replay
from backtest.report import summarize, write_reports


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
    args = parser.parse_args(argv)
    root = Path(args.data)
    if not root.is_dir():
        print(f"data directory not found: {root}", file=sys.stderr)
        return 2
    haircut = None if args.haircut_cents is None else Decimal(str(args.haircut_cents)) / Decimal("100")
    model = FillModel.from_name(args.fill_model, haircut=haircut)
    try:
        dataset = load_dataset(root)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    decisions_path = out_dir / "decisions.jsonl"
    handle = decisions_path.open("w", encoding="utf-8")

    def _write(row: dict) -> None:
        handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    try:
        result = replay(
            dataset,
            strategy=args.strategy,
            fill_model=model,
            start_ts=parse_date(args.start),
            end_ts=parse_date(args.end, end=True),
            oos_start_ts=parse_date(args.oos_start) or parse_date("2026-07-01"),
            decision_log=_write,
        )
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


if __name__ == "__main__":
    raise SystemExit(main())
