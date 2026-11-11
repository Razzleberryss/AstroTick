"""Token-bucket limiter sized to Kalshi's documented Basic tier.

Basic is 200 read and 100 write tokens per second. A V2 order costs 10
write tokens by default. The balance endpoint has its own 1 request/second
bucket with no burst. Tiers above Basic are account-specific, so the rates
are settings and the defaults are the slowest documented tier. There is no
documented limit for unauthenticated public reads; those share the read
bucket so a public poll cannot outrun the authenticated budget.
"""

from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    rate: float
    capacity: float
    tokens: float
    updated: float

    def take(self, cost: float, now: float, sleep) -> None:
        if self.rate <= 0:
            raise RuntimeError("rate must be > 0")
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now
        if self.tokens >= cost:
            self.tokens -= cost
            return
        deficit = cost - self.tokens
        wait = deficit / self.rate
        sleep(wait)
        self.tokens = 0.0
        self.updated = now + wait


class RateLimiter:
    def __init__(
        self,
        *,
        read_per_second: float = 200.0,
        write_per_second: float = 100.0,
        read_burst_seconds: float = 3.0,
        write_burst_seconds: float = 1.0,
        order_cost: float = 10.0,
        balance_per_second: float = 1.0,
        clock=time.monotonic,
        sleep=time.sleep,
    ):
        now = clock()
        self._clock = clock
        self._sleep = sleep
        self.order_cost = order_cost
        self._read = _Bucket(read_per_second, read_per_second * read_burst_seconds, read_per_second, now)
        self._write = _Bucket(write_per_second, write_per_second * write_burst_seconds, write_per_second, now)
        # Balance bucket holds one second, so capacity equals the rate.
        self._balance = _Bucket(balance_per_second, balance_per_second, balance_per_second, now)

    def acquire(self, kind: str, cost: float | None = None) -> None:
        now = self._clock()
        if kind == "read":
            self._read.take(1.0 if cost is None else cost, now, self._sleep)
        elif kind == "write":
            self._write.take(self.order_cost if cost is None else cost, now, self._sleep)
        elif kind == "order":
            self._write.take(self.order_cost if cost is None else cost, now, self._sleep)
        elif kind == "balance":
            self._balance.take(1.0, now, self._sleep)
        else:
            raise ValueError(f"unknown rate-limit kind {kind!r}")
