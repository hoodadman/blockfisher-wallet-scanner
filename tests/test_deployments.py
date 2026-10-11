"""Deployment parsing — no network."""

from __future__ import annotations

import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from chains.evm import EvmChain, deployment_from_blockscout_item, deployment_from_etherscan_item, dexscreener_url
from chains.solana import (
    SolanaChain,
    deployment_from_mint_instruction,
    deployments_from_solana_tx,
    dexscreener_url_solana,
)


class DeploymentParseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.chain = EvmChain(
            "base",
            {
                "native": "ETH",
                "decimals": 18,
                "explorer": "https://basescan.org",
                "blockscout": "https://base.blockscout.com/api/v2",
            },
        )
        self.deployer = "0xa010d3f2a041c5baa5fb759a5c1639bf0d007170"

    def test_blockscout_created_contract(self) -> None:
        item = {
            "hash": "0xdeploy",
            "timestamp": "2026-01-02T12:00:00Z",
            "status": "ok",
            "from": {"hash": self.deployer},
            "created_contract": {"hash": "0xToken1111111111111111111111111111111111", "name": "Scam"},
        }
        dep = deployment_from_blockscout_item(item, self.chain, self.deployer)
        self.assertIsNotNone(dep)
        assert dep is not None
        self.assertEqual(dep["contract"], "0xtoken1111111111111111111111111111111111")
        self.assertEqual(dep["name"], "Scam")
        self.assertIn("dexscreener.com/base/", dep["dexscreener"])

    def test_etherscan_contract_address(self) -> None:
        item = {
            "hash": "0xabc",
            "from": self.deployer,
            "to": "",
            "contractAddress": "0x490b1e689ca23be864e55b46bf038e007b528208",
            "timeStamp": "1700000000",
            "isError": "0",
        }
        dep = deployment_from_etherscan_item(item, self.chain, self.deployer)
        self.assertIsNotNone(dep)
        assert dep is not None
        self.assertEqual(dep["contract"], "0x490b1e689ca23be864e55b46bf038e007b528208")

    def test_dexscreener_fallback_search(self) -> None:
        url = dexscreener_url("unknown-chain", "0x490b1e689ca23be864e55b46bf038e007b528208")
        self.assertIn("search?q=", url)

    def test_solana_initialize_mint(self) -> None:
        chain = SolanaChain("sol", {"explorer": "https://solscan.io", "rpc": "https://api.mainnet-beta.solana.com"})
        wallet = "Deployer1111111111111111111111111111111"
        mint = "Mint1111111111111111111111111111111111"
        dep = deployment_from_mint_instruction(
            chain,
            wallet,
            "sig123",
            "2026-01-02T12:00:00Z",
            "initializeMint2",
            {"mint": mint, "mintAuthority": wallet, "decimals": 6},
            fee_payer=True,
        )
        self.assertIsNotNone(dep)
        assert dep is not None
        self.assertEqual(dep["contract"], mint)
        self.assertEqual(dep["kind"], "token")
        self.assertIn("dexscreener.com/solana/", dep["dexscreener"])

    def test_solana_skips_unrelated_mint(self) -> None:
        chain = SolanaChain("sol", {"explorer": "https://solscan.io", "rpc": "https://api.mainnet-beta.solana.com"})
        wallet = "Deployer1111111111111111111111111111111"
        dep = deployment_from_mint_instruction(
            chain,
            wallet,
            "sig123",
            "2026-01-02T12:00:00Z",
            "initializeMint2",
            {"mint": "Mint1111111111111111111111111111111111", "mintAuthority": "SomeoneElse1111111111111111111111111"},
            fee_payer=False,
        )
        self.assertIsNone(dep)

    def test_solana_tx_parsing(self) -> None:
        chain = SolanaChain("sol", {"explorer": "https://solscan.io", "rpc": "https://api.mainnet-beta.solana.com"})
        wallet = "Deployer1111111111111111111111111111111"
        mint = "Mint1111111111111111111111111111111111"
        tx = {
            "blockTime": 1700000000,
            "meta": {"err": None},
            "transaction": {
                "message": {
                    "accountKeys": [{"pubkey": wallet, "signer": True, "writable": True}],
                    "instructions": [
                        {
                            "programId": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
                            "parsed": {
                                "type": "initializeMint2",
                                "info": {"mint": mint, "mintAuthority": wallet, "decimals": 9},
                            },
                        }
                    ],
                }
            },
        }
        rows = deployments_from_solana_tx(chain, wallet, {"signature": "abc"}, tx)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["contract"], mint)

    def test_dexscreener_solana(self) -> None:
        url = dexscreener_url_solana("So11111111111111111111111111111111111111112")
        self.assertEqual(url, "https://dexscreener.com/solana/So11111111111111111111111111111111111111112")


if __name__ == "__main__":
    unittest.main()
