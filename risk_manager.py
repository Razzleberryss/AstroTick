"""
risk_manager.py  -  Position sizing and trade gating.

Responsibilities:
  - Check max open positions
  - Check total exposure vs MAX_TOTAL_EXPOSURE
  - Check available balance vs MAX_TRADE_DOLLARS
  - Calculate contract count for a given dollar risk
  - Log every trade decision to CSV
  - Detect if we already have a position in this market (avoid doubling)
"""

import csv
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import config
from strategy import Signal

log = logging.getLogger(__name__)


class RiskManager:
    """Stateful risk guard. One instance lives for the duration of the bot run."""

    def __init__(self):
        self._ensure_log_file()

    # ── Trade approval ───────────────────────────────────────────────────────────

    def approve_trade(
        self,
        signal: Signal,
        balance: float,
        positions: list,
        market_ticker: str,
    ) -> tuple[bool, str]:
        """
        Returns (approved: bool, reason: str).
        Gates the trade against all risk limits.
        """
        # 1. Already have a position in this market?
        existing = [p for p in positions if p.get("ticker") == market_ticker]
        if existing:
            return False, f"Already have a position in {market_ticker}"

        # 2. Max open positions
        open_count = len(positions)
        if open_count >= config.MAX_OPEN_POSITIONS:
            return False, f"At max open positions ({open_count}/{config.MAX_OPEN_POSITIONS})"

        # 3. Available balance check
        if balance < config.MAX_TRADE_DOLLARS:
            return False, f"Insufficient balance ${balance:.2f} < ${config.MAX_TRADE_DOLLARS}"

        # 4. Total exposure check
        # Estimate current deployed capital: count all positions * avg price
        deployed = self._estimate_deployed(positions)
        if deployed + config.MAX_TRADE_DOLLARS > config.MAX_TOTAL_EXPOSURE:
            return False, (
                f"Would exceed MAX_TOTAL_EXPOSURE: "
                f"${deployed:.2f} + ${config.MAX_TRADE_DOLLARS:.2f} > ${config.MAX_TOTAL_EXPOSURE:.2f}"
            )

        # 5. Price sanity
        p = signal.price_cents
        if not (config.MIN_CONTRACT_PRICE_CENTS <= p <= config.MAX_CONTRACT_PRICE_CENTS):
            return False, f"Price {p}c outside allowed range [{config.MIN_CONTRACT_PRICE_CENTS},{config.MAX_CONTRACT_PRICE_CENTS}]"

        return True, "All risk checks passed"

    # ── Position sizing ───────────────────────────────────────────────────────────

    def calculate_contracts(
        self,
        price_cents: int,
        max_dollars: Optional[float] = None,
    ) -> int:
        """
        Calculate how many contracts to buy.

        Each contract costs `price_cents` cents.
        We never risk more than MAX_TRADE_DOLLARS per trade.
        Returns at least 1 or 0 if the price exceeds the budget.
        """
        budget_cents = (max_dollars or config.MAX_TRADE_DOLLARS) * 100
        if price_cents <= 0:
            return 0
        contracts = int(budget_cents // price_cents)
        return max(0, contracts)

    # ── Trade logging ─────────────────────────────────────────────────────────────

    def log_trade(
        self,
        ticker: str,
        side: str,
        contracts: int,
        price_cents: int,
        confidence: float,
        dry_run: bool,
        order_id: Optional[str] = None,
        reason: str = "",
    ):
        """Append one row to the CSV trade log."""
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "ticker": ticker,
            "side": side,
            "contracts": contracts,
            "price_cents": price_cents,
            "cost_dollars": round(contracts * price_cents / 100, 2),
            "confidence": round(confidence, 4),
            "dry_run": dry_run,
            "order_id": order_id or "",
            "reason": reason,
        }
        with open(config.TRADE_LOG_FILE, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(row.keys()))
            writer.writerow(row)
        log.info("Trade logged: %s", row)

    # ── Helpers ───────────────────────────────────────────────────────────────────

    @staticmethod
    def _estimate_deployed(positions: list) -> float:
        """Rough estimate of total dollars currently at risk in open positions."""
        total_cents = 0
        for pos in positions:
            # Kalshi position: quantity * average cost
            qty = abs(pos.get("position", 0))
            avg_price = pos.get("average_price", 50)  # default 50c
            total_cents += qty * avg_price
        return total_cents / 100

    def _ensure_log_file(self):
        """Create the CSV with headers if it doesn't already exist."""
        if not os.path.exists(config.TRADE_LOG_FILE):
            headers = [
                "timestamp", "ticker", "side", "contracts",
                "price_cents", "cost_dollars", "confidence",
                "dry_run", "order_id", "reason",
            ]
            with open(config.TRADE_LOG_FILE, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=headers)
                writer.writeheader()
            log.info("Created trade log: %s", config.TRADE_LOG_FILE)
