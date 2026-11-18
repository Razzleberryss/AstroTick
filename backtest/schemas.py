"""Column names the adapters accept.

Candle and trade fields match ``fade/fetch_candles.py`` and
``fade/fetch_trades.py``. Book columns match the recorder CSV. Ladder
lines match ``ladder_YYYY-MM-DD.jsonl`` in the recorder README: each line
is one snapshot with ``yes_bids`` and ``no_bids`` as ``[price, size]``
lists, best price first.
"""

from __future__ import annotations

# markets.parquet / markets.csv (fade/fetch_markets.py KEEP list).
MARKET_COLUMNS = (
    "ticker",
    "open_time",
    "close_time",
    "floor_strike",
    "expiration_value",
    "result",
)

# Candles written by fade/fetch_candles.rows.
CANDLE_COLUMNS = (
    "ticker",
    "end_ts",
    "p_o",
    "p_h",
    "p_l",
    "p_c",
    "p_mean",
    "p_prev",
    "bid_o",
    "bid_c",
    "bid_h",
    "bid_l",
    "ask_o",
    "ask_c",
    "ask_h",
    "ask_l",
    "vol",
    "oi",
)

# Trades written by fade/fetch_trades.py. ``p`` is the YES price in dollars.
TRADE_COLUMNS = ("ticker", "ts", "p", "q", "side", "blk")

# Binance BTCUSDT 1s klines from fade/fetch_binance.py. ``ts`` is the kline
# open, in unix seconds. ``c`` is that bar's close, which is not known until
# the bar ends, so the loader stores it at ``ts + 1``.
SPOT_COLUMNS = ("ts", "o", "h", "l", "c", "vol", "n")

# book_YYYY-MM-DD.csv from the recorder.
BOOK_COLUMNS = (
    "ts_utc",
    "ticker",
    "close_time",
    "seconds_to_close",
    "floor_strike",
    "yes_bid",
    "yes_ask",
    "no_bid",
    "no_ask",
    "spread",
    "yes_bid_sz_top",
    "yes_ask_sz_top",
    "bid_depth_1c",
    "ask_depth_1c",
    "bid_depth_3c",
    "ask_depth_3c",
    "bid_depth_5c",
    "ask_depth_5c",
    "n_yes_levels",
    "n_no_levels",
    "latency_ms",
)

# ladder_YYYY-MM-DD.jsonl keys.
LADDER_KEYS = ("ts_utc", "ticker", "close_time", "floor_strike", "yes_bids", "no_bids")
