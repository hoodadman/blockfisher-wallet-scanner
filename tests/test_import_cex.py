"""Parser tests for the public CEX address import. No network."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from import_cex import (
    accept_scrape_row,
    merge_new,
    parse_bitfinex_sol,
    parse_dune_evm,
    parse_dune_sol,
    parse_scrape_json,
)

NEEDLES = ["binance", "kraken", "coinbase", "mexc", "bitget"]

EVM_SQL = """
SELECT address, cex_name, distinct_name, added_by, added_date
FROM (VALUES
    (0x28C6c06298d514db089934071355E5743bf21d60, 'Binance', 'Binance 14', 'hildobby', date '2022-08-28')
    , (0x490b1e689ca23be864e55b46bf038e007b528208, 'Kraken', 'Kraken 25', 'hildobby', date '2024-01-01')
    , (0x490b1e689ca23be864e55b46bf038e007b528208, 'Kraken', 'Kraken duplicate', 'x', date '2024-01-02')
)
"""

SOL_SQL = """
FROM (VALUES
    ('solana', '5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9', 'Binance', 'Binance 1', 'hildobby', date '2024-04-19')
)
"""


class ImportCexTests(unittest.TestCase):
    def test_parse_dune_evm_dedupes_and_lowercases(self):
        rows = parse_dune_evm(EVM_SQL)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["address"], "0x28c6c06298d514db089934071355e5743bf21d60")
        self.assertEqual(rows[0]["name"], "Binance 14")
        self.assertEqual(rows[1]["name"], "Kraken 25")

    def test_okx_dex_router_is_not_imported(self):
        sql = EVM_SQL + (
            "\n, (0x6b2c0c7be2048daa9b5527982c29f48062b34d58, 'OKX', 'OKX 138', 'hildobby', date '2024-05-04')\n"
        )
        rows = parse_dune_evm(sql)
        self.assertNotIn("0x6b2c0c7be2048daa9b5527982c29f48062b34d58", [row["address"] for row in rows])

    def test_parse_bitfinex_solana_wallets(self):
        text = "\n".join([
            "FxteHmLwG9nk1eL4pjNve3Eub2goGkkz6g6TbvdmW46a - Solana hot wallet",
            "0x77134cbc06cb00b66f4c7e623d5fdbf6777635ec - ETH/ERC20 hot wallet",
            "GnCRxKqUEPouYMvTb5nJMGrDB3VkTXZnDTaDuVZdnWA3 - Tether USDt (Solana) hot wallet",
        ])
        rows = parse_bitfinex_sol(text)
        self.assertEqual([row["address"] for row in rows], [
            "FxteHmLwG9nk1eL4pjNve3Eub2goGkkz6g6TbvdmW46a",
            "GnCRxKqUEPouYMvTb5nJMGrDB3VkTXZnDTaDuVZdnWA3",
        ])
        self.assertEqual(rows[0]["chain"], "sol")
        self.assertIn("Bitfinex", rows[0]["name"])

    def test_parse_dune_sol(self):
        rows = parse_dune_sol(SOL_SQL)
        self.assertEqual(rows[0]["chain"], "sol")
        self.assertEqual(rows[0]["name"], "Binance 1")

    def test_scrape_keeps_exchange_brands_and_drops_dex(self):
        self.assertTrue(accept_scrape_row("kraken", "Kraken 4", NEEDLES))
        self.assertTrue(accept_scrape_row("exchange", "MEXC Hot Wallet", NEEDLES))
        self.assertFalse(accept_scrape_row("exchange", "Portto: Hot Wallet", NEEDLES))
        self.assertFalse(accept_scrape_row("hot-wallet", "OKX: DEX Router", ["okx"]))
        payload = {
            "0x21a31ee1afc51d94c2efccaa2092ad1028285549": "Binance 15",
            "0x1111111111111111111111111111111111111111": "Some DEX pool",
            "": "Sum of 1 Account",
        }
        rows = parse_scrape_json("binance", payload, NEEDLES)
        self.assertEqual([row["name"] for row in rows], ["Binance 15"])

    def test_merge_does_not_rename_existing(self):
        existing = [{
            "chain": "evm",
            "address": "0x28C6c06298d514db089934071355E5743bf21d60",
            "name": "Binance 14",
            "category": "CEX",
        }]
        incoming = [
            {"chain": "evm", "address": "0x28c6c06298d514db089934071355e5743bf21d60", "name": "Binance other", "category": "CEX"},
            {"chain": "evm", "address": "0x490b1e689ca23be864e55b46bf038e007b528208", "name": "Kraken 25", "category": "CEX"},
        ]
        added = merge_new(existing, incoming)
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["name"], "Kraken 25")


if __name__ == "__main__":
    unittest.main()
