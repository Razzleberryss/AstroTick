"""
Regression tests against the CURRENT Kalshi API response shapes.

The fixtures below are trimmed copies of real production responses captured on
2026-10-17: the /markets payload carries ONLY *_dollars / *_fp fields (the old
integer-cent fields yes_bid, yes_ask, last_price, volume are gone), and the
orderbook arrives as orderbook_fp.yes_dollars / no_dollars with string prices
and fractional string sizes. If parsing of these shapes regresses, the bot goes
blind against the live API even though synthetic-format tests still pass.
"""

import os
import unittest

os.environ.setdefault("ASTROTICK_SKIP_DOTENV", "1")

from kalshi_money import enrich_market_quotes_from_dollar_fields
from orderbook_utils import extract_raw_arrays, extract_yes_no_bids
from bot import _quotes_from_orderbook

# Trimmed from GET /trade-api/v2/markets?series_ticker=KXBTC15M (2026-10-17).
REAL_MARKET = {
    "ticker": "KXBTC15M-26OCT170045-45",
    "status": "active",
    "close_time": "2026-10-17T04:45:00Z",
    "result": "",
    "yes_bid_dollars": "0.5600",
    "yes_ask_dollars": "0.5700",
    "no_bid_dollars": "0.4300",
    "no_ask_dollars": "0.4400",
    "last_price_dollars": "0.5600",
    "volume_fp": "605122.61",
    "open_interest_fp": "258572.03",
    "yes_bid_size_fp": "3400.22",
}

# Trimmed from GET /trade-api/v2/markets/{ticker}/orderbook?depth=3 (2026-10-17).
REAL_ORDERBOOK = {
    "orderbook_fp": {
        "no_dollars": [["0.2400", "909.76"], ["0.2500", "8040.99"], ["0.2600", "2047.15"]],
        "yes_dollars": [["0.7100", "798.00"], ["0.7200", "3749.36"], ["0.7300", "9674.48"]],
    }
}


class TestCurrentMarketShape(unittest.TestCase):
    def test_enrich_populates_legacy_cent_fields(self):
        m = enrich_market_quotes_from_dollar_fields(dict(REAL_MARKET))
        self.assertEqual(m["yes_bid"], 56)
        self.assertEqual(m["yes_ask"], 57)
        self.assertEqual(m["no_bid"], 43)
        self.assertEqual(m["no_ask"], 44)
        self.assertEqual(m["last_price"], 56)
        self.assertEqual(m["best_yes_bid"], 56)
        self.assertEqual(m["best_no_bid"], 43)

    def test_market_has_no_legacy_fields_before_enrichment(self):
        # Guard: the fixture itself must reflect the current API (no cent fields),
        # otherwise this file stops testing what it claims to.
        for legacy in ("yes_bid", "yes_ask", "no_bid", "no_ask", "last_price", "volume"):
            self.assertNotIn(legacy, REAL_MARKET)


class TestCurrentOrderbookShape(unittest.TestCase):
    def test_extract_raw_arrays_finds_fp_dollars(self):
        yes_raw, no_raw = extract_raw_arrays(REAL_ORDERBOOK)
        self.assertEqual(len(yes_raw), 3)
        self.assertEqual(len(no_raw), 3)

    def test_extract_yes_no_bids_parses_prices_and_fractional_sizes(self):
        yes_bids, no_bids = extract_yes_no_bids(REAL_ORDERBOOK)
        # Best bid first, string dollars -> cents, fractional sizes floored.
        self.assertEqual(yes_bids[0], (73, 9674))
        self.assertEqual(no_bids[0], (26, 2047))

    def test_quotes_from_orderbook_derives_complementary_asks(self):
        quotes = _quotes_from_orderbook(REAL_ORDERBOOK)
        self.assertEqual(quotes["best_yes_bid"], 73)
        self.assertEqual(quotes["best_no_bid"], 26)
        # YES ask = 100 - best NO bid; NO ask = 100 - best YES bid.
        self.assertEqual(quotes["best_yes_ask"], 74)
        self.assertEqual(quotes["best_no_ask"], 27)
        self.assertEqual(quotes["mid_price"], 73)


if __name__ == "__main__":
    unittest.main()
