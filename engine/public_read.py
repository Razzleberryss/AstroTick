"""Public REST reads. Market data does not need a key.

The runner uses this for discovery, the book, the series fee fields, and
the index feed so a paper cycle does not depend on a signed SDK call.
Hosts are tried in order: the recommended demo host, then the also-supported
one, unless ``KALSHI_BASE_URL`` pins a single host. Which host answers is
confirmed at runtime, not assumed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

import requests

from engine.rate_limit import RateLimiter

log = logging.getLogger(__name__)

DEMO_REST_HOSTS = (
    "https://external-api.demo.kalshi.co/trade-api/v2",
    "https://demo-api.kalshi.co/trade-api/v2",
)
PROD_REST_HOSTS = (
    "https://external-api.kalshi.com/trade-api/v2",
    "https://api.elections.kalshi.com/trade-api/v2",
)


class PublicReadError(RuntimeError):
    pass


class PublicRead:
    def __init__(self, base_urls: Iterable[str], limiter: RateLimiter, timeout: float = 5.0):
        self.base_urls = [url.rstrip("/") for url in base_urls if url]
        if not self.base_urls:
            raise ValueError("at least one base URL is required")
        self.limiter = limiter
        self.timeout = timeout
        self.host_used: str | None = None

    def get(self, path: str, params: dict | None = None) -> dict:
        if not path.startswith("/"):
            path = "/" + path
        last_error: Exception | None = None
        for base in self.base_urls:
            self.limiter.acquire("read", 1)
            url = base + path
            try:
                response = requests.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("public GET %s failed: %s", url, exc.__class__.__name__)
                continue
            if response.status_code == 429:
                last_error = PublicReadError(f"429 on {path}")
                log.warning("public GET %s rate limited", path)
                continue
            if response.status_code >= 500:
                last_error = PublicReadError(f"{response.status_code} on {path}")
                log.warning("public GET %s -> %s", path, response.status_code)
                continue
            if response.status_code >= 400:
                last_error = PublicReadError(f"{response.status_code} on {path}")
                log.warning("public GET %s -> %s", path, response.status_code)
                continue
            self.host_used = base
            try:
                body = response.json()
            except ValueError as exc:
                raise PublicReadError(f"non-JSON body from {path}") from exc
            if isinstance(body, dict):
                return body
            return {"value": body}
        raise PublicReadError(f"GET {path} failed on {self.base_urls}: {last_error}")
