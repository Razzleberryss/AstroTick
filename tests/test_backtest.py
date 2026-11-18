"""Fills, fees, the T-50 sim rule, settlement ties, and no lookahead."""

import math
import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

import numpy as np

from backtest.adapters import load_dataset
from backtest.features import asof_price, trailing_sigma
from backtest.fill_model import FillModel
from backtest.replay import replay
from backtest.run import main as backtest_main
from backtest.settlement import resolves_yes
from backtest.sim_exchange import SimExchange
from engine.cutoff import CutoffPolicy, reset_registry
from engine.fair_value import fair_value_yes
from engine.orders import OrderGateway, QuoteIntent, TrackedQuote
from engine.risk import Exposure, RiskBook, RiskLimits
from engine.vol import realized_sigma


def _exposure() -> Exposure:
    return Exposure(
        contracts_on_market=Decimal("0"),
        contracts_total=Decimal("0"),
        dollars_total=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        feed_age_seconds=0,
    )


class TestFillsAndFees(unittest.TestCase):
    def test_conservative_maker_needs_a_trade_through(self):
        model = FillModel.from_name("conservative")
        self.assertFalse(model.maker_fills_on("yes", Decimal("0.40"), Decimal("0.40")))
        self.assertTrue(model.maker_fills_on("yes", Decimal("0.40"), Decimal("0.39")))
        self.assertFalse(model.maker_fills_on("no", Decimal("0.10"), Decimal("0.90")))
        self.assertTrue(model.maker_fills_on("no", Decimal("0.10"), Decimal("0.91")))
        optimistic = FillModel.from_name("optimistic")
        self.assertTrue(optimistic.maker_fills_on("yes", Decimal("0.40"), Decimal("0.40")))
        self.assertEqual(optimistic.haircut_per_contract, Decimal("0"))
        self.assertEqual(model.haircut_per_contract, Decimal("0.03"))

    def test_taker_crosses_the_ask_and_caps_size(self):
        model = FillModel.from_name("conservative")
        self.assertEqual(
            model.taker_price("yes", Decimal("0.40"), Decimal("0.41")),
            Decimal("0.41"),
        )
        self.assertEqual(
            model.taker_price("no", Decimal("0.40"), Decimal("0.41")),
            Decimal("0.60"),
        )
        self.assertEqual(model.taker_size(Decimal("10"), Decimal("3")), Decimal("3"))
        self.assertEqual(model.taker_size(Decimal("10"), None), Decimal("1"))

    def test_fee_uses_the_live_schedule(self):
        model = FillModel.from_name("conservative")
        raw = Decimal("0.07") * Decimal("1") * Decimal("0.50") * Decimal("0.50")
        taker = model.fee(liquidity="taker", contracts=Decimal("1"), price=Decimal("0.50"))
        maker = model.fee(liquidity="maker", contracts=Decimal("1"), price=Decimal("0.50"))
        self.assertGreaterEqual(taker, raw)
        self.assertEqual(maker, Decimal("0"))
        charged = FillModel(maker_fee_type="quadratic_with_maker_fees")
        self.assertGreater(
            charged.fee(liquidity="maker", contracts=Decimal("1"), price=Decimal("0.50")),
            Decimal("0"),
        )

    def test_touch_does_not_fill_and_through_does(self):
        close = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
        cutoff = close - timedelta(seconds=50)
        exchange = SimExchange(FillModel.from_name("conservative"))
        exchange.set_clock(close - timedelta(seconds=120))
        exchange.create(
            {
                "ticker": "KXBTC15M-T",
                "client_order_id": "c",
                "side": "bid",
                "price": "0.4000",
                "count": "1.00",
            },
            mode="paper",
        )
        none = exchange.match_trade(
            "KXBTC15M-T", close - timedelta(seconds=100), Decimal("0.40"), Decimal("5"), cutoff=cutoff
        )
        self.assertEqual(none, [])
        filled = exchange.match_trade(
            "KXBTC15M-T", close - timedelta(seconds=90), Decimal("0.39"), Decimal("2"), cutoff=cutoff
        )
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0].qty, Decimal("1.00"))
        late = exchange.match_trade(
            "KXBTC15M-T", close - timedelta(seconds=10), Decimal("0.10"), Decimal("5"), cutoff=cutoff
        )
        self.assertEqual(late, [])


class TestCutoffSimulation(unittest.TestCase):
    def test_no_orders_or_fills_after_t50_and_resting_orders_cancel(self):
        close = datetime(2026, 8, 1, 15, 0, tzinfo=UTC)
        policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0)
        exchange = SimExchange(FillModel.from_name("conservative"))
        gateway = OrderGateway(
            sender=exchange,
            risk=RiskBook(RiskLimits()),
            policy=policy,
            mode="paper",
        )
        reset_registry()
        ticker = "KXBTC15M-CUT"
        intent = QuoteIntent(
            strategy="favorite_maker",
            ticker=ticker,
            contract_side="yes",
            action="buy",
            limit_price=Decimal("0.40"),
            count=Decimal("1"),
            reason="rest before the cutoff",
            fair_value_yes=0.5,
        )
        placed_at = close - timedelta(seconds=120)
        exchange.set_clock(placed_at)
        sync = gateway.sync(
            ticker=ticker,
            close_time=close,
            now=placed_at,
            desired=[intent],
            fair_value_yes=0.5,
            resting=[],
            exposure=_exposure(),
        )
        self.assertEqual(sync["action"], "placed")
        for placed in sync["placed"]:
            exchange.bind_gateway_meta(placed)
        # Still working at T-40: the gateway must cancel and refuse a new order.
        later = close - timedelta(seconds=40)
        exchange.set_clock(later)

        working = exchange.working(ticker)
        self.assertEqual(len(working), 1)
        blocked = gateway.sync(
            ticker=ticker,
            close_time=close,
            now=later,
            desired=[intent],
            fair_value_yes=0.5,
            resting=[
                TrackedQuote(
                    order_id=working[0].order_id,
                    client_order_id=working[0].client_order_id,
                    ticker=ticker,
                    contract_side="yes",
                    action="buy",
                    price=working[0].price,
                    remaining=working[0].remaining,
                    fair_value_yes=0.5,
                    placed_at=working[0].placed_at,
                    strategy="favorite_maker",
                    reason="resting",
                )
            ],
            exposure=_exposure(),
        )
        self.assertEqual(blocked["action"], "cutoff")
        self.assertEqual(exchange.orders[working[0].order_id].status, "cancelled")
        self.assertTrue(all(ts <= close - timedelta(seconds=50) for ts in exchange.create_times))
        late = exchange.match_trade(
            ticker, close - timedelta(seconds=20), Decimal("0.10"), Decimal("3"),
            cutoff=close - timedelta(seconds=50),
        )
        self.assertEqual(late, [])
        self.assertTrue(all(fill.ts < close - timedelta(seconds=50) for fill in exchange.fills))
        reset_registry()


class TestSettlementTie(unittest.TestCase):
    def test_equal_average_is_yes(self):
        self.assertTrue(resolves_yes(100.0, 100.0, None))
        self.assertTrue(resolves_yes(100.0, 100.0, "no"))
        self.assertFalse(resolves_yes(99.99, 100.0, None))
        locked = fair_value_yes(
            spot=100, strike=100, tau_seconds=0, sigma_per_sqrt_second=0,
            locked_samples=[100.0] * 60,
        )
        self.assertEqual(locked.probability_yes, 1.0)


class TestNoLookahead(unittest.TestCase):
    def test_asof_ignores_future_prints(self):
        ts = np.array([10.0, 20.0, 30.0])
        px = np.array([1.0, 2.0, 100.0])
        self.assertEqual(asof_price(ts, px, 25, tolerance=100), 2.0)
        self.assertEqual(asof_price(ts, px, 20, tolerance=100), 2.0)
        self.assertEqual(asof_price(ts, px, 19, tolerance=100), 1.0)
        self.assertIsNone(asof_price(ts, px, 9, tolerance=100))

    def test_trailing_sigma_matches_the_live_estimator(self):
        prices = [100 * math.exp(0.0002 * math.sin(i / 3)) for i in range(40)]
        got = trailing_sigma(np.array(prices), 10.0, 12)
        expected = realized_sigma(prices[40 - 13 :], 10.0)
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(float(got[-1]), float(expected), places=10)


class TestReplayCutoff(unittest.TestCase):
    def test_replay_cancels_at_t50_and_does_not_fill_after(self):
        close = 1_700_000_000
        open_ts = close - 900
        start = datetime.fromtimestamp(close, UTC).date().isoformat()
        end = datetime.fromtimestamp(close + 86400, UTC).date().isoformat()
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "markets.csv").write_text(
                "ticker,open_time,close_time,floor_strike,expiration_value,result\n"
                f"KXBTC15M-T,{open_ts},{close},100,100,yes\n",
                encoding="utf-8",
            )
            candles = ["ticker,end_ts,bid_c,ask_c,p_c"]
            for candle_end in range(open_ts + 60, close - 40, 60):
                candles.append(f"KXBTC15M-T,{candle_end},0.80,0.81,0.80")
            (root / "candles.csv").write_text("\n".join(candles) + "\n", encoding="utf-8")
            # Touch before the cutoff (must not fill) and a sweep after it.
            (root / "trades.csv").write_text(
                "ticker,ts,p,q,side\n"
                f"KXBTC15M-T,{close - 100},0.80,5,yes\n"
                f"KXBTC15M-T,{close - 20},0.70,5,no\n",
                encoding="utf-8",
            )
            spot = ["ts,c"]
            for second in range(open_ts - 2000, close + 1, 1):
                price = 100 * math.exp(0.00002 * math.sin(second / 30))
                spot.append(f"{second},{price}")
            (root / "spot.csv").write_text("\n".join(spot) + "\n", encoding="utf-8")
            code = backtest_main(
                ["--data", str(root), "--strategy", "favorite_maker", "--fill-model", "conservative",
                 "--out", str(root / "out"), "--start", start, "--end", end]
            )
            self.assertEqual(code, 0)
            self.assertTrue((root / "out" / "summary.md").exists())
            self.assertTrue((root / "out" / "summary.json").exists())
            result = replay(
                load_dataset(root),
                strategy="favorite_maker",
                fill_model=FillModel.from_name("conservative"),
                policy=CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0),
            )
        cutoff = close - 50
        self.assertTrue(result.n_orders >= 1)
        self.assertTrue(all(ts < cutoff for ts in result.order_times))
        self.assertTrue(result.n_cancels_at_cutoff >= 1)
        self.assertEqual(result.fills, [])


if __name__ == "__main__":
    unittest.main()
