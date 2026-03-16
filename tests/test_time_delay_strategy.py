"""
tests/test_time_delay_strategy.py – Unit tests for the reddit_time_delay strategy.

Covers all six required cases from the problem statement:
  1. No trade when minutes_to_expiry > TRIGGER_MINUTE_REMAINING and no position.
  2. Enter YES when minutes_to_expiry <= TRIGGER_MINUTE_REMAINING, up_price >= TRIGGER_POINT_PRICE,
     and no previous trade in this window.
  3. Enter NO under the symmetric conditions for down_price.
  4. No trade when last_trade_window_id == current_window_id and MAX_TRADES_PER_WINDOW == 1.
  5. Exit position when current_position_side == "YES" and up_price <= EXIT_POINT_PRICE.
  6. Exit position when current_position_side == "NO" and down_price <= EXIT_POINT_PRICE.

Plus additional boundary and edge cases.
"""
import types
import unittest

from strategy import decide_trade_time_delay, decide_trade


def _make_cfg(
    strategy_mode="reddit_time_delay",
    trigger_point_price=0.90,
    exit_point_price=0.40,
    trigger_minute_remaining=14,
    max_trades_per_window=1,
    base_size=1,
):
    """Return a SimpleNamespace acting as a config object for the time-delay strategy."""
    return types.SimpleNamespace(
        STRATEGY_MODE=strategy_mode,
        TRIGGER_POINT_PRICE=trigger_point_price,
        EXIT_POINT_PRICE=exit_point_price,
        TRIGGER_MINUTE_REMAINING=trigger_minute_remaining,
        MAX_TRADES_PER_WINDOW=max_trades_per_window,
        BASE_SIZE=base_size,
    )


WINDOW_A = "2025-01-01T15:15:00+00:00"
WINDOW_B = "2025-01-01T15:30:00+00:00"


class TestTimeDelayNoPosition(unittest.TestCase):
    """Case 1: No open position – timing and trigger checks."""

    def setUp(self):
        self.cfg = _make_cfg()

    # ── Too early ──────────────────────────────────────────────────────────────

    def test_no_trade_when_too_many_minutes_remain(self):
        """Requirement 1: no trade when minutes_to_expiry > TRIGGER_MINUTE_REMAINING."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=15,          # > 14
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertIsNone(size)

    def test_no_trade_at_exact_trigger_boundary_plus_one(self):
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=15,          # one more than TRIGGER_MINUTE_REMAINING
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    # ── Exact boundary – armed ─────────────────────────────────────────────────

    def test_trade_allowed_at_exact_trigger_minute(self):
        """Armed exactly when minutes_to_expiry == TRIGGER_MINUTE_REMAINING."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=14,          # == TRIGGER_MINUTE_REMAINING
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_YES")

    # ── Enter YES ─────────────────────────────────────────────────────────────

    def test_enter_yes_when_up_price_at_trigger(self):
        """Requirement 2: Enter YES when up_price >= TRIGGER_POINT_PRICE, down_price below."""
        action, size = decide_trade_time_delay(
            up_price=0.90,
            down_price=0.10,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_YES")
        self.assertEqual(size, 1)

    def test_enter_yes_returns_base_size(self):
        cfg = _make_cfg(base_size=3)
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_YES")
        self.assertEqual(size, 3)

    # ── Enter NO ──────────────────────────────────────────────────────────────

    def test_enter_no_when_down_price_at_trigger(self):
        """Requirement 3: Enter NO when down_price >= TRIGGER_POINT_PRICE, up_price below."""
        action, size = decide_trade_time_delay(
            up_price=0.10,
            down_price=0.90,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_NO")
        self.assertEqual(size, 1)

    def test_enter_no_returns_base_size(self):
        cfg = _make_cfg(base_size=5)
        action, size = decide_trade_time_delay(
            up_price=0.05,
            down_price=0.95,
            minutes_to_expiry=2,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_NO")
        self.assertEqual(size, 5)

    # ── Neither / both qualify ─────────────────────────────────────────────────

    def test_no_trade_when_both_prices_above_trigger(self):
        action, size = decide_trade_time_delay(
            up_price=0.92,
            down_price=0.91,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_no_trade_when_neither_price_reaches_trigger(self):
        action, size = decide_trade_time_delay(
            up_price=0.70,
            down_price=0.30,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")


class TestTimeDelayWindowLimit(unittest.TestCase):
    """Requirement 4: per-window trade limit enforcement."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_no_trade_when_already_traded_this_window(self):
        """Requirement 4: no second entry in the same window when MAX_TRADES_PER_WINDOW == 1."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,  # already traded this window
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")
        self.assertIsNone(size)

    def test_trade_allowed_in_new_window_after_previous(self):
        """A new window resets the per-window limit."""
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_B,    # different window
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_YES")

    def test_trade_allowed_when_max_trades_greater_than_one(self):
        """With MAX_TRADES_PER_WINDOW > 1, same-window entries are allowed."""
        cfg = _make_cfg(max_trades_per_window=2)
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,  # same window, but limit is 2
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_YES")

    def test_trade_allowed_when_no_prior_trade(self):
        action, size = decide_trade_time_delay(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=self.cfg,
        )
        self.assertEqual(action, "ENTER_YES")


class TestTimeDelayExitYes(unittest.TestCase):
    """Requirement 5: exit when holding YES and up_price drops to/below EXIT_POINT_PRICE."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_exit_yes_when_up_price_at_exit_threshold(self):
        """Requirement 5: exit YES when up_price == EXIT_POINT_PRICE."""
        action, size = decide_trade_time_delay(
            up_price=0.40,
            down_price=0.60,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")
        self.assertIsNone(size)

    def test_exit_yes_when_up_price_below_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.20,
            down_price=0.80,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_no_exit_yes_when_up_price_above_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.60,
            down_price=0.40,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")


class TestTimeDelayExitNo(unittest.TestCase):
    """Requirement 6: exit when holding NO and down_price drops to/below EXIT_POINT_PRICE."""

    def setUp(self):
        self.cfg = _make_cfg()

    def test_exit_no_when_down_price_at_exit_threshold(self):
        """Requirement 6: exit NO when down_price == EXIT_POINT_PRICE."""
        action, size = decide_trade_time_delay(
            up_price=0.60,
            down_price=0.40,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")
        self.assertIsNone(size)

    def test_exit_no_when_down_price_below_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.80,
            down_price=0.15,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_no_exit_no_when_down_price_above_exit_threshold(self):
        action, size = decide_trade_time_delay(
            up_price=0.40,
            down_price=0.60,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=self.cfg,
        )
        self.assertEqual(action, "NO_TRADE")


class TestDecideTradeWrapper(unittest.TestCase):
    """decide_trade() wrapper routing tests."""

    def test_routes_to_time_delay_when_mode_set(self):
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        action, size = decide_trade(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "ENTER_YES")

    def test_returns_no_trade_for_fee_aware_model_mode(self):
        """For fee_aware_model mode the wrapper defers to generate_signal; returns NO_TRADE."""
        cfg = _make_cfg(strategy_mode="fee_aware_model")
        action, size = decide_trade(
            up_price=0.95,
            down_price=0.05,
            minutes_to_expiry=5,
            current_position_side=None,
            current_window_id=WINDOW_A,
            last_trade_window_id=None,
            cfg=cfg,
        )
        self.assertEqual(action, "NO_TRADE")

    def test_wrapper_exit_yes_via_time_delay(self):
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        action, size = decide_trade(
            up_price=0.30,
            down_price=0.70,
            minutes_to_expiry=5,
            current_position_side="YES",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")

    def test_wrapper_exit_no_via_time_delay(self):
        cfg = _make_cfg(strategy_mode="reddit_time_delay")
        action, size = decide_trade(
            up_price=0.70,
            down_price=0.30,
            minutes_to_expiry=5,
            current_position_side="NO",
            current_window_id=WINDOW_A,
            last_trade_window_id=WINDOW_A,
            cfg=cfg,
        )
        self.assertEqual(action, "EXIT_POSITION")


if __name__ == "__main__":
    unittest.main()
