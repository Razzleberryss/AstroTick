"""Historical replay of the live KXBTC15M engine.

The strategy, fair-value, risk, and order gateway are imported from
``engine``. This package only supplies data adapters, a fill simulator
behind the gateway's sender interface, and report writers. It never
places a live order and never reads API keys.
"""
