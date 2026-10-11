"""Classify a deposit's counterparty as CEX, DEFI, ONCHAIN, or OTHER."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CATEGORIES = ("CEX", "DEFI", "ONCHAIN", "OTHER")
STABLES = {
    "USDT", "USDC", "DAI", "USDE", "FDUSD", "PYUSD", "USD1", "USDS", "BUSD",
    "TUSD", "FRAX", "GUSD", "LUSD", "CRVUSD", "USDP",
}
NATIVE_ASSETS = {
    "ETH", "SOL", "BNB", "MATIC", "POL", "AVAX", "HYPE", "FTM", "S", "XDAI",
}


@dataclass
class Transfer:
    chain: str
    tx_hash: str
    timestamp: str
    from_address: str
    to_address: str
    amount_raw: str
    decimals: int
    asset: str
    from_label: str = ""
    from_is_contract: bool = False
    to_label: str = ""
    to_is_contract: bool = False
    kind: str = "native"  # native | internal | token
    explorer_tx: str = ""
    involved: list[str] = field(default_factory=list)
    category: str = ""
    category_reason: str = ""
    resolved_name: str = ""
    confidence: str = ""
    direction: str = ""  # in | out (relative to the scanned wallet)
    counterparty: str = ""
    counterparty_label: str = ""
    counterparty_is_contract: bool = False

    def amount(self) -> float:
        try:
            raw = int(self.amount_raw)
        except (TypeError, ValueError):
            return 0.0
        if self.decimals <= 0:
            return float(raw)
        return raw / (10 ** self.decimals)

    def amount_display(self) -> str:
        return format_amount(self.amount(), self.asset)


@dataclass
class LabelBook:
    addresses: dict[str, dict[str, str]] = field(default_factory=dict)
    patterns: list[dict[str, str]] = field(default_factory=list)

    def lookup(self, chain: str, address: str) -> dict[str, str] | None:
        key = _addr_key(chain, address)
        hit = self.addresses.get(key)
        if hit:
            return hit
        # Chain-agnostic EVM book entries are stored as "evm:<addr>"
        if address.startswith("0x"):
            return self.addresses.get(f"evm:{normalise_evm(address)}")
        return None


def load_book(path: Path) -> LabelBook:
    if not path.exists():
        return LabelBook()
    raw = json.loads(path.read_text(encoding="utf-8"))
    addresses: dict[str, dict[str, str]] = {}
    for entry in raw.get("addresses") or []:
        chain = (entry.get("chain") or "").strip().lower()
        addr = entry.get("address") or ""
        if not chain or not addr:
            continue
        key = _addr_key(chain, addr)
        addresses[key] = {
            "name": entry.get("name") or "",
            "category": (entry.get("category") or "OTHER").upper(),
        }
    return LabelBook(addresses=addresses, patterns=list(raw.get("patterns") or []))


def merge_user_labels(book: LabelBook, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        chain = (row.get("chain") or "").strip().lower()
        addr = row.get("address") or ""
        if not chain or not addr:
            continue
        book.addresses[_addr_key(chain, addr)] = {
            "name": row.get("name") or "",
            "category": (row.get("category") or "OTHER").upper(),
        }


def same_address(a: str, b: str) -> bool:
    left = (a or "").strip()
    right = (b or "").strip()
    if not left or not right:
        return False
    if left.startswith("0x") or right.startswith("0x"):
        return normalise_evm(left) == normalise_evm(right)
    return left == right


def tag_direction(tr: Transfer, scanned: str) -> Transfer:
    """Mark in/out and which side is the counterparty relative to ``scanned``."""
    if same_address(tr.to_address, scanned):
        tr.direction = "in"
        tr.counterparty = tr.from_address
        tr.counterparty_label = tr.from_label
        tr.counterparty_is_contract = tr.from_is_contract
        return tr
    if same_address(tr.from_address, scanned):
        tr.direction = "out"
        tr.counterparty = tr.to_address
        tr.counterparty_label = tr.to_label
        tr.counterparty_is_contract = tr.to_is_contract
        return tr
    tr.direction = tr.direction or ""
    tr.counterparty = tr.counterparty or tr.from_address
    tr.counterparty_label = tr.counterparty_label or tr.from_label
    tr.counterparty_is_contract = tr.counterparty_is_contract or tr.from_is_contract
    return tr


def classify_transfer(tr: Transfer, book: LabelBook) -> Transfer:
    peer = tr.counterparty or tr.from_address
    peer_label = tr.counterparty_label if tr.counterparty else tr.from_label
    peer_contract = tr.counterparty_is_contract if tr.counterparty else tr.from_is_contract
    book_hit = book.lookup(tr.chain, peer)
    haystack = " ".join(
        x for x in [peer_label, book_hit["name"] if book_hit else ""] if x
    ).strip()

    if book_hit:
        tr.category = book_hit["category"]
        tr.resolved_name = book_hit["name"] or peer_label
        tr.category_reason = f"label book: {tr.resolved_name}"
        tr.confidence = "high"
        return tr

    for pid in tr.involved:
        prog = book.lookup(tr.chain, pid)
        if prog and prog["category"] == "DEFI":
            tr.category = "DEFI"
            tr.resolved_name = prog["name"] or peer_label or pid
            tr.category_reason = f"tx invoked {prog['name'] or pid}"
            tr.confidence = "medium"
            return tr
        if prog and prog["category"] == "CEX":
            tr.category = "CEX"
            tr.resolved_name = prog["name"] or peer_label
            tr.category_reason = f"tx invoked {prog['name'] or pid}"
            tr.confidence = "medium"
            return tr

    pattern_hit = match_patterns(haystack, book.patterns)
    if pattern_hit and pattern_hit["category"] == "CEX" and _looks_like_dex(haystack):
        pattern_hit = {
            "match": "dex",
            "category": "DEFI",
            "name": "",
        }
    if pattern_hit:
        tr.category = pattern_hit["category"]
        tr.resolved_name = peer_label or pattern_hit.get("name") or pattern_hit["match"]
        tr.category_reason = f"name pattern '{pattern_hit['match']}' → {tr.category}"
        tr.confidence = "medium" if peer_label else "low"
        return tr

    if peer_contract:
        tr.category = "OTHER"
        tr.resolved_name = peer_label or "unlabeled contract"
        tr.category_reason = (
            f"named contract, no category pattern ({peer_label})"
            if peer_label
            else "unlabeled contract / program"
        )
        tr.confidence = "low"
        return tr

    tr.category = "ONCHAIN"
    if peer_label:
        tr.resolved_name = peer_label
        tr.category_reason = f"named wallet, no CEX/DEFI pattern ({peer_label})"
        tr.confidence = "low"
        return tr
    tr.resolved_name = "unlabeled wallet"
    tr.category_reason = "unlabeled EOA / wallet"
    tr.confidence = "low"
    return tr


def _looks_like_dex(text: str) -> bool:
    blob = text.lower()
    return any(word in blob for word in ("dex", "router", "amm", "uniswap", "sushi", "pool"))


def format_amount(qty: float, asset: str) -> str:
    if qty >= 1000:
        rendered = f"{qty:,.2f}"
    elif qty >= 1:
        rendered = f"{qty:.4f}".rstrip("0").rstrip(".")
    else:
        rendered = f"{qty:.8f}".rstrip("0").rstrip(".")
    return f"{rendered} {asset}".strip()


def format_amount_bucket(bucket: dict[tuple[str, int], int]) -> str:
    """Sum of raw token amounts keyed by (asset, decimals)."""
    if not bucket:
        return "—"
    parts: list[tuple[float, str]] = []
    for (asset, decimals), raw in bucket.items():
        qty = raw / (10 ** decimals) if decimals > 0 else float(raw)
        parts.append((qty, asset))
    parts.sort(key=lambda item: (-item[0], item[1]))
    return " + ".join(format_amount(qty, asset) for qty, asset in parts)


def match_patterns(text: str, patterns: list[dict[str, str]]) -> dict[str, str] | None:
    if not text:
        return None
    blob = text.lower()
    for pat in patterns:
        needle = (pat.get("match") or "").lower()
        if needle and needle in blob:
            return {
                "match": pat.get("match") or needle,
                "category": (pat.get("category") or "OTHER").upper(),
                "name": pat.get("name") or "",
            }
    return None


def canonical_asset(symbol: str) -> str:
    """Normalise explorer tickers. Arbitrum's USDT is labelled USDT0."""
    raw = (symbol or "").strip()
    if not raw:
        return ""
    cleaned = "".join(ch for ch in raw.replace("₮", "T").upper() if ch.isalnum())
    if cleaned in {"USDT", "USDT0", "USDTE"}:
        return "USDT"
    return cleaned or raw.upper()


def keep_asset(asset: str, token_mode: str) -> bool:
    asset = canonical_asset(asset)
    if token_mode == "all":
        return True
    if token_mode == "native":
        return asset in NATIVE_ASSETS
    return asset in STABLES or asset in NATIVE_ASSETS


def group_by_category(rows: list[Transfer]) -> dict[str, list[Transfer]]:
    grouped = {c: [] for c in CATEGORIES}
    for row in rows:
        grouped.setdefault(row.category or "OTHER", []).append(row)
    return grouped


def normalise_evm(address: str) -> str:
    return (address or "").strip().lower()


def _addr_key(chain: str, address: str) -> str:
    chain = chain.lower().strip()
    addr = address.strip()
    if addr.startswith("0x"):
        return f"{chain}:{normalise_evm(addr)}"
    return f"{chain}:{addr}"
