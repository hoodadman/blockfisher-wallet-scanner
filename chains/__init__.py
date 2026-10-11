"""Chain registry.

Add a new chain by:
  1. Giving it a block in config.json under ``chains``
  2. Setting ``kind`` to an existing adapter (``evm`` or ``solana``)
  3. If it needs a new protocol, add a module here and register it in ADAPTERS

EVM chains (ETH, later Base/Arbitrum/BSC) share one adapter — only URLs change.
"""

from __future__ import annotations

from typing import Any

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .evm import EvmChain
from .solana import SolanaChain

ADAPTERS = {
    "evm": EvmChain,
    "solana": SolanaChain,
}


def build_chains(cfg: dict[str, Any]) -> dict[str, Any]:
    shared_key = cfg.get("etherscan_api_key") or ""
    shared_api = (cfg.get("etherscan_api") or "https://api.etherscan.io/v2/api").rstrip("/")
    out: dict[str, Any] = {}
    for name, chain_cfg in (cfg.get("chains") or {}).items():
        if not chain_cfg.get("enabled", True):
            continue
        kind = chain_cfg.get("kind")
        adapter_cls = ADAPTERS.get(kind)
        if adapter_cls is None:
            raise ValueError(
                f"Unknown chain kind {kind!r} for {name}. "
                f"Known kinds: {', '.join(sorted(ADAPTERS))}"
            )
        merged = dict(chain_cfg)
        if kind == "evm":
            merged.setdefault("etherscan_api", shared_api)
            if not merged.get("etherscan_api_key"):
                merged["etherscan_api_key"] = shared_key
        out[name] = adapter_cls(name, merged)
    return out


def detect_chains(address: str, chains: dict[str, Any]) -> list[str]:
    """Return enabled chain names that recognise this address format."""
    hits = [name for name, chain in chains.items() if chain.looks_like(address)]
    return hits


def expand_targets(
    requested: str | None,
    address: str,
    cfg: dict[str, Any],
    chains: dict[str, Any],
) -> list[str]:
    """Resolve --chain tokens. ``eth`` / ``evm`` expand to the EVM family."""
    groups = {str(k).lower(): [str(x) for x in v] for k, v in (cfg.get("scan_groups") or {}).items()}
    aliases = {
        "eth-only": ["eth"],
        "eth-mainnet": ["eth"],
        "arbitrum": ["arb"],
        "optimism": ["op"],
        "avalanche": ["avax"],
        "binance": ["bsc"],
        "bnb": ["bsc"],
    }
    if "eth" in groups:
        aliases["evm"] = list(groups["eth"])
    tokens: list[str]
    if requested:
        tokens = [t.strip().lower() for t in requested.split(",") if t.strip()]
    else:
        hits = detect_chains(address, chains)
        evm_hits = [n for n in hits if getattr(chains[n], "kind", None) == "evm"]
        if evm_hits and "eth" in groups:
            tokens = ["eth"]
        else:
            return hits

    if _is_solana_address(address):
        # A Solana pubkey cannot be scanned on the EVM family. Honour an
        # explicit Solana choice, and also scan Solana when the dropdown is
        # still on All EVM.
        return ["sol"]

    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        expanded = aliases.get(token) or groups.get(token) or [token]
        for name in expanded:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def _is_solana_address(address: str) -> bool:
    addr = (address or "").strip()
    if not addr or addr.startswith("0x"):
        return False
    from .solana import BASE58_RE

    return bool(BASE58_RE.match(addr))
