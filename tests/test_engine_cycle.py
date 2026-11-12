"""Paper cycle logs fair value vs the book and does not quote when strategies are off."""

import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from engine.book import Book
from engine.clock_watch import BookCache
from engine.cutoff import CutoffPolicy
from engine.decision_log import DecisionLog
from engine.index_series import IndexSeries
from engine.orders import OrderGateway, RecordingSender
from engine.risk import RiskBook, RiskLimits
from engine.runner import CycleData, handle_stream_event, run_cycle
from engine.settings import EngineSettings
from engine.state_store import StateStore
from engine.strategies import ExtremeFade, FavoriteMaker, MarketView


class TestPaperCycle(unittest.TestCase):
    def test_disabled_strategies_log_fair_value_and_send_nothing(self):
        now = datetime(2026, 11, 11, 16, 0, tzinfo=UTC)
        points = [(1_700_000_000_000 + i * 1000, 100_000 + (i % 2)) for i in range(40)]
        series = IndexSeries(points=points, step_seconds=1.0, per_second=True)
        close = (now + timedelta(seconds=600)).strftime("%Y-%m-%dT%H:%M:%SZ")
        data = CycleData(
            now=now,
            markets=[{
                "ticker": "KXBTC15M-26NOV111600-00",
                "close_time": close,
                "open_time": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "status": "open",
                "floor_strike": "102000",
                "event_ticker": "KXBTC15M-26NOV111600",
            }],
            books={"KXBTC15M-26NOV111600-00": Book(Decimal("0.90"), Decimal("0.05"))},
            indexes={"KXBTC15M-26NOV111600-00": series},
            fee_type="quadratic",
            fee_multiplier="1",
            feed_age_seconds=1.0,
            feed_disconnected=False,
            discovery_source="close_window",
            shard_ok=False,
            shard_reason="shard_balance_unknown",
        )
        with TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            decisions = DecisionLog(Path(tmp) / "decisions.jsonl")
            sender = RecordingSender()
            gateway = OrderGateway(
                sender=sender,
                risk=RiskBook(RiskLimits()),
                policy=CutoffPolicy(clock_skew_margin_seconds=0),
                mode="paper",
            )
            records = run_cycle(
                data,
                settings=EngineSettings(),
                store=store,
                gateway=gateway,
                decisions=decisions,
            )
            logged = decisions.path.read_text(encoding="utf-8")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["action"], "observe")
        self.assertEqual(records[0]["reason"], "strategies_disabled")
        self.assertIsNotNone(records[0]["fair_value_yes"])
        self.assertLess(records[0]["fair_value_yes"], 0.5)
        self.assertEqual(records[0]["book"]["yes_bid"], "0.90")
        self.assertEqual(records[0]["inputs"]["fee_type"], "quadratic")
        self.assertFalse(records[0]["inputs"]["maker_fee_applies"])
        self.assertEqual(sender.creates, [])
        self.assertNotIn("api_key", logged)

    def test_stream_reset_and_disconnect(self):
        cache = BookCache()
        sender = RecordingSender()
        gateway = OrderGateway(
            sender=sender,
            risk=RiskBook(RiskLimits()),
            policy=CutoffPolicy(),
            mode="paper",
        )
        from engine.orders import TrackedQuote

        resting = [TrackedQuote(
            order_id="p1",
            client_order_id="c",
            ticker="KXBTC15M-T",
            contract_side="yes",
            action="buy",
            price=Decimal("0.8"),
            remaining=Decimal("1"),
            fair_value_yes=0.8,
            placed_at=datetime.now(UTC),
            strategy="favorite_maker",
            reason="resting",
        )]
        cache.on_message({
            "type": "orderbook_snapshot",
            "market_ticker": "KXBTC15M-T",
            "clock": 10,
        })
        self.assertEqual(
            handle_stream_event(cache, gateway, resting, {"type": "orderbook_delta", "market_ticker": "KXBTC15M-T", "clock": 3}),
            "resnapshot",
        )
        self.assertEqual(cache.books, {})
        self.assertTrue(cache.needs_snapshot)
        self.assertEqual(
            handle_stream_event(cache, gateway, resting, {"type": "disconnect"}),
            "cancelled",
        )
        self.assertEqual(sender.cancels, [("p1", "KXBTC15M-T")])


class TestStrategyTriggers(unittest.TestCase):
    def test_extreme_fade_off_by_default_and_joins_the_cheap_bid_when_on(self):
        view = MarketView(
            ticker="KXBTC15M-T",
            book=Book(Decimal("0.90"), Decimal("0.05")),
            fair_value_yes=0.70,
            tau_seconds=180,
        )
        self.assertIsNone(ExtremeFade().propose(view))
        quote = ExtremeFade(enabled=True).propose(view)
        self.assertIsNotNone(quote)
        self.assertEqual(quote.contract_side, "no")
        self.assertEqual(quote.limit_price, Decimal("0.05"))
        self.assertIn("extreme fade", quote.reason)

    def test_favorite_maker_band_and_default_off(self):
        inside = MarketView(
            ticker="KXBTC15M-T",
            book=Book(Decimal("0.80"), Decimal("0.18")),
            fair_value_yes=0.78,
            tau_seconds=180,
        )
        outside = MarketView(
            ticker="KXBTC15M-T",
            book=Book(Decimal("0.60"), Decimal("0.38")),
            fair_value_yes=0.6,
            tau_seconds=180,
        )
        self.assertIsNone(FavoriteMaker().propose(inside))
        self.assertIsNotNone(FavoriteMaker(enabled=True).propose(inside))
        self.assertIsNone(FavoriteMaker(enabled=True).propose(outside))


if __name__ == "__main__":
    unittest.main()
