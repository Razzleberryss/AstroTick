"""
config.py  -  Central configuration loader.

Loads all settings from .env and exposes them as typed constants.
Import this module everywhere instead of calling os.getenv() directly.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ── Load .env file from project root ─────────────────────────────────────────
load_dotenv(dotenv_path=Path(__file__).parent / ".env")


# ── Kalshi API ────────────────────────────────────────────────────────────────
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
KALSHI_ENV: str = os.getenv("KALSHI_ENV", "demo").lower()  # 'demo' or 'prod'

# Base URLs
if KALSHI_ENV == "prod":
    BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"
else:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"


# ── Risk Controls ─────────────────────────────────────────────────────────────
MAX_TRADE_DOLLARS: float = float(os.getenv("MAX_TRADE_DOLLARS", "10"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_TOTAL_EXPOSURE: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "50"))
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))


# ── Strategy Settings ─────────────────────────────────────────────────────────
BTC_TICKER: str = os.getenv("BTC_TICKER", "BTC-USD")
LOOP_INTERVAL_SECONDS: int = int(os.getenv("LOOP_INTERVAL_SECONDS", "30"))
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"

# Kalshi BTC 15-minute series ticker prefix (e.g. BTCZ-15M)
# The bot will auto-discover the active market ticker each loop.
BTC_SERIES_TICKER: str = os.getenv("BTC_SERIES_TICKER", "BTCZ")

# Number of recent BTC price samples used to compute momentum signal
MOMENTUM_LOOKBACK_BARS: int = int(os.getenv("MOMENTUM_LOOKBACK_BARS", "5"))

# Minimum contract price (cents) to consider trading — avoids illiquid tails
MIN_CONTRACT_PRICE_CENTS: int = int(os.getenv("MIN_CONTRACT_PRICE_CENTS", "5"))
MAX_CONTRACT_PRICE_CENTS: int = int(os.getenv("MAX_CONTRACT_PRICE_CENTS", "95"))


# ── Logging ───────────────────────────────────────────────────────────────────
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
TRADE_LOG_FILE: str = os.getenv("TRADE_LOG_FILE", "trade_log.csv")


# ── Validation ────────────────────────────────────────────────────────────────
def validate():
    """Call at startup to catch missing critical config."""
    errors = []
    if not KALSHI_API_KEY_ID:
        errors.append("KALSHI_API_KEY_ID is not set in .env")
    if not Path(KALSHI_PRIVATE_KEY_PATH).exists():
        errors.append(f"Private key not found at: {KALSHI_PRIVATE_KEY_PATH}")
    if errors:
        raise EnvironmentError(
            "Config errors:\n" + "\n".join(f"  - {e}" for e in errors)
        )


if __name__ == "__main__":
    print(f"Environment : {KALSHI_ENV}")
    print(f"Base URL    : {BASE_URL}")
    print(f"Dry run     : {DRY_RUN}")
    print(f"Max trade $ : ${MAX_TRADE_DOLLARS}")
    print(f"Max exposure: ${MAX_TOTAL_EXPOSURE}")
