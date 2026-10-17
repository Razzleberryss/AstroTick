"""
test_historical.py - Tests for the historical market data fetcher.

Verifies that:
1. The --end date is inclusive: max_close_ts covers the whole end day
   (bound extended to the start of the following day), and start == end
   yields a non-empty one-day window.
2. last_price falls back to parse_dollars_to_cents_int on last_price_dollars
   without raising NameError, and dollar-string prices convert to cents.
3. Pagination requests limit=1000 and follows the cursor across pages.
"""
import datetime
import os
import tempfile
import unittest
from unittest.mock import MagicMock

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")
os.environ.setdefault(
    "OPENCLAW_STOP_FILE",
    os.path.join(tempfile.gettempdir(), f"openclaw_stop_file_tests_{os.getpid()}"),
)

from historical import fetch_historical_markets

UTC = datetime.timezone.utc


def _day_ts(year, month, day):
    """Unix timestamp of UTC midnight for the given date."""
    return int(datetime.datetime(year, month, day, tzinfo=UTC).timestamp())


def _mock_client(pages):
    """A mock KalshiClient whose fetch_markets returns one page per call,
    recording a snapshot of the params passed on each call (the module
    mutates the same dict between calls, so copies are required)."""
    client = MagicMock()
    calls = []
    responses = iter(pages)

    def fetch_markets(params):
        calls.append(dict(params))
        return next(responses)

    client.fetch_markets.side_effect = fetch_markets
    client.captured_params = calls
    return client


class TestDateRangeInclusivity(unittest.TestCase):
    """The --end date must be inclusive of the entire last day."""

    def test_max_close_ts_covers_whole_end_day(self):
        client = _mock_client([{"markets": []}])

        fetch_historical_markets("KXBTC15M", "2025-12-01", "2025-12-31", client=client)

        params = client.captured_params[0]
        self.assertEqual(params["min_close_ts"], _day_ts(2025, 12, 1))
        # Upper bound is the start of the day AFTER --end, so markets closing
        # any time on Dec 31 (e.g. 23:45) are inside the window.
        self.assertEqual(params["max_close_ts"], _day_ts(2026, 1, 1))

    def test_start_equals_end_yields_one_full_day(self):
        client = _mock_client([{"markets": []}])

        fetch_historical_markets("KXBTC15M", "2025-12-15", "2025-12-15", client=client)

        params = client.captured_params[0]
        self.assertEqual(params["min_close_ts"], _day_ts(2025, 12, 15))
        self.assertEqual(params["max_close_ts"], _day_ts(2025, 12, 16))
        self.assertEqual(
            params["max_close_ts"] - params["min_close_ts"], 24 * 60 * 60
        )

    def test_start_after_end_raises(self):
        client = _mock_client([{"markets": []}])
        with self.assertRaises(ValueError):
            fetch_historical_markets(
                "KXBTC15M", "2025-12-31", "2025-12-01", client=client
            )

    def test_invalid_date_format_raises(self):
        client = _mock_client([{"markets": []}])
        with self.assertRaises(ValueError):
            fetch_historical_markets(
                "KXBTC15M", "12/01/2025", "2025-12-31", client=client
            )


class TestLastPriceCentsParsing(unittest.TestCase):
    """last_price handling: integer cents, dollar-string fallback, missing."""

    def _fetch_one(self, market):
        client = _mock_client([{"markets": [market]}])
        results = fetch_historical_markets(
            "KXBTC15M", "2025-12-01", "2025-12-02", client=client
        )
        self.assertEqual(len(results), 1)
        return results[0]

    def test_integer_last_price_is_preserved(self):
        row = self._fetch_one({"ticker": "T1", "last_price": 57})
        self.assertEqual(row["last_price"], 57)

    def test_last_price_dollars_string_converts_to_cents(self):
        row = self._fetch_one({"ticker": "T2", "last_price_dollars": "0.57"})
        self.assertEqual(row["last_price"], 57)

    def test_zero_dollar_last_price_is_zero_cents(self):
        row = self._fetch_one({"ticker": "T3", "last_price_dollars": "0.00"})
        self.assertEqual(row["last_price"], 0)

    def test_missing_last_price_fields_yield_none_without_error(self):
        # Previously raised NameError (parse_dollars_to_cents_int unimported)
        # on exactly this never-traded-market shape.
        row = self._fetch_one({"ticker": "T4", "title": "Never traded"})
        self.assertIsNone(row["last_price"])

    def test_malformed_last_price_dollars_yields_none_without_error(self):
        row = self._fetch_one({"ticker": "T5", "last_price_dollars": "not-a-price"})
        self.assertIsNone(row["last_price"])


class TestPagination(unittest.TestCase):
    """Pagination uses the endpoint's 1000-per-page maximum and follows cursors."""

    def test_limit_is_1000(self):
        client = _mock_client([{"markets": []}])
        fetch_historical_markets("KXBTC15M", "2025-12-01", "2025-12-02", client=client)
        self.assertEqual(client.captured_params[0]["limit"], 1000)

    def test_cursor_is_followed_and_pages_concatenated(self):
        pages = [
            {"markets": [{"ticker": "A", "last_price": 10}], "cursor": "page2"},
            {"markets": [{"ticker": "B", "last_price": 20}]},
        ]
        client = _mock_client(pages)

        results = fetch_historical_markets(
            "KXBTC15M", "2025-12-01", "2025-12-02", client=client
        )

        self.assertEqual([r["ticker"] for r in results], ["A", "B"])
        self.assertEqual(client.fetch_markets.call_count, 2)
        self.assertNotIn("cursor", client.captured_params[0])
        self.assertEqual(client.captured_params[1]["cursor"], "page2")


if __name__ == "__main__":
    unittest.main()
