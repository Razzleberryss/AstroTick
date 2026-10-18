# AstroTick

A rule-based Python trading bot for Kalshi's crypto Up/Down 15-minute prediction
markets — BTC by default (`KXBTC15M`), with optional parallel trading of the ETH and
SOL series (`SERIES_TICKERS=KXBTC15M,KXETH15M,KXSOL15M`). It polls each market every
few seconds, generates a per-series signal from settlement-index momentum and
orderbook skew, filters it through a fee-aware expected-value check and a risk
manager, and places immediate-or-cancel limit orders through the official Kalshi
Trade API v2 (`CreateOrderV2`).

Momentum is computed from the **settlement index itself**: Kalshi's
`/live_data/events/{event_ticker}` endpoint serves each event's CF Benchmarks
real-time index (the exact number the market settles on) as 1-minute candlesticks,
so the signal has no basis mismatch against an external exchange price. The yfinance
spot feed remains only as an automatic fallback.

The bot is dry-run by default and has two independent gates that must both be opened
before it can place a real order (see [Live trading](#live-trading-two-gates)).

---

## How it works

Every `POLL_INTERVAL_SECONDS` (default 10 s) the bot:

1. Finds the active `KXBTC15M` market (a new one opens every 15 minutes).
2. Fetches the orderbook — from the REST API, or from a WebSocket stream when
   `USE_WEBSOCKET_ORDERBOOK=true` (with staleness detection and automatic REST fallback).
3. Manages every tracked open position: stop-loss, take-profit, signal-reversal and
   expiry exits, plus settlement reconciliation for positions whose market has closed.
4. Generates a signal, risk-checks it, and places an entry order if everything passes.
5. Writes a snapshot to `dashboard_state.json` for the local dashboard, flushes the
   trade log, and sleeps.

### Strategies

`STRATEGY_MODE` selects one of two strategies:

- **`fee_aware_model`** (default) — momentum + orderbook skew:
  - Short-term momentum over `MOMENTUM_LOOKBACK_BARS` 1-minute bars of the event's
    own settlement index (CF Benchmarks, via `/live_data`, cached ~8 s per event);
    falls back per coin to yfinance spot bars (cached 60 s) when the index feed is
    unavailable.
  - Orderbook skew: relative YES vs NO bid liquidity over the top levels of the book.
  - The composite signal shifts a model probability off the market mid. A trade is
    only taken when the mispricing is at least `MIN_EDGE_PCT`, the YES price is outside
    the forbidden band (`FORBIDDEN_PRICE_LOW`–`FORBIDDEN_PRICE_HIGH`, where fees bite
    hardest), and the expected net value per contract **after** open and worst-case
    close taker fees clears `MIN_EXPECTED_NET_PER_CONTRACT`. Fees use Kalshi's
    standard quadratic schedule, `ceil(KALSHI_FEE_RATE × contracts × P × (1−P))`
    in cents, with `KALSHI_FEE_RATE` defaulting to 0.07 (matching `KXBTC15M`'s
    `fee_type=quadratic`, `fee_multiplier=1`).
  - Position size scales with edge from `BASE_SIZE` up to `MAX_SIZE` contracts
    (full size at `MAX_EDGE_PCT` mispricing).
  - Liquidity filters: side-aware minimum orderbook depth (`MIN_YES_DEPTH` /
    `MIN_NO_DEPTH`), maximum slippage (`MAX_SLIPPAGE`), ghost-book detection, and a
    plausibility cap on the model-vs-mid deviation (`MAX_PRICE_DEVIATION`).

- **`reddit_time_delay`** — a late-window price-trigger strategy: it arms only when
  `TRIGGER_MINUTE_REMAINING` minutes or fewer remain in the window, enters the side
  whose price has reached `TRIGGER_POINT_PRICE`, exits early if the position's price
  falls to `EXIT_POINT_PRICE`, and takes at most `MAX_TRADES_PER_WINDOW` entries per
  15-minute window.

### Orders and fills

All orders are **immediate-or-cancel limit orders** placed through the Kalshi
`CreateOrderV2` endpoint (single-book model: a YES buy is a bid, a NO buy is an ask at
the complementary price; sells are reduce-only). Because orders are IOC, the API
response is final and reports the exact fill: the bot records **only what actually
filled** — an order that misses entirely records no position, and partial fills reduce
the tracked position and book PnL at the actual average fill price. Order placement is
never auto-retried; read-only API calls retry transient failures with the configured
timeout.

### Position management and early exits

Open positions are actively managed every cycle, including positions left over from
earlier 15-minute windows:

- **Stop-loss** — sell when the contract price drops `STOP_LOSS_CENTS` below entry.
- **Take-profit** — sell when the price rises `TAKE_PROFIT_CENTS` above entry.
- **Signal reversal** — sell when the latest signal flips against the holding with
  confidence of at least `MIN_EDGE_THRESHOLD` (`SIGNAL_REVERSAL_EXIT=true`).
- **Expiry exit** — sell `EXPIRY_EXIT_SECONDS` before the market's close time.
- **Settlement reconciliation** — a position that could not be exited before close is
  reconciled once the market settles (at 100c or 0c, `exit_reason=settled`), so
  settlement losses count against the daily-loss limit instead of silently vanishing.

Exit orders accept the full 1–99c price range; the `MIN_CONTRACT_PRICE_CENTS` /
`MAX_CONTRACT_PRICE_CENTS` band (default 10–90c) is an entry-only filter.

### Risk manager

`risk_manager.py` gates every entry and keeps the books:

- Per-trade cap (`MAX_TRADE_DOLLARS`), open-position count (`MAX_OPEN_POSITIONS`),
  total exposure (`MAX_TOTAL_EXPOSURE`), and one position per market.
- Daily limits: after `MAX_DAILY_LOSS_CENTS` of realized loss or `MAX_DAILY_TRADES`
  entries in a UTC day, new entries are blocked while open positions continue to be
  managed and exited.
- Every entry and exit is appended to a CSV trade log (`TRADE_LOG_FILE`, default
  `trades.csv` in the project root). Daily stats are rebuilt from this file on
  restart, so the daily-loss gate survives a bot restart.

### Decision-engine envelope layer

Every order — whether placed by the bot or by the manual CLI — goes through a shared
envelope layer:

- `kalshi_inprocess_orders.py` builds buy/sell orders and returns a structured JSON
  envelope (`{ok, code, result/error, ...}`) with three decision flags: `retryable`,
  `halt_trading`, `requires_human_review`.
- `kalshi_agent_envelope.py` is the single source of truth for envelope shapes and
  the `DECISION_POLICY` mapping each response code to those flags.
- `agent_decision_engine.py` deterministically maps an envelope to one action:
  `CONTINUE`, `RETRY`, `RETRY_AND_ESCALATE`, `ESCALATE_TO_HUMAN`, or `HALT_TRADING`.
  Malformed envelopes fail closed to `HALT_TRADING`.
- `cli_executor.py` executes orders through this engine; a `HALT_TRADING` outcome
  writes the `STOP_TRADING` file and stops the bot.

## Safety gates

Three gates are enforced in code, in both the bot and the CLI:

1. **`DRY_RUN`** (default `true`) — orders are logged and simulated, never sent.
2. **`KALSHI_TRADING_LIVE=1`** — even with `DRY_RUN=false`, the envelope layer
   rejects every real order (`LIVE_TRADING_BLOCKED`) unless this environment
   variable is set to exactly `1`.
3. **The `STOP_TRADING` halt file** — if `~/.openclaw/workspace/STOP_TRADING`
   exists (path overridable via `OPENCLAW_STOP_FILE`), all order placement halts
   immediately. The decision engine writes this file itself on a `HALT_TRADING`
   outcome; remove the file to resume trading.

On top of these, `KALSHI_ENV` selects the demo (paper-money) or prod environment, and
the bot prints a prominent warning banner at startup when running live on prod.

## Project structure

```
.
├── bot.py                      # Main loop: per-series market discovery, position management, entries
├── strategy.py                 # Signal generation (momentum + orderbook skew, fee-aware EV filter)
├── index_feed.py               # Settlement-index momentum (CF Benchmarks via /live_data)
├── risk_manager.py             # Trade gating, position tracking, PnL, CSV trade log
├── kalshi_client.py            # Kalshi SDK wrapper (auth, CreateOrderV2 orders, positions, orderbooks)
├── kalshi_money.py             # Fixed-point dollar/_fp field parsing helpers (API v2 shapes)
├── orderbook_utils.py          # Shared orderbook parsing (bids, depth, weighted liquidity)
├── websocket_client.py         # Streaming orderbook client (resubscribe, staleness, seq gaps)
├── kalshi_agent_envelope.py    # JSON envelope builders + DECISION_POLICY (shared by bot and CLI)
├── kalshi_inprocess_orders.py  # In-process buy/sell envelope builders used by the bot and CLI
├── agent_decision_engine.py    # Deterministic envelope -> action interpreter (fail-closed)
├── cli_executor.py             # Executes orders through the decision engine; STOP_TRADING handling
├── openclaw_kalshi.py          # Manual CLI: status / markets / orderbook / buy / sell
├── dashboard.py                # Local Flask dashboard (reads dashboard_state.json)
├── historical.py               # Historical market fetcher for backtesting/analysis
├── config.py                   # Config loader: every env var, defaults, validation
├── tests/                      # pytest suite
├── .github/workflows/          # CI: pytest, Bandit, CodeQL
├── .env.example                # Copy to .env and fill in your keys
└── requirements.txt
```

## Quick start

### 1. Clone and set up the environment

Requires Python 3.13 or newer (the Kalshi SDK's minimum).

```bash
git clone https://github.com/Razzleberryss/AstroTick.git
cd AstroTick
python3 -m venv venv
source venv/bin/activate          # Mac/Linux
# venv\Scripts\activate           # Windows
pip install -r requirements.txt
```

### 2. Configure credentials

```bash
cp .env.example .env
```

Edit `.env` and provide your Kalshi API details (create a key at
kalshi.com → Profile → API management):

- `KALSHI_API_KEY_ID` — your API key ID
- `KALSHI_PRIVATE_KEY_PATH` — path to the private key file (e.g. `./kalshi_private_key.pem`)
- `KALSHI_ENV` — `demo` (paper trading) or `prod` (real money)

The SDK accepts both RSA and Ed25519 keys for REST calls; the optional WebSocket
client signs with RSA-PSS, so choose an RSA key if you plan to enable
`USE_WEBSOCKET_ORDERBOOK`.

All other settings have working defaults — `.env.example` documents every variable
`config.py` reads. Sanity-check your configuration with:

```bash
python config.py
```

### 3. Run the bot

```bash
# Dry run (the default): full decision loop, simulated orders
python bot.py
```

### Live trading (two gates)

Live orders require **both** `DRY_RUN=false` **and** `KALSHI_TRADING_LIVE=1`.
Setting only `DRY_RUN=false` runs the bot live-armed, but the envelope layer rejects
every order with `LIVE_TRADING_BLOCKED`.

```bash
# Paper money, real order flow (Kalshi demo environment)
KALSHI_ENV=demo DRY_RUN=false KALSHI_TRADING_LIVE=1 python bot.py

# Real money
KALSHI_ENV=prod DRY_RUN=false KALSHI_TRADING_LIVE=1 python bot.py
```

To stop all trading immediately without touching the bot process:

```bash
mkdir -p ~/.openclaw/workspace && touch ~/.openclaw/workspace/STOP_TRADING
```

Remove the file to resume. The bot also creates this file itself when the decision
engine halts trading (e.g. after an order failure that is unsafe to retry).

## Manual CLI

`openclaw_kalshi.py` is a standalone command-line tool for inspecting and trading
Kalshi BTC markets by hand (or from an agent/script). All output is a single JSON
envelope per invocation; add `--human` for readable output.

```bash
python openclaw_kalshi.py status                 # balance, positions, active market
python openclaw_kalshi.py markets                # open markets in the series
python openclaw_kalshi.py orderbook              # orderbook for the best live market
python openclaw_kalshi.py buy yes 5 45 --dry-run # buy 5 YES @ 45c (simulated)
python openclaw_kalshi.py sell yes 5 60          # sell 5 YES @ 60c (live: needs KALSHI_TRADING_LIVE=1)
```

The CLI defaults to the **hourly** BTC series `KXBTCD` (override with `--series` or
the `HOURLY_SERIES_TICKER` env var; use `--series KXBTC15M` to target the bot's
15-minute series). `--ticker` targets an exact market; without it, commands
auto-resolve the best live market from the series. Buy and sell delegate to the same
in-process envelope builders the bot uses, so all three safety gates and all config
risk limits apply identically.

## Local web dashboard

After every cycle the bot writes a JSON snapshot to `dashboard_state.json`.
`dashboard.py` is a small Flask server that renders it as an auto-refreshing page.

```bash
# Terminal 1
python bot.py

# Terminal 2
python dashboard.py
```

Open **http://127.0.0.1:8000**. The page refreshes every 5 seconds and shows:

| Field | Description |
|---|---|
| Active market | Current Kalshi ticker being traded |
| YES / NO bid & ask | Best quotes from the orderbook |
| Mid price | Midpoint of the YES spread |
| Spread | YES ask − YES bid (in cents) |
| Signal composite / momentum / skew / confidence | Strategy signal components |
| Position size | Contracts currently held |
| Realized PnL today | Today's closed-trade P&L in cents |

Errors writing or reading `dashboard_state.json` are logged at DEBUG level and never
crash the bot or the dashboard.

## Historical data

`historical.py` fetches settled markets for a series and date range (inclusive) as
JSON, for backtesting and analysis:

```bash
python -m historical --series KXBTC15M --start 2025-12-01 --end 2025-12-31 --output markets.json
```

## Tests and CI

```bash
python -m pytest tests/
```

GitHub Actions runs the pytest suite on every push and pull request (Python 3.13, the
project's minimum version), alongside Bandit and CodeQL security scans. See
`.github/workflows/`.

## Logs and output files

| File | What it is |
|---|---|
| `trades.csv` | CSV trade log (entries and exits); rebuilds daily-loss stats on restart. Path: `TRADE_LOG_FILE`. |
| `dashboard_state.json` | Per-cycle snapshot for the dashboard. |
| `~/.openclaw/workspace/STOP_TRADING` | Halt file: its presence stops all order placement. Path: `OPENCLAW_STOP_FILE`. |

Console log verbosity is controlled by `LOG_LEVEL` (default `INFO`).

## What's new (Oct 2026 refresh)

The codebase was modernized against the current Kalshi API and cleaned up:

- Order placement migrated to `CreateOrderV2` (the legacy order endpoint is sunsetting);
  orders are IOC and the bot records only actual fills, including partial fills.
- Market/position parsing updated to the current API shapes (`*_dollars` / `*_fp`
  fields; the old integer-cent fields are gone from responses).
- Money-path fixes: correct fee scaling in the EV filter (configurable via
  `KALSHI_FEE_RATE`), correct NO-side PnL sign, exits freed from the entry price band,
  dry-run exits no longer require live positions.
- All tracked positions are managed every cycle, with settlement reconciliation for
  markets that closed before an exit filled.
- WebSocket client hardened: resubscribe after reconnect, staleness detection,
  sequence-gap recovery, fixed backoff.
- Dead code removed (including the unused `performance.py` and a broken subprocess
  order path); the CLI now shares the bot's envelope builders.
- CI test workflow added; the suite currently has 400+ tests.

## Disclaimer

This bot is for educational purposes only. Trading involves risk. Use at your own risk.
