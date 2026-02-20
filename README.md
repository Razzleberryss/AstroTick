# Kalshi 15-Minute BTC Trader

A rule-based Python trading bot for Kalshi's BTC Up/Down 15-minute prediction markets.
Trades using the official Kalshi REST API v2 with RSA-PSS authentication.

---

## Project Structure

```
.
├── bot.py              # Main loop - run this
├── kalshi_client.py    # Kalshi API wrapper (auth, orders, positions)
├── strategy.py         # Signal generation (momentum + orderbook skew)
├── risk_manager.py     # Risk checks, position sizing, CSV trade log
├── config.py           # Config loader (reads from .env)
├── .env.example        # Copy to .env and fill in your keys
├── requirements.txt    # Python dependencies
└── .gitignore
```

---

## Quick Start

### 1. Clone and set up environment

```bash
git clone https://github.com/Razzleberryss/Kalshi-15-minute-BTC-trader.git
cd Kalshi-15-minute-BTC-trader

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure your credentials

```bash
cp .env.example .env
```

Then edit `.env`:

```
KALSHI_API_KEY_ID=your-api-key-id        # from kalshi.com/profile/api-management
KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
KALSHI_ENV=demo                          # start with demo!
DRY_RUN=true                             # logs trades, does NOT place them
```

### 3. Add your private key

Download your RSA private key from Kalshi's API management page and save it as
`kalshi_private_key.pem` in the project root (it is git-ignored).

### 4. Run the bot

```bash
# Dry run (safe - no real orders placed)
python bot.py

# Check config is valid
python config.py
```

---

## Strategy

The bot combines two signals every loop:

| Signal | Weight | Description |
|--------|--------|-------------|
| BTC Momentum | 60% | Short-term % price change over last N 1-min bars |
| Orderbook Skew | 40% | YES vs NO liquidity imbalance on Kalshi |

- If composite score > `MIN_EDGE_THRESHOLD` → buy **YES**
- If composite score < `-MIN_EDGE_THRESHOLD` → buy **NO**
- Otherwise → skip

---

## Risk Controls

All limits are set in `.env`:

| Variable | Default | Description |
|----------|---------|-------------|
| `MAX_TRADE_DOLLARS` | $10 | Max cost per trade |
| `MAX_OPEN_POSITIONS` | 3 | Max concurrent open positions |
| `MAX_TOTAL_EXPOSURE` | $50 | Max total capital deployed at once |
| `MIN_EDGE_THRESHOLD` | 0.05 | Min confidence to trigger a trade |
| `DRY_RUN` | true | Set to `false` to trade for real |

---

## Trade Log

Every trade attempt (including dry runs) is logged to `trade_log.csv`:

```
timestamp, ticker, side, contracts, price_cents, cost_dollars, confidence, dry_run, order_id, reason
```

---

## Going Live

1. Run in `KALSHI_ENV=demo` + `DRY_RUN=true` for at least a few days
2. Review `trade_log.csv` to validate signal quality
3. Switch to `KALSHI_ENV=demo` + `DRY_RUN=false` (paper money, real order flow)
4. Only switch to `KALSHI_ENV=prod` after you are satisfied with performance

---

## Extending the Bot

- **Better signals**: Add RSI, VWAP, funding rate, or fear/greed index in `strategy.py`
- **Market making**: Post both YES and NO limit orders to capture the spread
- **Cross-market arb**: Compare Kalshi probabilities against Polymarket or Betfair
- **Backtesting**: Pull historical Kalshi candlestick data via `/candlesticks` endpoint

---

## Legal

This bot uses only the official Kalshi API. Always comply with
[Kalshi's Terms of Service](https://kalshi.com/legal/terms-of-service) and applicable CFTC rules.
This is not financial advice. Trade at your own risk.

---

> Built for the Kalshi `BTCZ` 15-minute series.
