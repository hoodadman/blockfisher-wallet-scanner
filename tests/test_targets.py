"""Scan-target expansion — no network."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chains import expand_targets
from chains.solana import nonempty_signatures


class Dummy:
    def __init__(self, kind: str) -> None:
        self.kind = kind

    def looks_like(self, address: str) -> bool:
        if self.kind == "evm":
            return address.startswith("0x")
        return not address.startswith("0x")


CFG = {
    "scan_groups": {
        "eth": ["eth", "base", "arb", "op"],
    }
}
CHAINS = {
    "eth": Dummy("evm"),
    "base": Dummy("evm"),
    "arb": Dummy("evm"),
    "op": Dummy("evm"),
    "sol": Dummy("solana"),
}


class ExpandTargetsTests(unittest.TestCase):
    def test_eth_expands_family(self):
        self.assertEqual(
            expand_targets("eth", "0xabc", CFG, CHAINS),
            ["eth", "base", "arb", "op"],
        )

    def test_eth_only_is_mainnet(self):
        self.assertEqual(
            expand_targets("eth-only", "0xabc", CFG, CHAINS),
            ["eth"],
        )

    def test_bare_0x_uses_family(self):
        self.assertEqual(
            expand_targets(None, "0xabc", CFG, CHAINS),
            ["eth", "base", "arb", "op"],
        )

    def test_sol_stays_sol(self):
        self.assertEqual(
            expand_targets("sol", "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", CFG, CHAINS),
            ["sol"],
        )

    def test_empty_signature_page_is_not_a_result(self):
        self.assertIsNone(nonempty_signatures([]))
        self.assertIsNone(nonempty_signatures(None))
        self.assertEqual(len(nonempty_signatures([{"signature": "abc", "err": None}]) or []), 1)

    def test_solana_address_on_evm_dropdown_scans_solana(self):
        self.assertEqual(
            expand_targets("eth", "5tzFkiKscXHK5ZXCGbXZxdw7gTjjD1mBwuoFbhUvuAi9", CFG, CHAINS),
            ["sol"],
        )


if __name__ == "__main__":
    unittest.main()
