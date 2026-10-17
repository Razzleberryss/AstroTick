# Performance Notes

This document describes the performance-relevant design of the bot as it stands today.
It replaces the earlier `PERFORMANCE_ANALYSIS.md` and `PERFORMANCE_IMPROVEMENTS.md`,
whose recommendations have since been implemented or dropped.

The bot is a single-threaded synchronous poll loop (`POLL_INTERVAL_SECONDS`, default
10 s). The design goal is a cycle time well under the poll interval so that exits in a
15-minute market are never delayed by a slow cycle.

## Implemented

- **BTC momentum caching** (`strategy.py`): yfinance 1-minute bars are cached for
  60 seconds (`_btc_momentum_cache`), so most cycles reuse the last fetch instead of
  blocking on a network call.
- **Orderbook skew limited to top levels** (`strategy.py:get_orderbook_skew`): skew is
  computed over the top 10 levels per side (`max_levels=10`) rather than the whole book.
- **Unified orderbook parsing** (`orderbook_utils.py`): one shared implementation of
  bid-array parsing, best-bid extraction, depth and weighted liquidity, used by
  `bot.py`, `strategy.py`, `kalshi_client.py`, and `openclaw_kalshi.py`.
- **Datetime parsing cache** (`bot.py:_parse_close_time`): ISO close-time parsing is
  memoized with `functools.lru_cache(maxsize=200)` — bounded, so no growth over long
  runs.
- **Per-cycle datetime caching** (`risk_manager.py`): one `datetime.now()` per cycle,
  reused across risk checks; the cache is cleared centrally in `run_once`'s `finally`
  block rather than at scattered call sites.
- **Buffered trade logging** (`risk_manager.py`): trade rows are buffered in memory and
  flushed once per cycle and again on shutdown/halt, instead of an open/write/close per
  trade.
- **Fast startup log loading** (`risk_manager.py`): when the trade log exceeds 50 KB,
  only the last ~50 KB is read to rebuild today's daily stats.
- **Coalesced dashboard writes** (`bot.py:write_dashboard_state`): compact JSON,
  atomic replace, and writes skipped when the payload is unchanged within
  `DASHBOARD_MIN_WRITE_SECONDS` (default 2 s).
- **Parallel orderbook fetching** (`openclaw_kalshi.py`): the `markets` command fetches
  candidate orderbooks concurrently with a `ThreadPoolExecutor`.
- **WebSocket orderbook streaming** (`websocket_client.py`, opt-in via
  `USE_WEBSOCKET_ORDERBOOK=true`): replaces the per-cycle REST orderbook call with an
  in-memory snapshot maintained from Kalshi's stream, with staleness detection and
  automatic REST fallback.
- **Bounded API latency** (`kalshi_client.py`): every SDK call gets the configured
  `REQUEST_TIMEOUT_SECONDS` (default 5 s), and read-only calls retry transient
  failures up to `REQUEST_MAX_RETRIES` (default 2). Order placement is deliberately
  never auto-retried.

## Open ideas

- **Background momentum refresh**: when the 60 s cache is cold, the yfinance fetch
  still blocks the loop for the duration of one HTTP round trip. A daemon thread that
  refreshes the momentum value on its own schedule would take it out of the hot path
  entirely.
- **Settlement-index price source**: the markets settle on the CF Benchmarks BRTI
  index, which Kalshi now streams over WebSocket. Sourcing momentum from that feed
  instead of Yahoo's composite BTC-USD price would remove both the latency and the
  basis mismatch. (This is as much a signal-quality change as a performance one.)

Speculative items from the earlier documents (async/await refactor, SQLite trade
history, compiled math, generic "performance monitoring utilities") were dropped:
profiling has not shown the current loop to need them, and the unused `performance.py`
instrumentation module was deleted.

## Measuring

Run with `LOG_LEVEL=DEBUG` to see per-step debug logging, or profile a session:

```bash
python -m cProfile -o profile.stats bot.py
python -c "import pstats; p = pstats.Stats('profile.stats'); p.sort_stats('cumulative').print_stats(20)"
```
