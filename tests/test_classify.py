"""Classification unit tests — no network."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from classify import Transfer, classify_transfer, keep_asset, load_book, tag_direction


def _tr(**kwargs) -> Transfer:
    base = dict(
        chain="eth",
        tx_hash="0xabc",
        timestamp="2026-01-01T00:00:00Z",
        from_address="0x1111111111111111111111111111111111111111",
        to_address="0x2222222222222222222222222222222222222222",
        amount_raw="1000000000000000000",
        decimals=18,
        asset="ETH",
    )
    base.update(kwargs)
    return Transfer(**base)


class ClassifyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = load_book(ROOT / "labels" / "book.json")

    def test_binance_hot_wallet_is_cex(self):
        tr = classify_transfer(
            _tr(from_address="0x28c6c06298d514db089934071355e5743bf21d60"),
            self.book,
        )
        self.assertEqual(tr.category, "CEX")
        self.assertIn("Binance", tr.resolved_name)

    def test_uniswap_router_is_defi(self):
        tr = classify_transfer(
            _tr(from_address="0x7a250d5630b4cf539739df2c5dacb4c659f2488d"),
            self.book,
        )
        self.assertEqual(tr.category, "DEFI")

    def test_tornado_is_other(self):
        tr = classify_transfer(
            _tr(from_address="0x47ce0c6ed5b0ce3d3a51fdb1c52dc66a7c3c2936"),
            self.book,
        )
        self.assertEqual(tr.category, "OTHER")

    def test_blockscout_metadata_tag_flags_mexc_not_in_book(self):
        from chains.evm import _addr_field

        addr, label, is_contract = _addr_field({
            "hash": "0x4e3ae00E8323558fA5Cac04b152238924AA31B60",
            "name": None,
            "is_contract": False,
            "public_tags": [],
            "metadata": {
                "tags": [
                    {"name": "MEXC 15", "tagType": "name"},
                    {"name": "Exchange", "tagType": "generic"},
                    {"name": "note_0", "tagType": "name"},
                ]
            },
        })
        self.assertFalse(is_contract)
        self.assertIn("MEXC 15", label)
        self.assertNotIn("note_0", label)
        tr = classify_transfer(
            _tr(chain="base", from_address=addr, from_label=label),
            self.book,
        )
        self.assertEqual(tr.category, "CEX")
        self.assertIn("MEXC", tr.resolved_name)

    def test_okx_dex_router_is_not_a_cex(self):
        tr = classify_transfer(
            _tr(
                from_address="0x6b2c0c7be2048daa9b5527982c29f48062b34d58",
                from_label="OKX: DEX Router",
                from_is_contract=True,
            ),
            self.book,
        )
        self.assertEqual(tr.category, "DEFI")

    def test_nametag_binance_without_book_address(self):
        tr = classify_transfer(
            _tr(from_address="0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", from_label="Binance 99"),
            self.book,
        )
        self.assertEqual(tr.category, "CEX")

    def test_kraken_44_hot_wallet_is_cex(self):
        tr = classify_transfer(
            _tr(
                chain="arb",
                from_address="0x490b1e689ca23be864e55b46bf038e007b528208",
            ),
            self.book,
        )
        self.assertEqual(tr.category, "CEX")
        self.assertIn("Kraken", tr.resolved_name)

    def test_mexc_1_is_cex(self):
        tr = classify_transfer(
            _tr(from_address="0x75e89d5979e4f6fba9f97c104c2f0afb3f1dcb88"),
            self.book,
        )
        self.assertEqual(tr.category, "CEX")
        self.assertIn("MEXC", tr.resolved_name)

    def test_bitget_hot_wallet_is_cex(self):
        tr = classify_transfer(
            _tr(from_address="0x0639556f03714a74a5feeaf5736a4a64ff70d206"),
            self.book,
        )
        self.assertEqual(tr.category, "CEX")
        self.assertIn("Bitget", tr.resolved_name)

    def test_outbound_to_kraken_is_cex(self):
        scanned = "0xa010d3f2a041c5baa5fb759a5c1639bf0d007170"
        tr = tag_direction(
            _tr(
                chain="arb",
                from_address=scanned,
                to_address="0x490b1e689ca23be864e55b46bf038e007b528208",
            ),
            scanned,
        )
        classify_transfer(tr, self.book)
        self.assertEqual(tr.direction, "out")
        self.assertEqual(tr.category, "CEX")
        self.assertIn("Kraken", tr.resolved_name)

    def test_tag_direction_inbound(self):
        scanned = "0x2222222222222222222222222222222222222222"
        tr = tag_direction(_tr(), scanned)
        self.assertEqual(tr.direction, "in")
        self.assertEqual(tr.counterparty, "0x1111111111111111111111111111111111111111")

    def test_unlabeled_eoa_is_onchain(self):
        tr = classify_transfer(_tr(), self.book)
        self.assertEqual(tr.category, "ONCHAIN")

    def test_unlabeled_contract_is_other(self):
        tr = classify_transfer(_tr(from_is_contract=True), self.book)
        self.assertEqual(tr.category, "OTHER")

    def test_jupiter_program_marks_sol_deposit_defi(self):
        tr = classify_transfer(
            _tr(
                chain="sol",
                from_address="SomeUnlabeledPool11111111111111111111111111",
                asset="SOL",
                decimals=9,
                involved=["JUP6LkbZbjS1jKKwapdHNy74zcZ3tLUZoi5QNyVTaV4"],
            ),
            self.book,
        )
        self.assertEqual(tr.category, "DEFI")
        self.assertIn("Jupiter", tr.resolved_name)

    def test_amount_display_does_not_repeat_asset(self):
        tr = _tr(amount_raw="1250000000000000000")
        self.assertEqual(tr.amount_display(), "1.25 ETH")
        self.assertTrue(keep_asset("ETH", "stables"))
        self.assertTrue(keep_asset("USDC", "stables"))
        self.assertFalse(keep_asset("PEPE", "stables"))
        self.assertTrue(keep_asset("PEPE", "all"))
        self.assertFalse(keep_asset("USDC", "native"))
        self.assertTrue(keep_asset("USDT0", "stables"))
        self.assertTrue(keep_asset("USD₮", "stables"))

    def test_arbitrum_usdt0_transfer_is_usdt(self):
        from chains.evm import EvmChain

        chain = EvmChain("arb", {"native": "ETH", "decimals": 18, "blockscout": "https://example"})
        scanned = "0x2222222222222222222222222222222222222222"
        item = {
            "from": {"hash": "0x1111111111111111111111111111111111111111"},
            "to": {"hash": scanned},
            "token": {
                "symbol": "USDT0",
                "decimals": "6",
                "address_hash": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
            },
            "total": {"value": "2500000"},
            "tx_hash": "0xabc",
            "timestamp": "2026-01-01T00:00:00Z",
        }
        tr = chain._bs_token_item(item, scanned, "in")
        self.assertIsNotNone(tr)
        self.assertEqual(tr.asset, "USDT")
        self.assertEqual(tr.amount_display(), "2.5 USDT")


if __name__ == "__main__":
    unittest.main()
