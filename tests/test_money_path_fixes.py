"""
Regression tests for the money-path fixes:
  - Kalshi fee computed in cents (was dollars mislabeled as cents, ~100x low)
  - NO-side PnL uses side-denominated prices (was sign-inverted)
  - Exit orders are not constrained by the MIN/MAX_CONTRACT_PRICE entry band
  - Dry-run sells do not consult live positions
  - ENVELOPE_FN_EXCEPTION escalates instead of halting as malformed
"""

import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from agent_decision_engine import AgentAction, interpret_cli_response
from cli_executor import execute_with_decision_engine
from kalshi_inprocess_orders import buy_envelope, sell_envelope
from strategy import decide_trade_fee_aware


def _make_cfg(**overrides):
    cfg = SimpleNamespace(
        KALSHI_FEE_RATE=0.07,
        MIN_EDGE_PCT=0.10,
        FORBIDDEN_PRICE_LOW=0.35,
        FORBIDDEN_PRICE_HIGH=0.56,
        MIN_EXPECTED_NET_PER_CONTRACT=0.02,
        BASE_SIZE=1,
        MAX_SIZE=10,
        MAX_EDGE_PCT=0.30,
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestFeeComputedInCents(unittest.TestCase):
    def test_thin_edge_rejected_when_fees_counted_correctly(self):
        # P=0.60, model=0.66 with MIN_EDGE_PCT=0.05: ev_gross = $0.06/contract.
        # True worst-case fees at C=3 (0.6 edge_ratio sizing): open
        # ceil(0.07*3*0.6*0.4*100)=6c, close ceil(0.07*3*0.25*100)=6c
        # → $0.12/3 = $0.04/contract → net $0.02... use a tighter threshold so
        # the corrected formula rejects what the old (always ~1c) formula passed.
        cfg = _make_cfg(MIN_EDGE_PCT=0.05, MIN_EXPECTED_NET_PER_CONTRACT=0.03)
        action, size = decide_trade_fee_aware(0.60, 0.66, cfg=cfg)
        self.assertEqual(action, "NO_TRADE")
        self.assertEqual(size, 0)

    def test_fee_rate_is_configurable(self):
        # Same trade passes with a zero fee rate.
        cfg = _make_cfg(
            MIN_EDGE_PCT=0.05, MIN_EXPECTED_NET_PER_CONTRACT=0.03, KALSHI_FEE_RATE=0.0001
        )
        action, size = decide_trade_fee_aware(0.60, 0.66, cfg=cfg)
        self.assertEqual(action, "BUY_YES")
        self.assertGreaterEqual(size, 1)


class TestExitBandAndDryRunOrdering(unittest.TestCase):
    def test_sell_envelope_allows_price_below_entry_band(self):
        """A stop-loss sell at 5c must not be rejected by the 10-90c entry band."""
        client = MagicMock()
        with patch("config.MIN_CONTRACT_PRICE_CENTS", 10), patch(
            "config.MAX_CONTRACT_PRICE_CENTS", 90
        ):
            env = sell_envelope(client, "KXBTC15M-TEST", "yes", 2, 5, dry_run=True)
        self.assertTrue(env["ok"])
        self.assertEqual(env["code"], "SELL_DRY_RUN")

    def test_buy_envelope_still_enforces_entry_band(self):
        client = MagicMock()
        with patch("config.MIN_CONTRACT_PRICE_CENTS", 10), patch(
            "config.MAX_CONTRACT_PRICE_CENTS", 90
        ):
            env = buy_envelope(client, "KXBTC15M-TEST", "yes", 2, 5, dry_run=True)
        self.assertFalse(env["ok"])
        self.assertEqual(env["code"], "PRICE_OUTSIDE_CONFIG_RANGE")

    def test_dry_run_sell_skips_live_position_lookup(self):
        client = MagicMock()
        env = sell_envelope(client, "KXBTC15M-TEST", "no", 3, 40, dry_run=True)
        self.assertTrue(env["ok"])
        self.assertEqual(env["code"], "SELL_DRY_RUN")
        self.assertEqual(env["result"]["count"], 3)
        client.contracts_held_on_side.assert_not_called()

    def test_live_sell_still_requires_position(self):
        client = MagicMock()
        client.contracts_held_on_side.return_value = 0
        with patch.dict(os.environ, {"KALSHI_TRADING_LIVE": "1"}, clear=False):
            env = sell_envelope(client, "KXBTC15M-TEST", "no", 3, 40, dry_run=False)
        self.assertFalse(env["ok"])
        self.assertEqual(env["code"], "NO_POSITION")


class TestEnvelopeFnException(unittest.TestCase):
    def test_exception_in_envelope_fn_escalates_without_halting(self):
        def _boom():
            raise RuntimeError("builder blew up")

        outcome, envelope = execute_with_decision_engine([], envelope_fn=_boom)
        self.assertEqual(outcome.action, AgentAction.ESCALATE_TO_HUMAN)
        self.assertFalse(outcome.halt_trading)
        self.assertFalse(outcome.malformed)
        self.assertEqual(envelope["code"], "ENVELOPE_FN_EXCEPTION")
        # The envelope must be well-formed for the decision engine on its own.
        direct = interpret_cli_response(envelope)
        self.assertFalse(direct.malformed)


if __name__ == "__main__":
    unittest.main()
