"""
synthetic_cfb_price.py – Synthetic CF Benchmarks BTC price estimator.

Kalshi's BTC markets reference CF Benchmarks pricing (BRTI).  Without direct
BRTI feed access this module builds a best-effort synthetic estimate by
scraping several public BTC spot pages with Firecrawl, applying light outlier
filtering, and returning a structured snapshot suitable for agent context.

Public entry points
-------------------
build_synthetic_cfb_snapshot(api_key, outlier_threshold_bps) -> SyntheticCfbSnapshot

Helper functions (also tested individually)
-------------------------------------------
utc_now_iso() -> str
extract_price_usd(markdown_text) -> float | None
scrape_price_source(api_key, source_name, source_url) -> PriceObservation
"""

from __future__ import annotations

import re
import statistics
import datetime
import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Source list
# ---------------------------------------------------------------------------

BTC_SOURCES: list[tuple[str, str]] = [
    ("CoinGecko Bitcoin",   "https://www.coingecko.com/en/coins/bitcoin"),
    ("Coinbase BTC-USD",    "https://www.coinbase.com/price/bitcoin"),
    ("Kraken BTC/USD",      "https://www.kraken.com/prices/btc-bitcoin-price-chart/usd-us-dollar"),
    ("Binance BTC/USDT",    "https://www.binance.com/en/price/bitcoin"),
    ("TradingView BTCUSD",  "https://www.tradingview.com/symbols/BTCUSD/"),
]

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PriceObservation:
    source_name: str
    source_url: str
    price_usd: Optional[float]
    scraped_at: str
    ok: bool
    error: Optional[str]
    raw_excerpt: str


@dataclass
class SyntheticCfbSnapshot:
    synthetic_cfb_mid: Optional[float]
    source_count: int
    min_price: Optional[float]
    max_price: Optional[float]
    spread_dollars: Optional[float]
    spread_bps: Optional[float]
    confidence: str
    confidence_score: float
    observations: list[PriceObservation] = field(default_factory=list)
    scraped_at: str = ""
    ok: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def utc_now_iso() -> str:
    """Return current UTC time in ISO-8601 format."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# Matches dollar amounts like $66,870.79 or $66870.79 or $1,234,567.00
_PRICE_RE = re.compile(r"\$\s*([\d]{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)")


def extract_price_usd(markdown_text: str) -> Optional[float]:
    """
    Extract the first BTC-range USD price from markdown text.

    Looks for dollar-formatted numbers (e.g. ``$66,870.79``) and returns the
    first value that falls in a plausible BTC price range
    (1 000 – 10 000 000 USD).  Returns ``None`` if nothing plausible is found.
    """
    if not markdown_text:
        return None
    for match in _PRICE_RE.finditer(markdown_text):
        raw = match.group(1).replace(",", "")
        try:
            value = float(raw)
        except ValueError:
            continue
        # Filter to plausible BTC range
        if 1_000.0 <= value <= 10_000_000.0:
            return value
    return None


def scrape_price_source(
    api_key: str,
    source_name: str,
    source_url: str,
) -> PriceObservation:
    """
    Scrape *source_url* with Firecrawl and extract a USD price.

    Never raises – all exceptions are caught and reflected in the returned
    ``PriceObservation`` with ``ok=False``.
    """
    now = utc_now_iso()
    try:
        from firecrawl import FirecrawlApp  # type: ignore[import]
        app = FirecrawlApp(api_key=api_key)
        result = app.scrape_url(source_url, formats=["markdown"])
        markdown: str = ""
        if isinstance(result, dict):
            markdown = result.get("markdown") or result.get("content") or ""
        elif hasattr(result, "markdown"):
            markdown = result.markdown or ""
        elif hasattr(result, "content"):
            markdown = result.content or ""
        raw_excerpt = str(markdown)[:1000]
        price = extract_price_usd(markdown)
        if price is None:
            return PriceObservation(
                source_name=source_name,
                source_url=source_url,
                price_usd=None,
                scraped_at=now,
                ok=False,
                error="No BTC price found in scraped content",
                raw_excerpt=raw_excerpt,
            )
        return PriceObservation(
            source_name=source_name,
            source_url=source_url,
            price_usd=price,
            scraped_at=now,
            ok=True,
            error=None,
            raw_excerpt=raw_excerpt,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("scrape_price_source failed for %s: %s", source_name, exc)
        return PriceObservation(
            source_name=source_name,
            source_url=source_url,
            price_usd=None,
            scraped_at=now,
            ok=False,
            error=str(exc),
            raw_excerpt="",
        )


# ---------------------------------------------------------------------------
# Confidence classification
# ---------------------------------------------------------------------------

def _classify_confidence(source_count: int, spread_bps: Optional[float]) -> tuple[str, float]:
    """Return (confidence_label, confidence_score) from source_count and spread_bps."""
    if spread_bps is None:
        return "low", 0.3
    if source_count >= 4 and spread_bps <= 10.0:
        return "high", 0.9
    if source_count >= 3 and spread_bps <= 25.0:
        return "medium", 0.6
    return "low", 0.3


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def build_synthetic_cfb_snapshot(
    api_key: str,
    outlier_threshold_bps: float = 40.0,
) -> SyntheticCfbSnapshot:
    """
    Scrape all configured sources, filter outliers, and return a
    ``SyntheticCfbSnapshot``.

    Steps
    -----
    1. Scrape all sources in ``BTC_SOURCES``.
    2. Keep only observations with a parsed price (``ok=True``).
    3. If fewer than 3 valid prices, return ``ok=False``.
    4. Compute a first-pass median.
    5. Reject prices whose deviation from the median exceeds
       *outlier_threshold_bps* basis points.
    6. Re-compute final median from clean prices.
    7. Compute spread stats and classify confidence.

    Never raises – all exceptions are caught internally.
    """
    now = utc_now_iso()
    observations: list[PriceObservation] = []

    try:
        for source_name, source_url in BTC_SOURCES:
            obs = scrape_price_source(api_key, source_name, source_url)
            observations.append(obs)

        valid: list[float] = [
            o.price_usd for o in observations if o.ok and o.price_usd is not None
        ]

        if len(valid) < 3:
            return SyntheticCfbSnapshot(
                synthetic_cfb_mid=None,
                source_count=len(valid),
                min_price=None,
                max_price=None,
                spread_dollars=None,
                spread_bps=None,
                confidence="low",
                confidence_score=0.3,
                observations=observations,
                scraped_at=now,
                ok=False,
                error=f"Only {len(valid)} valid price(s) – minimum 3 required",
            )

        first_median = statistics.median(valid)

        # Reject outliers: abs deviation from median > outlier_threshold_bps
        clean: list[float] = []
        for p in valid:
            deviation_bps = abs(p - first_median) / first_median * 10_000.0
            if deviation_bps <= outlier_threshold_bps:
                clean.append(p)

        if len(clean) < 3:
            # Fall back to full valid set if filtering discards too many prices
            clean = valid

        final_mid = statistics.median(clean)
        min_price = min(clean)
        max_price = max(clean)
        spread_dollars = max_price - min_price
        spread_bps = (spread_dollars / final_mid) * 10_000.0 if final_mid else None

        confidence, confidence_score = _classify_confidence(len(clean), spread_bps)

        return SyntheticCfbSnapshot(
            synthetic_cfb_mid=final_mid,
            source_count=len(clean),
            min_price=min_price,
            max_price=max_price,
            spread_dollars=spread_dollars,
            spread_bps=spread_bps,
            confidence=confidence,
            confidence_score=confidence_score,
            observations=observations,
            scraped_at=now,
            ok=True,
            error=None,
        )

    except Exception as exc:  # noqa: BLE001
        log.error("build_synthetic_cfb_snapshot failed unexpectedly: %s", exc)
        return SyntheticCfbSnapshot(
            synthetic_cfb_mid=None,
            source_count=0,
            min_price=None,
            max_price=None,
            spread_dollars=None,
            spread_bps=None,
            confidence="low",
            confidence_score=0.3,
            observations=observations,
            scraped_at=now,
            ok=False,
            error=str(exc),
        )
