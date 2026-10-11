"""Search helpers — no network."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from classify import Transfer, tag_direction
from classify import LabelBook
from queries import group_counterparties, involves_both, match_cex_forward, matches_value
from web.server import _run_search


def _tr(**kwargs) -> Transfer:
    base = dict(
        chain="arb",
        tx_hash="0xabc",
        timestamp="2026-01-01T00:00:00Z",
        from_address="0x1111111111111111111111111111111111111111",
        to_address="0xa010d3f2a041c5baa5fb759a5c1639bf0d007170",
        amount_raw="28295770000000000",
        decimals=18,
        asset="ETH",
    )
    base.update(kwargs)
    return Transfer(**base)


class QueryHelperTests(unittest.TestCase):
    def test_matches_value_gte_eq_lte(self):
        tr = _tr()
        self.assertTrue(matches_value(tr, 0.02, "gte"))
        self.assertFalse(matches_value(tr, 0.03, "gte"))
        self.assertTrue(matches_value(tr, 0.02829577, "eq"))
        self.assertTrue(matches_value(tr, 0.03, "lte"))
        self.assertFalse(matches_value(tr, 0.02, "lte"))
        self.assertTrue(matches_value(tr, 0.02, "gte", "ETH"))
        self.assertFalse(matches_value(tr, 0.02, "gte", "USDC"))

    def test_involves_both_either_direction(self):
        a = "0xa010d3f2a041c5baa5fb759a5c1639bf0d007170"
        b = "0x490b1e689ca23be864e55b46bf038e007b528208"
        inbound = _tr(from_address=b, to_address=a)
        outbound = _tr(from_address=a, to_address=b)
        other = _tr(from_address=b, to_address="0x3333333333333333333333333333333333333333")
        self.assertTrue(involves_both(inbound, a, b))
        self.assertTrue(involves_both(outbound, a.upper(), b))
        self.assertFalse(involves_both(other, a, b))

    def test_group_counterparties_splits_in_out(self):
        scanned = "0xa010d3f2a041c5baa5fb759a5c1639bf0d007170"
        kraken = "0x490b1e689ca23be864e55b46bf038e007b528208"
        rows = [
            tag_direction(_tr(from_address=kraken, to_address=scanned, resolved_name="Kraken 44"), scanned),
            tag_direction(_tr(from_address=scanned, to_address=kraken, tx_hash="0xdef", resolved_name="Kraken 44"), scanned),
        ]
        peers = group_counterparties(rows)
        self.assertEqual(len(peers), 1)
        self.assertEqual(peers[0]["in_count"], 1)
        self.assertEqual(peers[0]["out_count"], 1)
        self.assertEqual(peers[0]["name"], "Kraken 44")
        self.assertEqual(peers[0]["deposited"], "0.02829577 ETH")
        self.assertEqual(peers[0]["withdrawn"], "0.02829577 ETH")
        self.assertEqual(peers[0]["volume"], "0.05659154 ETH")

    def test_deposit_address_matches_the_exchange_it_forwards_to(self):
        book = LabelBook(addresses={
            "evm:0x1ab4973a48dc892cd9971ece8e01dcc7688f8f23": {"name": "Bitget 24", "category": "CEX"},
        })
        forwards = [
            ("0x0b2c639c533813f4aa9d7837caf62653d097ff85", "USD Coin"),
            ("0x1ab4973a48dc892cd9971ece8e01dcc7688f8f23", "Bitget"),
        ]
        self.assertEqual(match_cex_forward(forwards, "op", book), "Bitget 24")
        self.assertIsNone(match_cex_forward([forwards[0]], "op", book))

    def test_search_rejects_empty_wallet(self):
        with self.assertRaisesRegex(ValueError, "wallet"):
            _run_search({}, {"mode": "cex", "address": ""})


if __name__ == "__main__":
    unittest.main()
