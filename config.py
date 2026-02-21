"""
config.py – Central configuration loader.

Loads all settings from .env and exposes them as typed constants.
Import this module everywhere instead of calling os.getenv() directly.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

# -- Load .env file from project root ----------------------------------------
load_dotenv(dotenv_path=Path(__file__).parent / ".env")

# =============================================================================
# Kalshi API
# =============================================================================
KALSHI_API_KEY_ID: str = os.getenv("KALSHI_API_KEY_ID", "")
KALSHI_PRIVATE_KEY_PATH: str = os.getenv("KALSHI_PRIVATE_KEY_PATH", "./kalshi_private_key.pem")
KALSHI_ENV: str = os.getenv("KALSHI_ENV", "prod").lower()  # 'demo' or 'prod'

# Base URLs (also exposed as KALSHI_BASE_URL for kalshi_client.py)
if KALSHI_ENV == "prod":
    BASE_URL = "https://trading-api.kalshi.com/trade-api/v2"
else:
    BASE_URL = "https://demo-api.kalshi.co/trade-api/v2"

KALSHI_BASE_URL = BASE_URL  # alias used in kalshi_client.py

# =============================================================================
# Risk Controls
# =============================================================================
MAX_TRADE_DOLLARS: float = float(os.getenv("MAX_TRADE_DOLLARS", "10"))
MAX_OPEN_POSITIONS: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
MAX_TOTAL_EXPOSURE: float = float(os.getenv("MAX_TOTAL_EXPOSURE", "50"))

# Contract price range allowed (in cents, 1-99)
MIN_CONTRACT_PRICE_CENTS: int = int(os.getenv("MIN_CONTRACT_PRICE_CENTS", "10"))
MAX_CONTRACT_PRICE_CENTS: int = int(os.getenv("MAX_CONTRACT_PRICE_CENTS", "90"))

# =============================================================================
# Strategy / Signal
# =============================================================================
# BTC_SERIES_TICKER: Kalshi 15-min BTC Up/Down series.
# The live series ticker is BTCZ (e.g. BTCZ-25DEC3100-T3PM).
# Override in .env if Kalshi changes the series name.
BTC_SERIES_TICKER: str = os.getenv("BTC_SERIES_TICKER", "BTCZ")
BTC_TICKER: str = os.getenv("BTC_TICKER", "BTC-USD")  # yfinance symbol
MOMENTUM_LOOKBACK_BARS: int = int(os.getenv("MOMENTUM_LOOKBACK_BARS", "5"))
MIN_EDGE: float = float(os.getenv("MIN_EDGE", "0.05"))
MIN_EDGE_THRESHOLD: float = float(os.getenv("MIN_EDGE_THRESHOLD", "0.05"))

# =============================================================================
# Bot Loop
# =============================================================================
DRY_RUN: bool = os.getenv("DRY_RUN", "true").lower() == "true"
POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
LOOP_INTERVAL_SECONDS: int = POLL_INTERVAL_SECONDS  # alias used in bot.py

# =============================================================================
# Logging & Output
# =============================================================================
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()
TRADE_LOG_FILE: str = os.getenv("TRADE_LOG_FILE", "trades.csv")

# =============================================================================
# Validation
# =============================================================================
def validate() -> None:
    """
    Raise EnvironmentError listing every missing or invalid config value.
    Called once at bot startup before any API requests are made.
    """
    errors: list[str] = []

    if not KALSHI_API_KEY_ID:
        errors.append("KALSHI_API_KEY_ID is not set")
    if not KALSHI_PRIVATE_KEY_PATH or not Path(KALSHI_PRIVATE_KEY_PATH).exists():
        errors.append(
            f"KALSHI_PRIVATE_KEY_PATH '{KALSHI_PRIVATE_KEY_PATH}' does not exist"
        )
    if KALSHI_ENV not in ("prod", "demo"):
        errors.append(f"KALSHI_ENV must be 'prod' or 'demo', got '{KALSHI_ENV}'")
    if MAX_TRADE_DOLLARS <= 0:
        errors.append("MAX_TRADE_DOLLARS must be > 0")
    if MAX_OPEN_POSITIONS < 1:
        errors.append("MAX_OPEN_POSITIONS must be >= 1")
    if MAX_TOTAL_EXPOSURE < MAX_TRADE_DOLLARS:
        errors.append("MAX_TOTAL_EXPOSURE must be >= MAX_TRADE_DOLLARS")
    if not (1 <= MIN_CONTRACT_PRICE_CENTS <= 99):
        errors.append("MIN_CONTRACT_PRICE_CENTS must be between 1 and 99")
    if not (1 <= MAX_CONTRACT_PRICE_CENTS <= 99):
        errors.append("MAX_CONTRACT_PRICE_CENTS must be between 1 and 99")
    if MIN_CONTRACT_PRICE_CENTS >= MAX_CONTRACT_PRICE_CENTS:
        errors.append("MIN_CONTRACT_PRICE_CENTS must be < MAX_CONTRACT_PRICE_CENTS")
    if MOMENTUM_LOOKBACK_BARS < 1:
        errors.append("MOMENTUM_LOOKBACK_BARS must be >= 1")
    if not (0.0 < MIN_EDGE_THRESHOLD < 1.0):
        errors.append("MIN_EDGE_THRESHOLD must be between 0 and 1")

    if errors:
        raise EnvironmentError(
            "Config validation failed:\n" + "\n".join(f"  - {e}" for e in errors)
        )


if __name__ == "__main__":
    # Quick sanity check: print all resolved config values
    print(f"KALSHI_ENV            : {KALSHI_ENV}")
    print(f"BASE_URL              : {BASE_URL}")
    print(f"KALSHI_API_KEY_ID     : {'SET' if KALSHI_API_KEY_ID else 'NOT SET'}")
    print(f"KALSHI_PRIVATE_KEY    : {KALSHI_PRIVATE_KEY_PATH}")
    print(f"BTC_SERIES_TICKER     : {BTC_SERIES_TICKER}")
    print(f"DRY_RUN               : {DRY_RUN}")
    print(f"MAX_TRADE_DOLLARS     : ${MAX_TRADE_DOLLARS}")
    print(f"MAX_OPEN_POSITIONS    : {MAX_OPEN_POSITIONS}")
    print(f"MAX_TOTAL_EXPOSURE    : ${MAX_TOTAL_EXPOSURE}")
    print(f"LOOP_INTERVAL_SECONDS : {LOOP_INTERVAL_SECONDS}s")
    print(f"TRADE_LOG_FILE        : {TRADE_LOG_FILE}")
    try:
        validate()
        print("\nConfig validation: PASSED")
    except EnvironmentError as e:
        print(f"\nConfig validation: FAILED\n{e}")
