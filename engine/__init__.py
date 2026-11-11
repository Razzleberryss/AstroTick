"""Strategy-agnostic core for Kalshi KXBTC15M demo/paper trading.

Strategies propose quotes. They do not submit orders. Every create, amend,
and size decrease goes through ``engine.orders`` and the cutoff guard in
``engine.cutoff``, which blocks new orders from T-50s (plus clock-skew
margin) through settlement.
"""
