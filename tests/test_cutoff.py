"""T-50s cutoff: pure policy, gateway, and the SDK submit path."""

import os
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

import config
from engine.cutoff import (
    CUTOFF_FLOOR_SECONDS,
    CutoffBlocked,
    CutoffPolicy,
    assert_submission_allowed,
    register_close,
    reset_registry,
    trading_blocked,
)
from engine.orders import OrderGateway, QuoteIntent, RecordingSender, TrackedQuote
from engine.risk import Exposure, RiskBook, RiskLimits


def _utc(seconds_from_now: float, now: datetime) -> datetime:
    return now + timedelta(seconds=seconds_from_now)


class TestCutoffPolicy(unittest.TestCase):
    def test_floor_cannot_be_weakened(self):
        with self.assertRaises(ValueError):
            CutoffPolicy(cutoff_seconds=45, clock_skew_margin_seconds=0)
        self.assertEqual(CUTOFF_FLOOR_SECONDS, 50.0)

    def test_blocks_at_t_minus_50_and_inside(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0)
        close = _utc(50, now)
        self.assertTrue(trading_blocked(close, now, policy))
        self.assertTrue(trading_blocked(_utc(49, now), now, policy))
        self.assertTrue(trading_blocked(_utc(0, now), now, policy))
        self.assertTrue(trading_blocked(_utc(-5, now), now, policy))
        self.assertFalse(trading_blocked(_utc(50.1, now), now, policy))

    def test_clock_skew_margin_blocks_earlier(self):
        now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=5)
        # 50 + 5 = 55s. A slow local clock is treated as already inside the window.
        self.assertTrue(trading_blocked(_utc(55, now), now, policy))
        self.assertTrue(trading_blocked(_utc(50, now), now, policy))
        self.assertFalse(trading_blocked(_utc(55.1, now), now, policy))

    def test_missing_close_and_amend_are_blocked_cancels_are_not(self):
        reset_registry()
        now = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)
        policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0)
        with self.assertRaises(CutoffBlocked):
            assert_submission_allowed("KXBTC15M-X", "create", now=now, policy=policy)
        with self.assertRaises(CutoffBlocked):
            assert_submission_allowed("KXBTC15M-X", "amend", now=now, policy=policy)
        with self.assertRaises(CutoffBlocked):
            assert_submission_allowed("KXBTC15M-X", "decrease", now=now, policy=policy)
        assert_submission_allowed("KXBTC15M-X", "cancel", now=now, policy=policy)
        register_close("KXBTC15M-X", _utc(10, now))
        with self.assertRaises(CutoffBlocked):
            assert_submission_allowed("KXBTC15M-X", "amend", now=now, policy=policy)
        # Cancels stay allowed inside the window.
        assert_submission_allowed("KXBTC15M-X", "cancel", now=now, policy=policy)
        reset_registry()


class TestGatewayCutoff(unittest.TestCase):
    def test_strategy_intent_cannot_submit_inside_the_window(self):
        now = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
        policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=2)
        sender = RecordingSender()
        gateway = OrderGateway(
            sender=sender,
            risk=RiskBook(RiskLimits()),
            policy=policy,
            mode="paper",
        )
        intent = QuoteIntent(
            strategy="extreme_fade",
            ticker="KXBTC15M-WINDOW",
            contract_side="no",
            action="buy",
            limit_price=Decimal("0.10"),
            count=Decimal("1"),
            reason="try to bypass the cutoff",
            fair_value_yes=0.2,
        )
        resting = TrackedQuote(
            order_id="paper-9",
            client_order_id="c",
            ticker="KXBTC15M-WINDOW",
            contract_side="no",
            action="buy",
            price=Decimal("0.10"),
            remaining=Decimal("1"),
            fair_value_yes=0.2,
            placed_at=now - timedelta(seconds=30),
            strategy="extreme_fade",
            reason="already resting",
        )
        # 40s to close is inside 50+2.
        result = gateway.sync(
            ticker="KXBTC15M-WINDOW",
            close_time=_utc(40, now),
            now=now,
            desired=[intent],
            fair_value_yes=0.2,
            resting=[resting],
            exposure=Exposure(
                contracts_on_market=Decimal("0"),
                contracts_total=Decimal("0"),
                dollars_total=Decimal("0"),
                daily_realized_pnl=Decimal("0"),
                feed_age_seconds=0,
            ),
        )
        self.assertEqual(result["action"], "cutoff")
        self.assertEqual(sender.creates, [])
        self.assertEqual(sender.amends, [])
        self.assertEqual(sender.decreases, [])
        self.assertEqual(sender.cancels, [("paper-9", "KXBTC15M-WINDOW")])

    def test_quote_outside_the_window_is_sent(self):
        now = datetime(2026, 11, 11, 15, 0, tzinfo=UTC)
        policy = CutoffPolicy(cutoff_seconds=50, clock_skew_margin_seconds=0)
        sender = RecordingSender()
        gateway = OrderGateway(
            sender=sender,
            risk=RiskBook(RiskLimits()),
            policy=policy,
            mode="paper",
        )
        intent = QuoteIntent(
            strategy="favorite_maker",
            ticker="KXBTC15M-OPEN",
            contract_side="yes",
            action="buy",
            limit_price=Decimal("0.80"),
            count=Decimal("1"),
            reason="join the favorite",
            fair_value_yes=0.8,
        )
        result = gateway.sync(
            ticker="KXBTC15M-OPEN",
            close_time=_utc(120, now),
            now=now,
            desired=[intent],
            fair_value_yes=0.8,
            resting=[],
            exposure=Exposure(
                contracts_on_market=Decimal("0"),
                contracts_total=Decimal("0"),
                dollars_total=Decimal("0"),
                daily_realized_pnl=Decimal("0"),
                feed_age_seconds=1,
            ),
        )
        self.assertEqual(len(sender.creates), 1)
        payload = sender.creates[0]["payload"]
        self.assertTrue(payload["post_only"])
        self.assertTrue(payload["cancel_order_on_pause"])
        self.assertEqual(payload["time_in_force"], "good_till_canceled")
        self.assertNotIn("expiration_time", payload)
        self.assertEqual(payload["self_trade_prevention_type"], "taker_at_cross")
        self.assertEqual(result["action"], "placed")


class TestClientCutoff(unittest.TestCase):
    def setUp(self):
        reset_registry()
        import kalshi_client as kc

        self.kc = kc
        with patch.object(kc, "_SdkConfiguration"), patch.object(
            kc, "_SdkKalshiClient"
        ) as p_client, patch.object(kc, "_read_private_key_pem", return_value="---PEM---"):
            self.sdk = MagicMock()
            p_client.return_value = self.sdk
            self.client = kc.KalshiClient()
        self.ticker = f"{config.BTC_SERIES_TICKER}-CUTOFF"

    def tearDown(self):
        reset_registry()

    def test_live_order_without_close_time_is_refused(self):
        with self.assertRaises(CutoffBlocked):
            self.client.place_order(self.ticker, "yes", 1, 50, dry_run=False)
        self.sdk.create_order_v2.assert_not_called()

    def test_live_order_inside_window_does_not_reach_the_sdk(self):
        register_close(self.ticker, datetime.now(UTC) + timedelta(seconds=10))
        with self.assertRaises(CutoffBlocked):
            self.client.place_order(self.ticker, "yes", 1, 50, dry_run=False)
        self.sdk.create_order_v2.assert_not_called()

    def test_decrease_inside_window_is_refused_cancel_is_not(self):
        register_close(self.ticker, datetime.now(UTC) + timedelta(seconds=5))
        with self.assertRaises(CutoffBlocked):
            self.client.decrease_order("ord-1", self.ticker, "1.00")
        self.sdk.decrease_order_v2.assert_not_called()
        self.sdk.cancel_order_v2.return_value = {"ok": True}
        self.client.cancel_order("ord-1")
        self.sdk.cancel_order_v2.assert_called()

    def test_live_order_outside_window_reaches_create_order_v2(self):
        register_close(self.ticker, datetime.now(UTC) + timedelta(hours=1))
        self.sdk.create_order_v2.return_value = {
            "order_id": "ord-ok",
            "fill_count": "1.00",
            "remaining_count": "0.00",
            "average_fill_price": "0.50",
        }
        resp = self.client.place_order(self.ticker, "yes", 1, 50, dry_run=False)
        self.sdk.create_order_v2.assert_called_once()
        self.assertEqual(resp["order"]["order_id"], "ord-ok")


if __name__ == "__main__":
    unittest.main()
