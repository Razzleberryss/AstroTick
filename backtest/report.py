"""PnL, markouts, Brier, and the JSON + markdown summary."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from backtest.fill_model import HAIRCUT_SENSITIVITY
from backtest.replay import ReplayOutput, price_bucket


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return sum(values) / len(values)


def _brier(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _log_loss(pairs: list[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for probability, outcome in pairs:
        p = min(max(probability, 1e-6), 1 - 1e-6)
        total += outcome * math.log(p) + (1 - outcome) * math.log(1 - p)
    return -total / len(pairs)


def _block(fills: list[dict], scores_headline, scores_buckets) -> dict:
    pnl = [float(row["pnl_net"]) for row in fills]
    raw = [float(row["pnl_raw"]) for row in fills]
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in pnl:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    wins = sum(1 for row in fills if float(row["pnl_net"]) > 0)
    by_price: dict[str, list[float]] = defaultdict(list)
    for row in fills:
        by_price[price_bucket(float(row["price"]))].append(float(row["pnl_net"]))
    mark_10 = [float(row["markout_10s"]) for row in fills if row.get("markout_10s") is not None]
    mark_60 = [float(row["markout_60s"]) for row in fills if row.get("markout_60s") is not None]
    return {
        "n": len(fills),
        "hit_rate": None if not fills else wins / len(fills),
        "pnl_net": sum(pnl),
        "pnl_raw": sum(raw),
        "edge_cents": None if not pnl else 100 * (sum(pnl) / len(pnl)),
        "edge_cents_before_haircut": None if not raw else 100 * (sum(raw) / len(raw)),
        "max_drawdown": max_dd,
        "mean_markout_10s": _mean(mark_10),
        "mean_markout_60s": _mean(mark_60),
        "brier": _brier(scores_headline),
        "log_loss": _log_loss(scores_headline),
        "brier_n": len(scores_headline),
        "by_price": {
            name: {"n": len(values), "edge_cents": 100 * sum(values) / len(values)}
            for name, values in sorted(by_price.items())
        },
        "by_tau": {
            name: {
                "n": len(pairs),
                "brier": _brier(pairs),
                "log_loss": _log_loss(pairs),
            }
            for name, pairs in sorted(scores_buckets.items())
        },
    }


def _sensitivity(fills: list[dict]) -> list[dict]:
    """Same maker fills, haircut replaced. Settlement PnL before haircut is pnl_raw."""
    rows = []
    for haircut in HAIRCUT_SENSITIVITY:
        if not fills:
            rows.append({"haircut_cents": float(haircut * 100), "n": 0, "edge_cents": None})
            continue
        pnls = []
        for row in fills:
            qty = float(row["qty"])
            pnls.append(float(row["pnl_raw"]) - qty * float(haircut))
        rows.append(
            {
                "haircut_cents": float(haircut * 100),
                "n": len(pnls),
                "edge_cents": 100 * sum(pnls) / len(pnls),
                "pnl_net": sum(pnls),
            }
        )
    return rows


def summarize(result: ReplayOutput, *, strategy: str, fill_model: str) -> dict:
    def take(split: str) -> list[dict]:
        return [row for row in result.fills if row["split"] == split]

    scores = {"train": result.scores_train, "oos": result.scores_oos}
    blocks = {}
    for name in ("train", "oos"):
        board = scores[name]
        blocks[name] = _block(take(name), board.headline, board.buckets)
    everything = _block(
        result.fills,
        result.scores_train.headline + result.scores_oos.headline,
        _merge_buckets(result.scores_train.buckets, result.scores_oos.buckets),
    )
    return {
        "strategy": strategy,
        "fill_model": fill_model,
        "n_markets": result.n_markets,
        "n_decisions": result.n_decisions,
        "n_orders": result.n_orders,
        "n_cancels_at_cutoff": result.n_cancels_at_cutoff,
        "max_contracts": result.max_contracts,
        "max_notional": result.max_notional,
        "decisions_path": result.decisions_path,
        "assumptions": result.assumptions,
        "study_reference": {
            "name": "fade_extreme taker, fixed >=85c and model -10c",
            "period": "OOS Jul-Nov 2026",
            "n": 169,
            "edge_cents": 5.43,
            "se_cents": 2.83,
            "note": (
                "That number bought the cheap side as a taker and held to settlement. "
                "This replay runs the live post-only extreme_fade and favorite_maker "
                "quotes, so it is not expected to print +5.4c."
            ),
        },
        "train": blocks["train"],
        "oos": blocks["oos"],
        "all": everything,
        "haircut_sensitivity_oos": _sensitivity(take("oos")),
        "tau_buckets_note": "Brier uses one fair value per market per bucket (closest to the bucket center). Headline Brier uses the decision closest to 5 minutes.",
    }


def _merge_buckets(left, right):
    merged: dict[str, list] = {}
    for source in (left, right):
        for name, pairs in source.items():
            merged.setdefault(name, []).extend(pairs)
    return merged


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


def to_markdown(summary: dict) -> str:
    lines = [
        "# KXBTC15M backtest",
        "",
        f"Strategy `{summary['strategy']}`, fill model `{summary['fill_model']}`.",
        f"Markets {summary['n_markets']}, decisions {summary['n_decisions']}, orders {summary['n_orders']}.",
        f"Max contracts {summary['max_contracts']}, max notional ${summary['max_notional']:.4f}.",
        f"Cutoff cancels recorded at the T-50 decision: {summary['n_cancels_at_cutoff']}.",
        "",
        "The live extreme-fade quote is post-only. The fade study's +5.4c "
        "(n=169, OOS, taker, SE 2.8c) is a different execution. It is listed "
        "so a reader does not treat a match or a miss as a bug in the loader.",
        "",
        "## Out of sample",
        "",
        _table(summary["oos"]),
        "",
        "## Train",
        "",
        _table(summary["train"]),
        "",
        "## Maker haircut sensitivity (OOS, cents per contract)",
        "",
        "| haircut c | n | edge c |",
        "| --- | --- | --- |",
    ]
    for row in summary["haircut_sensitivity_oos"]:
        lines.append(
            f"| {_fmt(row['haircut_cents'])} | {row['n']} | {_fmt(row['edge_cents'])} |"
        )
    lines.extend(["", "## Assumptions", ""])
    for item in summary["assumptions"]:
        lines.append(f"- {item} **needs queue data to validate**")
    lines.extend(
        [
            "",
            "See `backtest/FILL_MODEL.md`. Spread context: 1c median and p90 from T-15m to T-1m, so a taker pays the ask.",
            "",
        ]
    )
    return "\n".join(lines)


def _table(block: dict) -> str:
    lines = [
        f"- Fills: {block['n']}",
        f"- Hit rate: {_fmt(block['hit_rate'])}",
        f"- Edge after fees and haircut: {_fmt(block['edge_cents'])} c/contract",
        f"- Edge before haircut: {_fmt(block['edge_cents_before_haircut'])} c/contract",
        f"- PnL net: {_fmt(block['pnl_net'])}",
        f"- Max drawdown: {_fmt(block['max_drawdown'])}",
        f"- Markout +10s / +60s (cents): {_fmt(block['mean_markout_10s'])} / {_fmt(block['mean_markout_60s'])}",
        f"- Brier / log loss (near 5 min, n={block['brier_n']}): {_fmt(block['brier'])} / {_fmt(block['log_loss'])}",
        "",
        "By entry price:",
        "",
        "| bucket | n | edge c |",
        "| --- | --- | --- |",
    ]
    for name, row in block["by_price"].items():
        lines.append(f"| {name} | {row['n']} | {_fmt(row['edge_cents'])} |")
    lines.extend(["", "Fair value by time to close:", "", "| bucket | n | brier | log loss |", "| --- | --- | --- | --- |"])
    for name, row in block["by_tau"].items():
        lines.append(f"| {name} | {row['n']} | {_fmt(row['brier'])} | {_fmt(row['log_loss'])} |")
    return "\n".join(lines)


def write_reports(out_dir: Path, summary: dict, decisions_path: Path) -> None:
    summary["decisions_path"] = str(decisions_path)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "summary.md").write_text(to_markdown(summary), encoding="utf-8")
