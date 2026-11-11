"""State file survives a restart and reconciles with an API snapshot."""

import os
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from engine.state_store import BotState, StateStore, TrackedOrder, TrackedPosition


class TestStateStore(unittest.TestCase):
    def test_round_trip(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "state.json"
            store = StateStore(path)
            store.state = BotState()
            store.state.orders.append(
                TrackedOrder(
                    order_id="ord-1",
                    client_order_id="c1",
                    ticker="KXBTC15M-A",
                    contract_side="yes",
                    action="buy",
                    price="0.80",
                    count="1.00",
                    remaining="1.00",
                    fair_value_yes=0.77,
                    placed_at="2026-11-11T12:00:00+00:00",
                    strategy="favorite_maker",
                    reason="join",
                    status="resting",
                    mode="live",
                )
            )
            store.state.positions["KXBTC15M-A"] = TrackedPosition(
                ticker="KXBTC15M-A", side="yes", contracts="1.00", avg_price="0.80"
            )
            store.add_realized(Decimal("-1.25"), datetime(2026, 11, 11, tzinfo=UTC))
            store.set_balance(Decimal("25.50"))
            store.save()

            reloaded = StateStore(path)
            reloaded.load()
            self.assertEqual(reloaded.state.orders[0].order_id, "ord-1")
            self.assertEqual(reloaded.state.orders[0].fair_value_yes, 0.77)
            self.assertEqual(reloaded.state.positions["KXBTC15M-A"].contracts, "1.00")
            self.assertEqual(reloaded.state.daily_pnl("2026-11-11"), Decimal("-1.25"))
            self.assertEqual(reloaded.state.balance_dollars, "25.50")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_reconcile_adopts_api_position_and_drops_missing_orders(self):
        with TemporaryDirectory() as tmp:
            store = StateStore(Path(tmp) / "state.json")
            store.state.orders.append(
                TrackedOrder(
                    order_id="gone",
                    client_order_id="c",
                    ticker="KXBTC15M-A",
                    contract_side="yes",
                    action="buy",
                    price="0.40",
                    count="1.00",
                    remaining="1.00",
                    fair_value_yes=0.5,
                    placed_at="2026-11-11T12:00:00+00:00",
                    strategy="favorite_maker",
                    reason="old",
                    status="resting",
                    mode="live",
                )
            )
            store.state.positions["KXBTC15M-A"] = TrackedPosition(
                ticker="KXBTC15M-A", side="yes", contracts="1.00", avg_price="0.40"
            )
            notes = store.reconcile(
                api_orders=[{
                    "order_id": "new-1",
                    "ticker": "KXBTC15M-B",
                    "status": "resting",
                    "remaining_count_fp": "1.00",
                    "price": "0.70",
                    "outcome_side": "yes",
                }],
                api_positions=[{
                    "ticker": "KXBTC15M-A",
                    "position_fp": "2.00",
                }],
            )
            self.assertEqual(store.state.orders[0].status, "cancelled")
            adopted = [order for order in store.state.orders if order.order_id == "new-1"]
            self.assertEqual(len(adopted), 1)
            self.assertEqual(store.state.positions["KXBTC15M-A"].contracts, "2.00")
            self.assertEqual(store.state.positions["KXBTC15M-A"].avg_price, "0.40")
            self.assertTrue(any("2.00" in note for note in notes))

    def test_settlement_realizes_pnl_and_clears_the_position(self):
        store = StateStore("/tmp/does-not-matter-until-save.json")
        store.state.positions["KXBTC15M-A"] = TrackedPosition(
            ticker="KXBTC15M-A", side="yes", contracts="1", avg_price="0.80"
        )
        now = datetime(2026, 11, 11, 13, 0, tzinfo=UTC)
        pnl = store.note_settlement("KXBTC15M-A", "no", now)
        self.assertEqual(pnl, Decimal("-0.80"))
        self.assertNotIn("KXBTC15M-A", store.state.positions)
        self.assertEqual(store.state.daily_pnl("2026-11-11"), Decimal("-0.80"))


if __name__ == "__main__":
    unittest.main()
