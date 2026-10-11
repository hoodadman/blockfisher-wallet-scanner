"""Multi-chain searches used by the CLI and the local UI.

Blockscan lists every EVM chain on one page but blocks automated access.
These queries hit each chain's indexer (Blockscout, plus Etherscan when a
key is set) and still return Blockscan links.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from classify import (
    Transfer,
    classify_transfer,
    format_amount_bucket,
    keep_asset,
    same_address,
    tag_direction,
)
from chains import build_chains, expand_targets
from chains.evm import _is_cex_label
from nametag import apply_nametags


ProgressFn = Callable[[str, str, str], None]


def matches_value(tr: Transfer, amount: float, cmp: str, asset: str = "") -> bool:
    if asset and tr.asset.upper() != asset.strip().upper():
        return False
    qty = tr.amount()
    if cmp == "eq":
        if amount == 0:
            return qty == 0
        return abs(qty - amount) <= max(amount * 0.001, 1e-12)
    if cmp == "lte":
        return qty <= amount + 1e-12
    return qty + 1e-12 >= amount


def involves_both(tr: Transfer, a: str, b: str) -> bool:
    return (
        (same_address(tr.from_address, a) and same_address(tr.to_address, b))
        or (same_address(tr.from_address, b) and same_address(tr.to_address, a))
    )


def _raw_amount(tr: Transfer) -> int:
    try:
        return int(tr.amount_raw)
    except (TypeError, ValueError):
        return 0


def _add_amount(bucket: dict[tuple[str, int], int], tr: Transfer) -> None:
    key = (tr.asset or "", tr.decimals)
    bucket[key] = bucket.get(key, 0) + _raw_amount(tr)


def group_counterparties(rows: list[Transfer]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for tr in rows:
        key = (tr.counterparty or tr.from_address or "").lower()
        if not key:
            continue
        g = groups.get(key)
        if g is None:
            g = {
                "address": tr.counterparty or tr.from_address,
                "name": tr.resolved_name or tr.counterparty_label or "",
                "category": tr.category,
                "chains": [],
                "in_count": 0,
                "out_count": 0,
                "tx_count": 0,
                "in_amounts": {},
                "out_amounts": {},
            }
            groups[key] = g
        if tr.chain not in g["chains"]:
            g["chains"].append(tr.chain)
        g["tx_count"] += 1
        if tr.direction == "out":
            g["out_count"] += 1
            _add_amount(g["out_amounts"], tr)
        else:
            g["in_count"] += 1
            _add_amount(g["in_amounts"], tr)
        if not g["name"] and tr.resolved_name:
            g["name"] = tr.resolved_name
    out = list(groups.values())
    for g in out:
        volume: dict[tuple[str, int], int] = {}
        for bucket in (g["in_amounts"], g["out_amounts"]):
            for key, raw in bucket.items():
                volume[key] = volume.get(key, 0) + raw
        g["deposited"] = format_amount_bucket(g.pop("in_amounts"))
        g["withdrawn"] = format_amount_bucket(g.pop("out_amounts"))
        g["volume"] = format_amount_bucket(volume)
    out.sort(key=lambda g: (g["tx_count"], g["name"]), reverse=True)
    return out


def match_cex_forward(forwards: list[tuple[str, str]], chain: str, book) -> str | None:
    """Name of the exchange a deposit address forwarded funds to."""
    for addr, label in forwards:
        hit = book.lookup(chain, addr)
        if hit and hit.get("category") == "CEX":
            return hit.get("name") or label or "CEX"
        if _is_cex_label(label):
            return label
    return None


def attribute_cex_deposits(rows: list[Transfer], chains: dict[str, Any], book, limit: int = 20) -> int:
    """Mark outbound sends whose recipient forwarded the funds to an exchange.

    CEX withdrawals usually land on a fresh deposit address, not the hot wallet.
    """
    cache: dict[tuple[str, str], str | None] = {}
    checked = 0
    marked = 0
    for tr in rows:
        if tr.direction != "out" or tr.category == "CEX" or tr.counterparty_is_contract:
            continue
        peer = (tr.counterparty or "").strip()
        if not peer:
            continue
        key = (tr.chain, peer.lower() if peer.startswith("0x") else peer)
        if key not in cache:
            chain_obj = chains.get(tr.chain)
            probe = getattr(chain_obj, "outgoing_peers", None)
            if checked >= limit or not callable(probe):
                cache[key] = None
            else:
                checked += 1
                cache[key] = match_cex_forward(probe(peer), tr.chain, book)
        name = cache[key]
        if not name:
            continue
        tr.category = "CEX"
        tr.resolved_name = name
        tr.category_reason = f"deposit address forwarded to {name}"
        tr.confidence = "medium"
        marked += 1
    return marked


def scan_activity(
    cfg: dict[str, Any],
    conn: sqlite3.Connection,
    book,
    address: str,
    *,
    chain: str | None,
    limit: int,
    token_mode: str,
    direction: str = "both",
    progress: ProgressFn | None = None,
) -> dict[str, Any]:
    chains = build_chains(cfg)
    wanted = expand_targets(chain, address, cfg, chains)
    missing = [c for c in wanted if c not in chains]
    targets = [c for c in wanted if c in chains]
    scanned: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    raw: list[Transfer] = []

    def _one(name: str) -> tuple[str, str, list[Transfer], str]:
        chain_obj = chains[name]
        if not chain_obj.looks_like(address):
            return ("skip", name, [], "address not valid on this chain")
        probe = getattr(chain_obj, "has_activity", None)
        if callable(probe):
            active = probe(address)
            if active is False:
                return ("skip", name, [], "")
        fetch = getattr(chain_obj, "fetch_activity", None)
        try:
            if callable(fetch):
                rows = fetch(address, limit=limit, token_mode=token_mode, direction=direction)
            else:
                rows = chain_obj.fetch_funding(address, limit=limit, token_mode=token_mode)
        except Exception as exc:  # noqa: BLE001
            return ("error", name, [], str(exc))
        return ("ok", name, rows, "")

    if progress:
        for name in targets:
            progress(name, "queued", "")

    workers = min(6, max(1, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, name): name for name in targets}
        for fut in as_completed(futs):
            status, name, rows, err = fut.result()
            if status == "skip":
                skipped.append(name)
                if err:
                    errors.append(f"{name}: {err}")
                if progress:
                    progress(name, "skipped", err)
                continue
            if status == "error":
                errors.append(f"{name}: {err}")
                skipped.append(name)
                if progress:
                    progress(name, "error", err)
                continue
            scanned.append(name)
            raw.extend(rows)
            if progress:
                progress(name, "ok", f"{len(rows)} txs")

    kept: list[Transfer] = []
    for tr in raw:
        if not keep_asset(tr.asset, token_mode):
            continue
        kept.append(tr)
    apply_nametags(kept, book, conn, cfg)
    for tr in kept:
        tag_direction(tr, address)
        classify_transfer(tr, book)
    attribute_cex_deposits(kept, chains, book)
    kept.sort(key=lambda t: t.timestamp or "", reverse=True)
    blockscan = ""
    if address.startswith("0x"):
        base = (cfg.get("blockscan") or "https://blockscan.com").rstrip("/")
        blockscan = f"{base}/address/{address}"
    return {
        "address": address,
        "scanned": scanned,
        "skipped": skipped,
        "missing": missing,
        "errors": errors,
        "blockscan": blockscan,
        "rows": kept,
    }


def cex_links(
    cfg: dict[str, Any],
    conn: sqlite3.Connection,
    book,
    address: str,
    **kwargs: Any,
) -> dict[str, Any]:
    result = scan_activity(cfg, conn, book, address, direction="both", **kwargs)
    txs = [tr for tr in result["rows"] if tr.category == "CEX"]
    result["rows"] = txs
    result["counterparties"] = group_counterparties(txs)
    return result


def between_wallets(
    cfg: dict[str, Any],
    conn: sqlite3.Connection,
    book,
    address: str,
    other: str,
    **kwargs: Any,
) -> dict[str, Any]:
    result = scan_activity(cfg, conn, book, address, direction="both", **kwargs)
    txs = [tr for tr in result["rows"] if involves_both(tr, address, other)]
    result["rows"] = txs
    result["other"] = other
    result["counterparties"] = group_counterparties(txs)
    other_scan = (cfg.get("blockscan") or "https://blockscan.com").rstrip("/")
    if other.startswith("0x"):
        result["blockscan_other"] = f"{other_scan}/address/{other}"
    return result


def value_txs(
    cfg: dict[str, Any],
    conn: sqlite3.Connection,
    book,
    address: str,
    *,
    amount: float,
    cmp: str = "gte",
    asset: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    result = scan_activity(cfg, conn, book, address, direction="both", **kwargs)
    txs = [tr for tr in result["rows"] if matches_value(tr, amount, cmp, asset)]
    result["rows"] = txs
    result["amount"] = amount
    result["cmp"] = cmp
    result["asset_filter"] = asset
    result["counterparties"] = group_counterparties(txs)
    return result


def token_deployments(
    cfg: dict[str, Any],
    conn: sqlite3.Connection,
    book,
    address: str,
    *,
    chain: str | None,
    limit: int,
    **kwargs: Any,
) -> dict[str, Any]:
    """List EVM contracts or Solana SPL mints this wallet deployed."""
    _ = book, conn, kwargs
    address = (address or "").strip()
    chains = build_chains(cfg)
    from chains.solana import BASE58_RE

    is_evm = address.startswith("0x")
    is_sol = bool(BASE58_RE.match(address)) and not is_evm
    if not is_evm and not is_sol:
        raise ValueError("Paste a valid EVM (0x…) or Solana wallet address.")

    wanted = expand_targets(chain, address, cfg, chains)
    missing = [c for c in wanted if c not in chains]
    targets = [c for c in wanted if c in chains]
    scanned: list[str] = []
    skipped: list[str] = []
    errors: list[str] = []
    deployments: list[dict[str, Any]] = []

    def _one(name: str) -> tuple[str, str, list[dict[str, Any]], str]:
        chain_obj = chains[name]
        kind = getattr(chain_obj, "kind", None)
        if kind not in ("evm", "solana"):
            return ("skip", name, [], "unsupported chain kind")
        if not chain_obj.looks_like(address):
            return ("skip", name, [], "address not valid on this chain")
        fetch = getattr(chain_obj, "fetch_token_deployments", None)
        if not callable(fetch):
            return ("skip", name, [], "no deployment scan")
        if kind == "evm":
            has_indexer = bool(
                getattr(chain_obj, "blockscout", None) or getattr(chain_obj, "etherscan_key", None)
            )
            if not has_indexer:
                return ("skip", name, [], "no blockscout or etherscan key")
        try:
            rows = fetch(address, limit=limit)
        except Exception as exc:  # noqa: BLE001
            return ("error", name, [], str(exc))
        return ("ok", name, rows, "")

    workers = min(6, max(1, len(targets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_one, name): name for name in targets}
        for fut in as_completed(futs):
            status, name, rows, err = fut.result()
            if status == "skip":
                skipped.append(name)
                if err and err not in ("not EVM", "address not valid on this chain"):
                    errors.append(f"{name}: {err}")
                continue
            if status == "error":
                errors.append(f"{name}: {err}")
                skipped.append(name)
                continue
            scanned.append(name)
            deployments.extend(rows)

    deployments.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
    blockscan = ""
    if is_sol:
        sol = chains.get("sol")
        if sol and hasattr(sol, "explorer_address"):
            blockscan = sol.explorer_address(address)
    elif is_evm:
        base = (cfg.get("blockscan") or "https://blockscan.com").rstrip("/")
        blockscan = f"{base}/address/{address}"
    return {
        "address": address,
        "scanned": scanned,
        "skipped": skipped,
        "missing": missing,
        "errors": errors,
        "blockscan": blockscan,
        "deployments": deployments,
        "rows": [],
    }
