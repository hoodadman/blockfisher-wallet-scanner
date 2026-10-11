"""Etherscan title parsing — no network."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nametag import parse_etherscan_title


class ParseTitleTests(unittest.TestCase):
    def test_kraken_44(self):
        html = """
        <html><head>
        <title>
	Kraken 44 | Address: 0x490b1e68...07b528208 | Etherscan
</title>
        </head></html>
        """
        self.assertEqual(parse_etherscan_title(html), "Kraken 44")

    def test_unlabeled_address_title(self):
        html = "<title>Address 0x490b1E689Ca23be864e55B46bf038e007b528208 | Etherscan</title>"
        self.assertIsNone(parse_etherscan_title(html))

    def test_address_colon_title(self):
        html = "<title>Address: 0x490b1e68...07b528208 | Etherscan</title>"
        self.assertIsNone(parse_etherscan_title(html))

    def test_cloudflare_challenge(self):
        html = "<title>Just a moment...</title>"
        self.assertIsNone(parse_etherscan_title(html))

    def test_uniswap_contract(self):
        html = "<title>Uniswap V3: Router 2 | Address: 0xE592... | Etherscan</title>"
        self.assertEqual(parse_etherscan_title(html), "Uniswap V3: Router 2")

    def test_og_title_fallback(self):
        html = '<meta property="og:title" content="Binance 14 | Address: 0x28c6... | Etherscan">'
        self.assertEqual(parse_etherscan_title(html), "Binance 14")


if __name__ == "__main__":
    unittest.main()
