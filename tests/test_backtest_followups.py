"""Follow-ups on the backtest: per-market stats, chunked loads, bisect, clock skew, trade log."""

from __future__ import annotations

import math
import os
import unittest
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from backtest.adapters import BookSnap, Dataset, TradeColumns, load_dataset
from backtest.fill_model import FillModel
from backtest.replay import ReplayOutput, ScoreBoard, replay
from backtest.report import summarize, to_markdown
from backtest.run import main as backtest_main
from engine.cutoff import CutoffPolicy, load_policy


def _write_spot(path: Path, start: int, stop: int) -> None:
    lines = ["ts,c"]
    for second in range(start, stop):
        price = 100 * math.exp(0.00002 * math.sin(second / 30))
        lines.append(f"{second},{price}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


class TestReportWeights(unittest.TestCase):
    def test_edge_and_hit_rate_are_per_contract_and_per_market(self):
        # Two partials on B would look like a 2/3 hit rate if each row is a trial.
        # The market lost money, so the per-market hit is one win out of two.
        fills = [
            {
                "ticker": "A",
                "split": "oos",
                "qty": 1,
                "price": 0.80,
                "pnl_net": 0.10,
                "pnl_raw": 0.10,
                "markout_10s": 4.0,
                "markout_60s": None,
            },
            {
                "ticker": "B",
                "split": "oos",
                "qty": 0.4,
                "price": 0.80,
                "pnl_net": -0.08,
                "pnl_raw": -0.08,
                "markout_10s": None,
                "markout_60s": 2.0,
            },
            {
                "ticker": "B",
                "split": "oos",
                "qty": 0.6,
                "price": 0.80,
                "pnl_net": 0.02,
                "pnl_raw": 0.02,
                "markout_10s": 1.0,
                "markout_60s": None,
            },
        ]
        result = ReplayOutput(
            fills=fills,
            decisions_path=None,
            n_markets=2,
            n_decisions=0,
            n_orders=0,
            order_times=[],
            n_cancels_at_cutoff=0,
            max_contracts=0.0,
            max_notional=0.0,
            scores_train=ScoreBoard(),
            scores_oos=ScoreBoard(),
            assumptions=[],
        )
        oos = summarize(result, strategy="favorite_maker", fill_model="conservative")["oos"]
        self.assertEqual(oos["n"], 2)
        self.assertEqual(oos["n_fills"], 3)
        self.assertAlmostEqual(oos["n_contracts"], 2.0)
        self.assertAlmostEqual(oos["edge_cents"], 2.0)
        self.assertAlmostEqual(oos["edge_cents_per_market"], 2.0)
        self.assertAlmostEqual(oos["edge_se"], 8.0)
        self.assertAlmostEqual(oos["edge_t"], 0.25)
        self.assertAlmostEqual(oos["hit_rate"], 0.5)
        self.assertAlmostEqual(oos["hit_rate_se"], 0.5)
        self.assertAlmostEqual(oos["hit_rate_t"], 1.0)
        self.assertAlmostEqual(oos["hit_rate_per_contract"], 0.8)
        # Qty-weighted: (4*1 + 1*0.6) / 1.6
        self.assertEqual(oos["markout_10s_n"], 2)
        self.assertAlmostEqual(oos["mean_markout_10s"], 2.875)
        self.assertEqual(oos["markout_60s_n"], 1)
        self.assertAlmostEqual(oos["mean_markout_60s"], 2.0)
        text = to_markdown(summarize(result, strategy="favorite_maker", fill_model="conservative"))
        self.assertIn("(n=2)", text)
        self.assertIn("(n=1)", text)


class TestColumnarWindow(unittest.TestCase):
    def test_window_drops_spot_and_prints_outside_the_chunk(self):
        close = 1_800_000_000
        open_ts = close - 900
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "markets.csv").write_text(
                "ticker,open_time,close_time,floor_strike,expiration_value,result\n"
                f"KXBTC15M-A,{open_ts},{close},100,101,yes\n"
                f"KXBTC15M-B,{open_ts - 86400},{close - 86400},100,101,yes\n",
                encoding="utf-8",
            )
            (root / "trades.csv").write_text(
                "ticker,ts,p,q\n"
                f"KXBTC15M-A,{close - 100},0.70,1\n"
                f"KXBTC15M-B,{close - 86400 - 100},0.70,3\n",
                encoding="utf-8",
            )
            _write_spot(root / "spot.csv", close - 86400 - 8000, close + 1)
            full = load_dataset(root)
            window = load_dataset(root, start_ts=float(close - 3600), end_ts=float(close + 1))
        self.assertEqual(sorted(full.trades), ["KXBTC15M-A", "KXBTC15M-B"])
        self.assertEqual(list(window.trades), ["KXBTC15M-A"])
        self.assertLess(len(window.spot_ts), len(full.spot_ts))
        self.assertGreater(float(window.spot_ts[0]), close - 86400)
        stamps, _prices, qty = window.trades["KXBTC15M-A"].between(close - 200, close - 50)
        self.assertEqual(len(stamps), 1)
        self.assertEqual(float(qty[0]), 1.0)


class TestIndexedLookups(unittest.TestCase):
    def test_asof_and_trade_window_match_a_boundary_scan(self):
        books = [
            BookSnap(ts=float(i), yes_bid=0.40, yes_ask=0.41, yes_bid_sz=1, yes_ask_sz=1)
            for i in range(0, 50_000, 5)
        ]
        dataset = Dataset(markets=[], books={"T": books})
        self.assertIsNone(dataset.book_asof("T", -1).ts if dataset.book_asof("T", -1) else None)
        self.assertEqual(dataset.book_asof("T", 100).ts, 100)
        self.assertEqual(dataset.book_asof("T", 104).ts, 100)
        self.assertEqual(dataset.book_asof("T", 105).ts, 105)
        columns = TradeColumns(
            ts=np.arange(10_000, dtype=np.float64),
            yes_price=np.full(10_000, 0.5),
            qty=np.ones(10_000),
        )
        stamps, _prices, _qty = columns.between(100.0, 110.0)
        self.assertEqual(list(stamps), list(range(101, 110)))

    def test_replay_fills_only_the_print_inside_the_decision_window(self):
        close = 1_800_000_000
        open_ts = close - 900
        candle = close - 200
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "markets.csv").write_text(
                "ticker,open_time,close_time,floor_strike,expiration_value,result\n"
                f"KXBTC15M-T,{open_ts},{close},100,101,yes\n",
                encoding="utf-8",
            )
            (root / "candles.csv").write_text(
                "ticker,end_ts,bid_c,ask_c,p_c\n"
                f"KXBTC15M-T,{candle},0.80,0.81,0.80\n",
                encoding="utf-8",
            )
            lines = ["ticker,ts,p,q"]
            for i in range(500):
                lines.append(f"KXBTC15M-T,{open_ts + i},0.80,1")
            lines.append(f"KXBTC15M-T,{close - 100},0.70,1")
            lines.append(f"KXBTC15M-T,{close - 10},0.70,1")
            (root / "trades.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
            _write_spot(root / "spot.csv", open_ts - 4000, close + 1)
            result = replay(
                load_dataset(root),
                strategy="favorite_maker",
                fill_model=FillModel.from_name("conservative"),
                policy=CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0),
            )
        self.assertEqual(len(result.fills), 1)
        self.assertAlmostEqual(float(result.fills[0]["qty"]), 1.0)


class TestReplayClockSkew(unittest.TestCase):
    def _root(self, tmp: str, close: int) -> Path:
        root = Path(tmp)
        open_ts = close - 900
        (root / "markets.csv").write_text(
            "ticker,open_time,close_time,floor_strike,expiration_value,result\n"
            f"KXBTC15M-T,{open_ts},{close},100,101,yes\n",
            encoding="utf-8",
        )
        # tau = 51s. A 2s skew blocks this candle; a 0s skew still quotes it.
        (root / "candles.csv").write_text(
            "ticker,end_ts,bid_c,ask_c,p_c\n"
            f"KXBTC15M-T,{close - 51},0.80,0.81,0.80\n",
            encoding="utf-8",
        )
        _write_spot(root / "spot.csv", open_ts - 4000, close + 1)
        return root

    def test_default_margin_matches_the_live_engine_and_cannot_go_negative(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CUTOFF_SECONDS", None)
            os.environ.pop("CUTOFF_CLOCK_SKEW_SECONDS", None)
            policy = load_policy()
        self.assertEqual(policy.cutoff_seconds, 50)
        self.assertEqual(policy.clock_skew_margin_seconds, 2)
        with patch.dict(os.environ, {"CUTOFF_SECONDS": "45", "CUTOFF_CLOCK_SKEW_SECONDS": "-3"}):
            clamped = load_policy()
        self.assertEqual(clamped.cutoff_seconds, 50)
        self.assertEqual(clamped.clock_skew_margin_seconds, 0)
        with self.assertRaises(ValueError):
            CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=-1)
        close = 1_800_000_000
        with TemporaryDirectory() as tmp:
            root = self._root(tmp, close)
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CUTOFF_SECONDS", None)
                os.environ.pop("CUTOFF_CLOCK_SKEW_SECONDS", None)
                default = replay(
                    load_dataset(root),
                    strategy="favorite_maker",
                    fill_model=FillModel.from_name("conservative"),
                )
            explicit = replay(
                load_dataset(root),
                strategy="favorite_maker",
                fill_model=FillModel.from_name("conservative"),
                policy=CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0),
            )
            code = backtest_main(
                [
                    "--data", str(root),
                    "--strategy", "favorite_maker",
                    "--fill-model", "conservative",
                    "--out", str(root / "out"),
                    "--clock-skew", "-1",
                ]
            )
        self.assertEqual(default.n_orders, 0)
        self.assertGreaterEqual(explicit.n_orders, 1)
        self.assertTrue(all(ts < close - 50 for ts in explicit.order_times))
        self.assertEqual(code, 2)


class TestChunkAggregation(unittest.TestCase):
    def test_one_day_chunks_match_a_full_replay(self):
        day1 = datetime(2026, 7, 1, 12, tzinfo=UTC)
        close1 = int(day1.timestamp())
        close2 = close1 + 86400
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = ["ticker,open_time,close_time,floor_strike,expiration_value,result"]
            candles = ["ticker,end_ts,bid_c,ask_c,p_c"]
            trades = ["ticker,ts,p,q"]
            for close, ticker in ((close1, "KXBTC15M-D1"), (close2, "KXBTC15M-D2")):
                open_ts = close - 900
                rows.append(f"{ticker},{open_ts},{close},100,101,yes")
                candles.append(f"{ticker},{close - 200},0.80,0.81,0.80")
                trades.append(f"{ticker},{close - 100},0.70,1")
            (root / "markets.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
            (root / "candles.csv").write_text("\n".join(candles) + "\n", encoding="utf-8")
            (root / "trades.csv").write_text("\n".join(trades) + "\n", encoding="utf-8")
            _write_spot(root / "spot.csv", close1 - 900 - 8000, close2 + 1)
            model = FillModel.from_name("conservative")
            policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0)
            full = replay(load_dataset(root), strategy="favorite_maker", fill_model=model, policy=policy)
            code = backtest_main(
                [
                    "--data", str(root),
                    "--strategy", "favorite_maker",
                    "--fill-model", "conservative",
                    "--out", str(root / "out"),
                    "--chunk", "1",
                    "--clock-skew", "0",
                    "--start", "2026-07-01",
                    "--end", "2026-07-02",
                ]
            )
            summary = (root / "out" / "summary.json").read_text(encoding="utf-8")
        self.assertEqual(code, 0)
        import json

        payload = json.loads(summary)
        self.assertEqual(full.n_markets, 2)
        self.assertEqual(len(full.fills), 2)
        self.assertEqual(payload["n_markets"], full.n_markets)
        self.assertEqual(payload["n_orders"], full.n_orders)
        self.assertEqual(payload["n_decisions"], full.n_decisions)
        self.assertEqual(payload["oos"]["n_fills"], len(full.fills))
        self.assertAlmostEqual(
            payload["oos"]["pnl_net"],
            sum(float(row["pnl_net"]) for row in full.fills),
        )


class TestTradeLogPath(unittest.TestCase):
    def test_risk_manager_does_not_create_repo_root_trades_csv(self):
        import config
        from risk_manager import RiskManager

        repo_file = Path("trades.csv").resolve()
        existed = repo_file.exists()
        before = repo_file.stat().st_mtime_ns if existed else None
        configured = Path(config.TRADE_LOG_FILE).resolve()
        self.assertNotEqual(configured, repo_file)
        RiskManager()
        self.assertTrue(configured.exists())
        if existed:
            self.assertEqual(repo_file.stat().st_mtime_ns, before)
        else:
            self.assertFalse(repo_file.exists())


if __name__ == "__main__":
    unittest.main()
