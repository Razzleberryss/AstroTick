"""Per-market cap, overlapping-window cap, and the daily-loss kill."""

import os
import unittest
from decimal import Decimal

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from engine.risk import Exposure, RiskBook, RiskLimits


def _book(**overrides) -> RiskBook:
    limits = RiskLimits(
        max_contracts_per_market=Decimal("1"),
        max_contracts_total=Decimal("2"),
        max_dollars_total=Decimal("5"),
        max_daily_loss_dollars=Decimal("10"),
        stale_feed_seconds=15,
    )
    return RiskBook(limits)


def _exposure(**overrides) -> Exposure:
    base = dict(
        contracts_on_market=Decimal("0"),
        contracts_total=Decimal("0"),
        dollars_total=Decimal("0"),
        daily_realized_pnl=Decimal("0"),
        feed_age_seconds=1.0,
    )
    base.update(overrides)
    return Exposure(**base)


class TestRiskLimits(unittest.TestCase):
    def test_per_market_cap(self):
        ok, reason = _book().check_order(
            _exposure(contracts_on_market=Decimal("1")),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.80"),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "max_contracts_per_market")

    def test_overlapping_windows_share_one_cap(self):
        # One contract already working on an earlier window.
        ok, reason = _book().check_order(
            _exposure(contracts_on_market=Decimal("0"), contracts_total=Decimal("2")),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.50"),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "max_contracts_total")

    def test_dollar_cap(self):
        ok, reason = _book().check_order(
            _exposure(dollars_total=Decimal("4.50")),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.80"),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "max_dollars_total")

    def test_daily_loss_kill(self):
        ok, reason = _book().check_order(
            _exposure(daily_realized_pnl=Decimal("-10")),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.50"),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "daily_loss_kill")
        ok, reason = _book().check_order(
            _exposure(daily_realized_pnl=Decimal("-9.99")),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.50"),
        )
        self.assertTrue(ok)

    def test_stale_feed_blocks(self):
        ok, reason = _book().check_order(
            _exposure(feed_age_seconds=15.1),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.40"),
        )
        self.assertFalse(ok)
        self.assertEqual(reason, "stale_feed")
        ok, reason = _book().check_order(
            _exposure(feed_age_seconds=None),
            add_contracts=Decimal("1"),
            add_dollars=Decimal("0.40"),
        )
        self.assertEqual(reason, "stale_feed")


if __name__ == "__main__":
    unittest.main()
