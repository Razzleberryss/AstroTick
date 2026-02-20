"""
bot.py  -  Main entry point for the Kalshi 15-minute BTC trader.

Usage:
    python bot.py              # runs live (DRY_RUN=true by default in .env)
    DRY_RUN=false python bot.py  # real trading

Loop logic (every LOOP_INTERVAL_SECONDS):
    1. Validate config
    2. Find active 15-min BTC market on Kalshi
    3. Fetch orderbook + account balance + open positions
    4. Generate signal (strategy.py)
    5. Risk-check the signal (risk_manager.py)
    6. Place order (or log as dry run)
    7. Sleep and repeat
"""

import logging
import signal
import sys
import time

import colorlog

import config
from kalshi_client import KalshiClient
from risk_manager import RiskManager
from strategy import generate_signal

# ── Logging setup ─────────────────────────────────────────────────────────────
def setup_logging():
    handler = colorlog.StreamHandler()
    handler.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))
    root = logging.getLogger()
    root.setLevel(getattr(logging, config.LOG_LEVEL, logging.INFO))
    root.addHandler(handler)

log = logging.getLogger("bot")

# ── Graceful shutdown ─────────────────────────────────────────────────────────
_running = True

def _handle_signal(sig, frame):
    global _running
    log.warning("Shutdown signal received — stopping after this cycle...")
    _running = False

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

# ── Core bot loop ─────────────────────────────────────────────────────────────
def run_once(client: KalshiClient, risk: RiskManager):
    """
    Execute one complete bot cycle.
    Returns True if a trade was attempted, False otherwise.
    """
    # 1. Find the active market
    market = client.get_active_btc_market()
    if not market:
        log.warning("No active BTC 15-min market found. Skipping cycle.")
        return False

    ticker = market["ticker"]
    log.info("Active market: %s | close_time=%s", ticker, market.get("close_time", "?"))

    # 2. Fetch supporting data
    try:
        orderbook = client.get_orderbook(ticker)
        balance   = client.get_balance()
        positions = client.get_positions()
    except Exception as exc:
        log.error("API fetch error: %s", exc)
        return False

    log.info("Balance: $%.2f | Open positions: %d", balance, len(positions))

    # 3. Generate signal
    sig = generate_signal(market, orderbook)
    if sig is None:
        log.info("No signal this cycle.")
        return False

    log.info("Signal: %s", sig.reason)

    # 4. Risk check
    approved, reason = risk.approve_trade(sig, balance, positions, ticker)
    if not approved:
        log.info("Trade rejected by risk manager: %s", reason)
        return False

    # 5. Size the trade
    contracts = risk.calculate_contracts(sig.price_cents)
    if contracts < 1:
        log.warning("Contract count is 0 — price too high for budget. Skipping.")
        return False

    log.info(
        "Placing %s %s x%d @ %dc (est. cost $%.2f) | dry_run=%s",
        sig.side.upper(), ticker, contracts, sig.price_cents,
        contracts * sig.price_cents / 100, config.DRY_RUN,
    )

    # 6. Execute  (DRY_RUN is handled inside KalshiClient.place_order)
    order = client.place_order(
        ticker=ticker,
        side=sig.side,
        count=contracts,
        price_cents=sig.price_cents,
    )
    order_id = order.get("order", {}).get("order_id") if order else None

    # 7. Log to CSV
    risk.log_trade(
        ticker=ticker,
        side=sig.side,
        contracts=contracts,
        price_cents=sig.price_cents,
        confidence=sig.confidence,
        dry_run=config.DRY_RUN,
        order_id=order_id,
        reason=sig.reason,
    )
    return True


def main():
    setup_logging()
    log.info("=" * 60)
    log.info(" Kalshi 15-minute BTC Trader")
    log.info(" Environment : %s", config.KALSHI_ENV.upper())
    log.info(" Dry run     : %s", config.DRY_RUN)
    log.info(" Max trade   : $%.2f", config.MAX_TRADE_DOLLARS)
    log.info(" Loop every  : %ds", config.LOOP_INTERVAL_SECONDS)
    log.info("=" * 60)

    # Validate config before doing anything else
    try:
        config.validate()
    except EnvironmentError as e:
        log.critical("Configuration error:\n%s", e)
        sys.exit(1)

    client = KalshiClient()
    risk   = RiskManager()

    log.info("Bot started. Press Ctrl+C to stop.")
    while _running:
        try:
            run_once(client, risk)
        except KeyboardInterrupt:
            break
        except Exception as exc:
            log.error("Unexpected error in main loop: %s", exc, exc_info=True)

        if not _running:
            break

        log.debug("Sleeping %ds...", config.LOOP_INTERVAL_SECONDS)
        time.sleep(config.LOOP_INTERVAL_SECONDS)

    log.info("Bot stopped cleanly.")


if __name__ == "__main__":
    main()
