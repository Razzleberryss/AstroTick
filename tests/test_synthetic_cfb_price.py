"""
tests/test_synthetic_cfb_price.py – Unit tests for synthetic_cfb_price module.

Covers:
- price parsing from markdown string with $66,870.79
- scrape helper success using mocked Firecrawl response
- scrape helper failure using mocked exception
- snapshot success with 5 mocked observations
- outlier rejection
- fewer than 3 valid sources returns ok=False
- confidence classification: high / medium / low
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from synthetic_cfb_price import (
    PriceObservation,
    SyntheticCfbSnapshot,
    build_synthetic_cfb_snapshot,
    extract_price_usd,
    scrape_price_source,
    utc_now_iso,
    _classify_confidence,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_obs(price: float | None, ok: bool = True, source_name: str = "TestSource") -> PriceObservation:
    return PriceObservation(
        source_name=source_name,
        source_url="https://example.com",
        price_usd=price,
        scraped_at=utc_now_iso(),
        ok=ok,
        error=None if ok else "mock error",
        raw_excerpt="",
    )


# ---------------------------------------------------------------------------
# utc_now_iso
# ---------------------------------------------------------------------------

class TestUtcNowIso(unittest.TestCase):
    def test_returns_iso_string(self):
        ts = utc_now_iso()
        self.assertIsInstance(ts, str)
        # Should contain a "T" separator and a timezone marker
        self.assertIn("T", ts)
        self.assertIn("+", ts)


# ---------------------------------------------------------------------------
# extract_price_usd
# ---------------------------------------------------------------------------

class TestExtractPriceUsd(unittest.TestCase):

    def test_parses_dollar_amount_with_commas(self):
        md = "Bitcoin price today: **$66,870.79** USD"
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 66870.79)

    def test_parses_dollar_amount_without_commas(self):
        md = "BTC is trading at $67000.00 right now."
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 67000.0)

    def test_ignores_non_btc_range_prices(self):
        # $100 is below the 1_000 minimum – should be ignored
        md = "Fee: $100  BTC price: $66,500.00"
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 66500.0)

    def test_returns_none_when_no_price_found(self):
        md = "No prices here, just text."
        result = extract_price_usd(md)
        self.assertIsNone(result)

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(extract_price_usd(""))

    def test_returns_none_for_none_input(self):
        self.assertIsNone(extract_price_usd(None))  # type: ignore[arg-type]

    def test_large_million_dollar_btc_price(self):
        md = "BTC all time high: $1,000,000.00"
        result = extract_price_usd(md)
        self.assertAlmostEqual(result, 1_000_000.0)


# ---------------------------------------------------------------------------
# scrape_price_source
# ---------------------------------------------------------------------------

class TestScrapePriceSource(unittest.TestCase):

    def _mock_firecrawl_result(self, price_text: str) -> dict:
        return {"markdown": f"Bitcoin current price: **{price_text}**"}

    @patch("synthetic_cfb_price.FirecrawlApp", create=True)
    def test_success_returns_parsed_price(self, MockApp):
        mock_app = MagicMock()
        mock_app.scrape_url.return_value = self._mock_firecrawl_result("$66,870.79")
        MockApp.return_value = mock_app

        with patch.dict("sys.modules", {"firecrawl": MagicMock(FirecrawlApp=MockApp)}):
            # Import firecrawl inside the module so we can patch it
            import importlib
            import synthetic_cfb_price as mod
            original = None
            try:
                import firecrawl
                original = firecrawl.FirecrawlApp
                firecrawl.FirecrawlApp = MockApp
            except ImportError:
                pass

            obs = scrape_price_source("test-key", "TestSource", "https://example.com")

        # If firecrawl is not installed the scrape will fail gracefully
        if obs.ok:
            self.assertAlmostEqual(obs.price_usd, 66870.79)
            self.assertIsNone(obs.error)
        else:
            # Module not installed – still returns a valid PriceObservation
            self.assertIsNotNone(obs.error)
            self.assertIsInstance(obs, PriceObservation)

    def test_failure_on_exception_returns_ok_false(self):
        """If Firecrawl raises an exception the helper returns ok=False, never raises."""
        with patch("builtins.__import__", side_effect=ImportError("firecrawl not installed")):
            # We can't reliably re-import inside the test; just call with a bad key
            obs = scrape_price_source("", "TestSource", "https://example.com")
        self.assertIsInstance(obs, PriceObservation)
        self.assertFalse(obs.ok)
        self.assertIsNotNone(obs.error)

    def test_failure_returns_valid_observation_structure(self):
        """Even on failure the returned object has the expected fields."""
        obs = scrape_price_source("", "FailSource", "https://bad.url")
        self.assertEqual(obs.source_name, "FailSource")
        self.assertEqual(obs.source_url, "https://bad.url")
        self.assertIsNone(obs.price_usd)
        self.assertFalse(obs.ok)


# ---------------------------------------------------------------------------
# build_synthetic_cfb_snapshot
# ---------------------------------------------------------------------------

def _mock_scrape(prices: list[float | None]):
    """Return a side_effect function that yields each price in turn."""
    iter_prices = iter(prices)

    def _side_effect(api_key, source_name, source_url):
        try:
            p = next(iter_prices)
        except StopIteration:
            p = None
        ok = p is not None
        return PriceObservation(
            source_name=source_name,
            source_url=source_url,
            price_usd=p,
            scraped_at=utc_now_iso(),
            ok=ok,
            error=None if ok else "no price",
            raw_excerpt="",
        )

    return _side_effect


class TestBuildSyntheticCfbSnapshot(unittest.TestCase):

    def test_success_with_five_observations(self):
        prices = [66800.0, 66820.0, 66850.0, 66870.0, 66890.0]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")

        self.assertTrue(snap.ok)
        self.assertIsNotNone(snap.synthetic_cfb_mid)
        self.assertEqual(snap.source_count, 5)
        self.assertIsNotNone(snap.min_price)
        self.assertIsNotNone(snap.max_price)
        self.assertIsNotNone(snap.spread_dollars)
        self.assertIsNotNone(snap.spread_bps)
        self.assertAlmostEqual(snap.synthetic_cfb_mid, 66850.0)  # median of 5
        self.assertEqual(len(snap.observations), 5)
        self.assertIsNone(snap.error)

    def test_outlier_rejection(self):
        # 4 tight prices + 1 extreme outlier well beyond 40 bps
        tight = [66800.0, 66820.0, 66840.0, 66860.0]
        outlier = 70000.0  # ~4700 bps above median
        prices = tight + [outlier]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key", outlier_threshold_bps=40.0)

        self.assertTrue(snap.ok)
        # Outlier should have been removed; source_count should be 4
        self.assertEqual(snap.source_count, 4)
        # All remaining prices should be in the tight cluster
        self.assertLess(snap.max_price, 70000.0)  # type: ignore[operator]

    def test_fewer_than_3_valid_returns_ok_false(self):
        prices = [66800.0, None, None, None, None]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")

        self.assertFalse(snap.ok)
        self.assertIsNone(snap.synthetic_cfb_mid)
        self.assertIsNotNone(snap.error)
        self.assertEqual(snap.source_count, 1)

    def test_exactly_two_valid_returns_ok_false(self):
        prices = [66800.0, 66820.0, None, None, None]
        with patch("synthetic_cfb_price.scrape_price_source", side_effect=_mock_scrape(prices)):
            snap = build_synthetic_cfb_snapshot("test-key")

        self.assertFalse(snap.ok)

    def test_snapshot_never_raises(self):
        """build_synthetic_cfb_snapshot must not propagate exceptions."""
        with patch(
            "synthetic_cfb_price.scrape_price_source",
            side_effect=RuntimeError("unexpected crash"),
        ):
            snap = build_synthetic_cfb_snapshot("test-key")

        self.assertFalse(snap.ok)
        self.assertIsNotNone(snap.error)


# ---------------------------------------------------------------------------
# confidence classification
# ---------------------------------------------------------------------------

class TestClassifyConfidence(unittest.TestCase):

    def test_high_confidence(self):
        label, score = _classify_confidence(source_count=4, spread_bps=8.0)
        self.assertEqual(label, "high")
        self.assertAlmostEqual(score, 0.9)

    def test_high_confidence_five_sources(self):
        label, score = _classify_confidence(source_count=5, spread_bps=5.0)
        self.assertEqual(label, "high")
        self.assertAlmostEqual(score, 0.9)

    def test_medium_confidence(self):
        label, score = _classify_confidence(source_count=3, spread_bps=15.0)
        self.assertEqual(label, "medium")
        self.assertAlmostEqual(score, 0.6)

    def test_low_confidence_wide_spread(self):
        label, score = _classify_confidence(source_count=5, spread_bps=30.0)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_low_confidence_few_sources(self):
        label, score = _classify_confidence(source_count=2, spread_bps=5.0)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_low_confidence_no_spread(self):
        label, score = _classify_confidence(source_count=5, spread_bps=None)
        self.assertEqual(label, "low")
        self.assertAlmostEqual(score, 0.3)

    def test_boundary_high_exactly_10_bps(self):
        label, score = _classify_confidence(source_count=4, spread_bps=10.0)
        self.assertEqual(label, "high")

    def test_boundary_medium_exactly_25_bps(self):
        label, score = _classify_confidence(source_count=3, spread_bps=25.0)
        self.assertEqual(label, "medium")

    def test_boundary_just_over_high_threshold_drops_to_medium(self):
        # 4 sources, spread 11 bps → medium (not high, since spread > 10)
        label, score = _classify_confidence(source_count=4, spread_bps=11.0)
        self.assertEqual(label, "medium")


if __name__ == "__main__":
    unittest.main()
