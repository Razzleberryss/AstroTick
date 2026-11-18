"""PnL, markouts, Brier, and the JSON + markdown summary."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path

from backtest.fill_model import HAIRCUT_SENSITIVITY
from backtest.replay import ReplayOutput, price_bucket


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


def _sample_stats(values: list[float]) -> dict:
    """Mean, standard error, and t-stat. One value is one independent observation."""
    n = len(values)
    if n == 0:
        return {"n": 0, "mean": None, "se": None, "t": None}
    mean = sum(values) / n
    if n < 2:
        return {"n": n, "mean": mean, "se": None, "t": None}
    var = sum((value - mean) ** 2 for value in values) / (n - 1)
    se = math.sqrt(var / n)
    return {"n": n, "mean": mean, "se": se, "t": None if se == 0 else mean / se}


def _markets(fills: list[dict]) -> list[dict]:
    """Collapse partial fills so a market is one bet, not one row per print."""
    grouped: dict[str, dict] = {}
    for row in fills:
        slot = grouped.setdefault(
            row["ticker"],
            {"ticker": row["ticker"], "qty": 0.0, "pnl_net": 0.0, "pnl_raw": 0.0, "price_qty": 0.0},
        )
        qty = float(row["qty"])
        slot["qty"] += qty
        slot["pnl_net"] += float(row["pnl_net"])
        slot["pnl_raw"] += float(row["pnl_raw"])
        slot["price_qty"] += float(row["price"]) * qty
    markets = []
    for slot in grouped.values():
        if slot["qty"] <= 0:
            continue
        slot["price"] = slot["price_qty"] / slot["qty"]
        slot["edge_cents"] = 100.0 * slot["pnl_net"] / slot["qty"]
        slot["won"] = slot["pnl_net"] > 0
        markets.append(slot)
    return markets


def _weighted_mean(values: list[float], weights: list[float]) -> float | None:
    total = sum(weights)
    if total <= 0 or not values:
        return None
    return sum(value * weight for value, weight in zip(values, weights, strict=True)) / total


def _block(fills: list[dict], scores_headline, scores_buckets) -> dict:
    markets = _markets(fills)
    qty = [float(row["qty"]) for row in fills]
    pnl = [float(row["pnl_net"]) for row in fills]
    raw = [float(row["pnl_raw"]) for row in fills]
    contracts = sum(qty)
    cumulative = 0.0
    peak = 0.0
    max_dd = 0.0
    for value in pnl:
        cumulative += value
        peak = max(peak, cumulative)
        max_dd = max(max_dd, peak - cumulative)
    market_edges = [slot["edge_cents"] for slot in markets]
    market_hits = [1.0 if slot["won"] else 0.0 for slot in markets]
    edge_stats = _sample_stats(market_edges)
    hit_stats = _sample_stats(market_hits)
    won_qty = sum(
        q for q, flag in zip(qty, (float(row["pnl_net"]) > 0 for row in fills), strict=True) if flag
    )
    by_price: dict[str, list[dict]] = defaultdict(list)
    for slot in markets:
        by_price[price_bucket(slot["price"])].append(slot)
    mark_10 = [(float(row["markout_10s"]), float(row["qty"])) for row in fills if row.get("markout_10s") is not None]
    mark_60 = [(float(row["markout_60s"]), float(row["qty"])) for row in fills if row.get("markout_60s") is not None]
    return {
        "n": len(markets),
        "n_fills": len(fills),
        "n_contracts": contracts,
        "hit_rate": hit_stats["mean"],
        "hit_rate_se": hit_stats["se"],
        "hit_rate_t": hit_stats["t"],
        "hit_rate_per_contract": None if contracts <= 0 else won_qty / contracts,
        "pnl_net": sum(pnl),
        "pnl_raw": sum(raw),
        "edge_cents": None if contracts <= 0 else 100.0 * sum(pnl) / contracts,
        "edge_cents_per_market": edge_stats["mean"],
        "edge_se": edge_stats["se"],
        "edge_t": edge_stats["t"],
        "edge_cents_before_haircut": None if contracts <= 0 else 100.0 * sum(raw) / contracts,
        "max_drawdown": max_dd,
        "mean_markout_10s": _weighted_mean([v for v, _q in mark_10], [q for _v, q in mark_10]),
        "markout_10s_n": len(mark_10),
        "mean_markout_60s": _weighted_mean([v for v, _q in mark_60], [q for _v, q in mark_60]),
        "markout_60s_n": len(mark_60),
        "brier": _brier(scores_headline),
        "log_loss": _log_loss(scores_headline),
        "brier_n": len(scores_headline),
        "by_price": {
            name: {
                "n": len(slots),
                "n_contracts": sum(slot["qty"] for slot in slots),
                "edge_cents": 100.0 * sum(slot["pnl_net"] for slot in slots) / sum(slot["qty"] for slot in slots),
                **{k: _sample_stats([slot["edge_cents"] for slot in slots])[k] for k in ("se", "t")},
            }
            for name, slots in sorted(by_price.items())
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
            rows.append({"haircut_cents": float(haircut * 100), "n": 0, "edge_cents": None, "se": None, "t": None})
            continue
        adjusted = []
        for row in fills:
            adjusted.append({**row, "pnl_net": float(row["pnl_raw"]) - float(row["qty"]) * float(haircut)})
        markets = _markets(adjusted)
        stats = _sample_stats([slot["edge_cents"] for slot in markets])
        contracts = sum(float(row["qty"]) for row in adjusted)
        rows.append(
            {
                "haircut_cents": float(haircut * 100),
                "n": stats["n"],
                "n_contracts": contracts,
                "edge_cents": None if contracts <= 0 else 100.0 * sum(float(row["pnl_net"]) for row in adjusted) / contracts,
                "se": stats["se"],
                "t": stats["t"],
                "pnl_net": sum(float(row["pnl_net"]) for row in adjusted),
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
        "| haircut c | markets | edge c/contract | se | t |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in summary["haircut_sensitivity_oos"]:
        lines.append(
            f"| {_fmt(row['haircut_cents'])} | {row['n']} | {_fmt(row['edge_cents'])} | {_fmt(row.get('se'))} | {_fmt(row.get('t'))} |"
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
        f"- Markets with a fill: {block['n']} ({block['n_fills']} fill rows, { _fmt(block['n_contracts']) } contracts)",
        f"- Hit rate per market: {_fmt(block['hit_rate'])} (se {_fmt(block['hit_rate_se'])}, t {_fmt(block['hit_rate_t'])})",
        f"- Hit rate per contract: {_fmt(block['hit_rate_per_contract'])}",
        f"- Edge per contract after fees and haircut: {_fmt(block['edge_cents'])} c",
        f"- Edge per market: {_fmt(block['edge_cents_per_market'])} c (se {_fmt(block['edge_se'])}, t {_fmt(block['edge_t'])})",
        f"- Edge per contract before haircut: {_fmt(block['edge_cents_before_haircut'])} c",
        f"- PnL net: {_fmt(block['pnl_net'])}",
        f"- Max drawdown: {_fmt(block['max_drawdown'])}",
        (
            f"- Markout +10s / +60s (cents, per contract): "
            f"{_fmt(block['mean_markout_10s'])} (n={block['markout_10s_n']}) / "
            f"{_fmt(block['mean_markout_60s'])} (n={block['markout_60s_n']})"
        ),
        f"- Brier / log loss (near 5 min, n={block['brier_n']}): {_fmt(block['brier'])} / {_fmt(block['log_loss'])}",
        "",
        "By entry price:",
        "",
        "| bucket | markets | edge c/contract | se | t |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, row in block["by_price"].items():
        lines.append(
            f"| {name} | {row['n']} | {_fmt(row['edge_cents'])} | {_fmt(row.get('se'))} | {_fmt(row.get('t'))} |"
        )
    lines.extend(["", "Fair value by time to close:", "", "| bucket | n | brier | log loss |", "| --- | --- | --- | --- |"])
    for name, row in block["by_tau"].items():
        lines.append(f"| {name} | {row['n']} | {_fmt(row['brier'])} | {_fmt(row['log_loss'])} |")
    return "\n".join(lines)


def write_reports(out_dir: Path, summary: dict, decisions_path: Path) -> None:
    summary["decisions_path"] = str(decisions_path)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    (out_dir / "summary.md").write_text(to_markdown(summary), encoding="utf-8")
