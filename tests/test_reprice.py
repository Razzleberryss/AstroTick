"""Reprice threshold, stale orders, and size changes that keep queue priority."""

import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from engine.orders import QuoteIntent, TrackedQuote, plan_quotes


def _resting(price="0.80", remaining="2", fv=0.70, age_seconds=10) -> TrackedQuote:
    now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
    return TrackedQuote(
        order_id="ord-1",
        client_order_id="c1",
        ticker="KXBTC15M-T",
        contract_side="yes",
        action="buy",
        price=Decimal(price),
        remaining=Decimal(remaining),
        fair_value_yes=fv,
        placed_at=now - timedelta(seconds=age_seconds),
        strategy="favorite_maker",
        reason="resting",
    )


def _want(price="0.81", count="2", fv=0.70) -> QuoteIntent:
    return QuoteIntent(
        strategy="favorite_maker",
        ticker="KXBTC15M-T",
        contract_side="yes",
        action="buy",
        limit_price=Decimal(price),
        count=Decimal(count),
        reason="still the favorite",
        fair_value_yes=fv,
    )


NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


def _kinds(actions):
    return [type(item).__name__ for item in actions]


class TestReprice(unittest.TestCase):
    def test_one_cent_fair_value_move_keeps_the_queue(self):
        actions, _notes = plan_quotes(
            [_resting(fv=0.70)],
            [_want(price="0.81", fv=0.71)],
            fair_value_yes=0.71,
            now=NOW,
            reprice_threshold=Decimal("0.02"),
        )
        self.assertEqual(_kinds(actions), ["Keep"])

    def test_two_cent_fair_value_move_reprices(self):
        actions, _notes = plan_quotes(
            [_resting(price="0.80", fv=0.70)],
            [_want(price="0.82", fv=0.72)],
            fair_value_yes=0.72,
            now=NOW,
            reprice_threshold=Decimal("0.02"),
        )
        self.assertEqual(_kinds(actions), ["Cancel", "Place"])
        self.assertEqual(actions[0].reason, "fair_value_moved")
        self.assertEqual(actions[1].intent.limit_price, Decimal("0.82"))

    def test_stale_order_reprices_even_if_fair_value_is_unchanged(self):
        actions, _notes = plan_quotes(
            [_resting(age_seconds=400, fv=0.70)],
            [_want(price="0.80", fv=0.70)],
            fair_value_yes=0.70,
            now=NOW,
            stale_after=timedelta(seconds=300),
        )
        self.assertEqual(_kinds(actions), ["Cancel", "Place"])
        self.assertEqual(actions[0].reason, "stale_order")

    def test_scale_down_uses_decrease_not_a_new_order(self):
        actions, _notes = plan_quotes(
            [_resting(remaining="5", price="0.80", fv=0.70)],
            [_want(price="0.80", count="3", fv=0.70)],
            fair_value_yes=0.70,
            now=NOW,
        )
        self.assertEqual(_kinds(actions), ["Decrease"])
        self.assertEqual(actions[0].reduce_to, Decimal("3"))

    def test_added_size_is_a_separate_order(self):
        actions, _notes = plan_quotes(
            [_resting(remaining="3", price="0.80", fv=0.70)],
            [_want(price="0.80", count="5", fv=0.70)],
            fair_value_yes=0.70,
            now=NOW,
        )
        self.assertEqual(_kinds(actions), ["Keep", "Place"])
        self.assertEqual(actions[1].intent.count, Decimal("2"))

    def test_two_price_levels_collapse_to_one(self):
        aggressive = _want(price="0.84", count="1")
        passive = QuoteIntent(
            strategy="other",
            ticker="KXBTC15M-T",
            contract_side="yes",
            action="buy",
            limit_price=Decimal("0.80"),
            count=Decimal("1"),
            reason="less aggressive",
            fair_value_yes=0.7,
        )
        actions, notes = plan_quotes(
            [],
            [aggressive, passive],
            fair_value_yes=0.7,
            now=NOW,
        )
        places = [item for item in actions if type(item).__name__ == "Place"]
        self.assertEqual(len(places), 1)
        self.assertEqual(places[0].intent.limit_price, Decimal("0.80"))
        self.assertTrue(notes)


if __name__ == "__main__":
    unittest.main()
