#!/usr/bin/env python3
"""Butters Trace - on-chain address scanner for crypto financial-crime work.

First function: funding (inbound deposits), classified CEX / DEFI / ONCHAIN / OTHER.

Standard library only. Python 3.10+.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from classify import (
    CATEGORIES,
    LabelBook,
    Transfer,
    classify_transfer,
    group_by_category,
    keep_asset,
    load_book,
    merge_user_labels,
)
from chains import build_chains, expand_targets
from nametag import apply_nametags
from queries import between_wallets, cex_links, value_txs

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = ROOT / "config.json"

SCHEMA = """
CREATE TABLE IF NOT EXISTS scans (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    address     TEXT NOT NULL,
    chain       TEXT NOT NULL,
    scanned_at  TEXT NOT NULL,
    token_mode  TEXT NOT NULL,
    n_funding   INTEGER NOT NULL DEFAULT 0,
    report_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_labels (
    chain       TEXT NOT NULL,
    address     TEXT NOT NULL,
    name        TEXT NOT NULL,
    category    TEXT NOT NULL,
    notes       TEXT,
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (chain, address)
);
CREATE TABLE IF NOT EXISTS nametag_cache (
    address    TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL DEFAULT 'etherscan',
    fetched_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _print(text: str) -> None:
    try:
        print(text)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(text.encode(enc, errors="replace").decode(enc, errors="replace"))


def load_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Missing config at {path}. Copy config.example.json to config.json.")
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg["_dir"] = str(path.resolve().parent)
    return cfg


def _abs(cfg: dict[str, Any], key: str, default: str) -> Path:
    raw = Path(str(cfg.get(key, default)))
    return raw if raw.is_absolute() else Path(cfg["_dir"]) / raw


def connect(cfg: dict[str, Any]) -> sqlite3.Connection:
    db = _abs(cfg, "db_path", "trace.db")
    conn = sqlite3.connect(str(db), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def load_labels(cfg: dict[str, Any], conn: sqlite3.Connection) -> LabelBook:
    book = load_book(_abs(cfg, "labels_path", "labels/book.json"))
    rows = [dict(r) for r in conn.execute("SELECT chain, address, name, category FROM user_labels")]
    merge_user_labels(book, rows)
    return book


def transfer_to_dict(tr: Transfer) -> dict[str, Any]:
    return {
        "chain": tr.chain,
        "tx_hash": tr.tx_hash,
        "timestamp": tr.timestamp,
        "from": tr.from_address,
        "to": tr.to_address,
        "amount": tr.amount(),
        "amount_display": tr.amount_display(),
        "asset": tr.asset,
        "kind": tr.kind,
        "from_label": tr.from_label,
        "resolved_name": tr.resolved_name,
        "from_is_contract": tr.from_is_contract,
        "category": tr.category,
        "reason": tr.category_reason,
        "confidence": tr.confidence,
        "explorer_tx": tr.explorer_tx,
        "involved": tr.involved,
        "direction": tr.direction,
        "counterparty": tr.counterparty,
        "counterparty_label": tr.counterparty_label,
        "to_label": tr.to_label,
    }


def format_report(
    address: str,
    chain_name: str,
    explorer_addr: str,
    rows: list[Transfer],
    *,
    scanned: list[str] | None = None,
    skipped: list[str] | None = None,
    blockscan: str = "",
) -> str:
    grouped = group_by_category(rows)
    multi = len({r.chain for r in rows}) > 1 or (scanned and len(scanned) > 1)
    title = f"# Funding sources for `{address}`"
    if multi:
        title += " (EVM family)"
    else:
        title += f" ({chain_name})"
    lines = [title, ""]
    if blockscan:
        lines.append(f"Blockscan: {blockscan}")
    if explorer_addr and not multi:
        lines.append(f"Explorer: {explorer_addr}")
    if scanned:
        lines.append(f"Chains scanned: {', '.join(scanned)}")
    if skipped:
        lines.append(f"No activity / skipped: {', '.join(skipped)}")
    lines += [
        f"Deposits classified: **{len(rows)}**",
        "",
        "## Summary",
        "",
        "| Category | Count |",
        "|---|---|",
    ]
    for cat in CATEGORIES:
        lines.append(f"| {cat} | {len(grouped.get(cat) or [])} |")
    if multi:
        by_chain: dict[str, int] = {}
        for tr in rows:
            by_chain[tr.chain] = by_chain.get(tr.chain, 0) + 1
        lines += ["", "| Chain | Deposits |", "|---|---|"]
        for name in scanned or sorted(by_chain):
            lines.append(f"| {name} | {by_chain.get(name, 0)} |")
    lines.append("")
    for cat in CATEGORIES:
        chunk = grouped.get(cat) or []
        lines.append(f"## {cat}")
        lines.append("")
        if not chunk:
            lines.append("_None found._")
            lines.append("")
            continue
        if multi:
            lines.append("| Chain | Time (UTC) | Amount | From | Name | Why | Tx |")
            lines.append("|---|---|---|---|---|---|---|")
        else:
            lines.append("| Time (UTC) | Amount | From | Name | Why | Tx |")
            lines.append("|---|---|---|---|---|---|")
        for tr in chunk:
            name = tr.resolved_name or tr.from_label or "—"
            tx = f"[tx]({tr.explorer_tx})" if tr.explorer_tx else tr.tx_hash[:10]
            frm = f"`{tr.from_address}`"
            if multi:
                lines.append(
                    f"| {tr.chain} | {tr.timestamp or '—'} | {tr.amount_display()} | {frm} | {name} | {tr.category_reason} | {tx} |"
                )
            else:
                lines.append(
                    f"| {tr.timestamp or '—'} | {tr.amount_display()} | {frm} | {name} | {tr.category_reason} | {tx} |"
                )
        lines.append("")
    lines.append("## Enrichment")
    lines.append("")
    lines.append("Open these if a counterparty is unlabeled or the category looks wrong:")
    evm = multi or chain_name != "sol"
    if evm:
        if blockscan:
            lines.append(f"- Blockscan (all EVM chains): {blockscan}")
        lines.append(f"- Arkham: https://intel.arkm.com/explorer/address/{address}")
        lines.append(f"- MetaSleuth: https://metasleuth.io/result/eth/{address}")
        lines.append(f"- DeBank (portfolio / protocol exposure, not funding): https://debank.com/profile/{address}")
    else:
        lines.append(f"- Solscan: {explorer_addr}")
        lines.append(f"- SolanaFM: https://solana.fm/address/{address}")
    lines.append("")
    lines.append("Teach the book with `label --address <from> --name <name> --category CEX|DEFI|ONCHAIN|OTHER`.")
    return "\n".join(line for line in lines if line is not None)


def cmd_funding(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    chains = build_chains(cfg)
    wanted = expand_targets(args.chain, args.address, cfg, chains)
    missing = [c for c in wanted if c not in chains]
    if missing:
        print(f"Unknown or disabled chain(s): {', '.join(missing)}", file=sys.stderr)
        print(f"Enabled: {', '.join(chains) or '(none)'}", file=sys.stderr)
        return 1
    targets = [c for c in wanted if c in chains]
    if not targets:
        print(
            "Could not detect chain from address. Pass --chain eth or --chain sol.",
            file=sys.stderr,
        )
        return 1

    conn = connect(cfg)
    book = load_labels(cfg, conn)
    token_mode = args.tokens
    limit = max(1, int(args.limit))
    all_payloads: list[dict[str, Any]] = []
    all_rows: list[Transfer] = []
    scanned: list[str] = []
    skipped: list[str] = []
    exit_code = 0
    blockscan = ""
    if any(getattr(chains[n], "kind", None) == "evm" for n in targets):
        base = (cfg.get("blockscan") or "https://blockscan.com").rstrip("/")
        blockscan = f"{base}/address/{args.address}"

    try:
        for name in targets:
            chain = chains[name]
            if not chain.looks_like(args.address):
                print(f"Skipping {name}: address is not valid on that chain.", file=sys.stderr)
                skipped.append(name)
                continue
            probe = getattr(chain, "has_activity", None)
            if callable(probe):
                active = probe(args.address)
                if active is False:
                    skipped.append(name)
                    continue
            try:
                raw_rows = chain.fetch_funding(args.address, limit=limit, token_mode=token_mode)
            except Exception as exc:  # noqa: BLE001
                print(f"ERROR scanning {name}: {exc}", file=sys.stderr)
                exit_code = 1
                skipped.append(name)
                continue
            apply_nametags(raw_rows, book, conn, cfg)
            classified: list[Transfer] = []
            for tr in raw_rows:
                if not keep_asset(tr.asset, token_mode):
                    continue
                classified.append(classify_transfer(tr, book))
            scanned.append(name)
            all_rows.extend(classified)
            payload = {
                "address": args.address,
                "chain": name,
                "scanned_at": now_iso(),
                "token_mode": token_mode,
                "explorer": chain.explorer_address(args.address),
                "blockscan": blockscan,
                "funding": [transfer_to_dict(tr) for tr in classified],
                "counts": {cat: sum(1 for t in classified if t.category == cat) for cat in CATEGORIES},
            }
            all_payloads.append(payload)
            conn.execute(
                "INSERT INTO scans(address, chain, scanned_at, token_mode, n_funding, report_json) VALUES (?,?,?,?,?,?)",
                (
                    args.address,
                    name,
                    payload["scanned_at"],
                    token_mode,
                    len(classified),
                    json.dumps(payload),
                ),
            )
            conn.commit()

        if args.json:
            print(json.dumps(all_payloads if len(all_payloads) != 1 else all_payloads[0], indent=2))
        else:
            label = ",".join(scanned) if scanned else (targets[0] if targets else "unknown")
            explorer = all_payloads[0]["explorer"] if all_payloads else ""
            _print(format_report(
                args.address,
                label,
                explorer,
                all_rows,
                scanned=scanned,
                skipped=skipped,
                blockscan=blockscan,
            ))
            print("")

        if args.to_brain and all_payloads:
            _ingest_brain(cfg, args.address, all_payloads)
    finally:
        conn.close()
    return exit_code


def _ingest_brain(cfg: dict[str, Any], address: str, payloads: list[dict[str, Any]]) -> None:
    brain_dir = Path(cfg.get("second_brain_dir") or str(ROOT.parent / "second-brain"))
    brain_py = brain_dir / "brain.py"
    if not brain_py.exists():
        print(f"second-brain not found at {brain_py} — skip ingest.", file=sys.stderr)
        return
    chunks = [f"Funding scan for `{address}` on {now_iso()}."]
    for payload in payloads:
        chunks.append(f"Chain {payload['chain']}: {payload['counts']}")
        for row in payload["funding"]:
            chunks.append(
                f"- {row['category']}: {row['amount_display']} from `{row['from']}` "
                f"({row['resolved_name'] or 'unlabeled'}) tx `{row['tx_hash']}`"
            )
    text = "\n".join(chunks)
    tmp = ROOT / "last_funding_intel.md"
    tmp.write_text(text, encoding="utf-8")
    title = f"Funding scan {address[:10]}"
    url = payloads[0].get("explorer") or ""
    cmd = [
        sys.executable,
        str(brain_py),
        "add",
        "--file",
        str(tmp),
        "--title",
        title,
        "--tags",
        "funding,on-chain,butters-trace",
        "--category",
        "On-chain Evidence",
    ]
    if url:
        cmd.extend(["--url", url])
    subprocess.run(cmd, check=False)
    print(f"Ingested scan into second-brain ({brain_py}).")


def cmd_stats(cfg: dict[str, Any]) -> int:
    conn = connect(cfg)
    try:
        n = conn.execute("SELECT COUNT(*) c FROM scans").fetchone()["c"]
        labels = conn.execute("SELECT COUNT(*) c FROM user_labels").fetchone()["c"]
        print(f"Scans stored:   {n}")
        print(f"User labels:    {labels}")
        chains = build_chains(cfg)
        print("Enabled chains: " + (", ".join(f"{k} ({v.kind})" for k, v in chains.items()) or "(none)"))
        return 0
    finally:
        conn.close()


def cmd_list(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        rows = conn.execute(
            "SELECT id, scanned_at, chain, address, n_funding FROM scans ORDER BY id DESC LIMIT ?",
            (args.limit,),
        ).fetchall()
        if not rows:
            print("No scans stored yet.")
            return 0
        print(f"{'id':<5} {'when':<22} {'chain':<6} {'n':<4} address")
        for r in rows:
            print(f"{r['id']:<5} {r['scanned_at']:<22} {r['chain']:<6} {r['n_funding']:<4} {r['address']}")
        return 0
    finally:
        conn.close()


def cmd_show(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        row = conn.execute("SELECT report_json FROM scans WHERE id=?", (args.id,)).fetchone()
        if not row:
            print(f"No scan #{args.id}.", file=sys.stderr)
            return 1
        payload = json.loads(row["report_json"])
        if args.json:
            print(json.dumps(payload, indent=2))
            return 0
        print(f"# Funding sources for `{payload['address']}` ({payload['chain']})")
        print("")
        print(f"Stored {payload.get('scanned_at')}. Deposits: {len(payload.get('funding') or [])}")
        print("")
        grouped: dict[str, list[dict[str, Any]]] = {c: [] for c in CATEGORIES}
        for item in payload.get("funding") or []:
            grouped.setdefault(item.get("category") or "OTHER", []).append(item)
        for cat in CATEGORIES:
            print(f"## {cat}")
            print("")
            chunk = grouped.get(cat) or []
            if not chunk:
                print("_None found._")
                print("")
                continue
            print("| Time (UTC) | Amount | From | Name | Why |")
            print("|---|---|---|---|---|")
            for item in chunk:
                print(
                    f"| {item.get('timestamp') or '—'} | {item.get('amount_display')} | "
                    f"`{item.get('from')}` | {item.get('resolved_name') or '—'} | {item.get('reason')} |"
                )
            print("")
        return 0
    finally:
        conn.close()


def cmd_chains(cfg: dict[str, Any]) -> int:
    chains = build_chains(cfg)
    print("Enabled chains (add another EVM chain with a new config block, kind=evm):")
    print("")
    print("| id | kind | native | explorer |")
    print("|---|---|---|---|")
    for name, chain in chains.items():
        print(f"| {name} | {chain.kind} | {chain.native} | {chain.explorer} |")
    print("")
    groups = cfg.get("scan_groups") or {}
    if groups:
        print("Scan groups (`--chain eth` expands the EVM family; `--chain eth-only` is mainnet):")
        for name, members in groups.items():
            print(f"  {name}: {', '.join(members)}")
    print("")
    print("Blockscan (Etherscan family, all EVM in one page):")
    print(f"  {(cfg.get('blockscan') or 'https://blockscan.com').rstrip('/')}/address/<addr>")
    return 0


def cmd_label(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    cat = args.category.upper()
    if cat not in CATEGORIES:
        print(f"category must be one of {', '.join(CATEGORIES)}", file=sys.stderr)
        return 1
    conn = connect(cfg)
    try:
        conn.execute(
            """INSERT INTO user_labels(chain, address, name, category, notes, updated_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(chain, address) DO UPDATE SET
                 name=excluded.name, category=excluded.category,
                 notes=excluded.notes, updated_at=excluded.updated_at""",
            (args.chain.lower(), args.address.strip(), args.name, cat, args.notes or "", now_iso()),
        )
        conn.commit()
        print(f"Saved label {args.chain}:{args.address} → {cat} ({args.name})")
        return 0
    finally:
        conn.close()


def cmd_labels(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    conn = connect(cfg)
    book = load_labels(cfg, conn)
    conn.close()
    q = (args.query or "").lower()
    hits = []
    for key, meta in book.addresses.items():
        blob = f"{key} {meta.get('name')} {meta.get('category')}".lower()
        if q and q not in blob:
            continue
        hits.append((key, meta))
    hits.sort()
    if not hits:
        print("No labels matched.")
        return 0
    for key, meta in hits[: args.limit]:
        print(f"{meta['category']:<8} {key}  {meta['name']}")
    print(f"({min(len(hits), args.limit)} shown / {len(hits)} matched)")
    return 0


def format_search_report(title: str, result: dict[str, Any], rows: list[Transfer]) -> str:
    lines = [f"# {title}", ""]
    if result.get("blockscan"):
        lines.append(f"Blockscan: {result['blockscan']}")
    if result.get("blockscan_other"):
        lines.append(f"Other: {result['blockscan_other']}")
    scanned = result.get("scanned") or []
    skipped = result.get("skipped") or []
    if scanned:
        lines.append(f"Chains scanned: {', '.join(scanned)}")
    if skipped:
        lines.append(f"No activity / skipped: {', '.join(skipped)}")
    for err in result.get("errors") or []:
        lines.append(f"Error: {err}")
    lines += ["", f"Matches: **{len(rows)}**", ""]
    peers = result.get("counterparties") or []
    if peers:
        lines += [
            "## Counterparties",
            "",
            "| Name | Address | Chains | Deposited from CEX | Withdrawn to CEX | Volume |",
            "|---|---|---|---|---|---|",
        ]
        for peer in peers:
            lines.append(
                f"| {peer.get('name') or '—'} | `{peer.get('address')}` | "
                f"{', '.join(peer.get('chains') or [])} | {peer.get('deposited') or '—'} | "
                f"{peer.get('withdrawn') or '—'} | {peer.get('volume') or '—'} |"
            )
        lines.append("")
    if not rows:
        lines.append("_None found in the scanned window._")
        return "\n".join(lines)
    lines += [
        "## Transactions",
        "",
        "| Chain | Time (UTC) | Dir | Amount | Name | Counterparty | Tx |",
        "|---|---|---|---|---|---|---|",
    ]
    for tr in rows:
        tx = f"[tx]({tr.explorer_tx})" if tr.explorer_tx else tr.tx_hash[:10]
        peer = tr.counterparty or tr.from_address
        lines.append(
            f"| {tr.chain} | {tr.timestamp or '—'} | {tr.direction or '—'} | "
            f"{tr.amount_display()} | {tr.resolved_name or '—'} | `{peer}` | {tx} |"
        )
    return "\n".join(lines)


def _search_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "chain": args.chain,
        "limit": max(1, int(args.limit)),
        "token_mode": args.tokens,
    }


def cmd_cex_links(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        book = load_labels(cfg, conn)
        result = cex_links(cfg, conn, book, args.address, **_search_kwargs(args))
        rows = result["rows"]
        if args.json:
            print(json.dumps({**{k: v for k, v in result.items() if k != "rows"}, "transactions": [transfer_to_dict(t) for t in rows]}, indent=2))
        else:
            _print(format_search_report(f"CEX links for `{args.address}`", result, rows))
        return 0
    finally:
        conn.close()


def cmd_between(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        book = load_labels(cfg, conn)
        result = between_wallets(cfg, conn, book, args.address, args.other, **_search_kwargs(args))
        rows = result["rows"]
        if args.json:
            print(json.dumps({**{k: v for k, v in result.items() if k != "rows"}, "transactions": [transfer_to_dict(t) for t in rows]}, indent=2))
        else:
            _print(format_search_report(
                f"Transactions between `{args.address}` and `{args.other}`", result, rows
            ))
        return 0
    finally:
        conn.close()


def cmd_value(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    conn = connect(cfg)
    try:
        book = load_labels(cfg, conn)
        result = value_txs(
            cfg,
            conn,
            book,
            args.address,
            amount=float(args.amount),
            cmp=args.cmp,
            asset=args.asset or "",
            **_search_kwargs(args),
        )
        rows = result["rows"]
        if args.json:
            print(json.dumps({**{k: v for k, v in result.items() if k != "rows"}, "transactions": [transfer_to_dict(t) for t in rows]}, indent=2))
        else:
            label = {"gte": "at least", "lte": "at most", "eq": "exactly"}[args.cmp]
            asset = args.asset or "any asset"
            _print(format_search_report(
                f"Value search for `{args.address}` ({label} {args.amount} {asset})",
                result,
                rows,
            ))
        return 0
    finally:
        conn.close()


def cmd_serve(cfg: dict[str, Any], args: argparse.Namespace) -> int:
    from web.server import run_server
    return run_server(cfg, args.host, args.port)


def _add_scan_flags(parser: argparse.ArgumentParser, *, default_limit: int) -> None:
    parser.add_argument(
        "--chain",
        help="eth = ETH+L2s (default for 0x). eth-only = mainnet. sol = Solana. Comma-separated ids also work.",
    )
    parser.add_argument("--limit", type=int, default=default_limit, help="Max rows per chain.")
    parser.add_argument(
        "--tokens",
        choices=["native", "stables", "all"],
        default="stables",
        help="native = ETH/SOL only; stables = native + stablecoins (default); all = every token.",
    )
    parser.add_argument("--json", action="store_true")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Butters Trace - crypto address scanner")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    sub = p.add_subparsers(dest="command", required=True)

    f = sub.add_parser("funding", help="Find inbound deposits and classify CEX / DEFI / ONCHAIN / OTHER.")
    f.add_argument("address")
    _add_scan_flags(f, default_limit=40)
    f.add_argument("--to-brain", action="store_true", help="Ingest the scan into second-brain.")

    cl = sub.add_parser("cex-links", help="Find CEX counterparties linked (in or out) to an address.")
    cl.add_argument("address")
    _add_scan_flags(cl, default_limit=60)

    bt = sub.add_parser("between", help="Find transfers between two wallets.")
    bt.add_argument("address")
    bt.add_argument("other")
    _add_scan_flags(bt, default_limit=80)

    vl = sub.add_parser("value", help="Find transfers at/above/below an amount.")
    vl.add_argument("address")
    vl.add_argument("--amount", type=float, required=True)
    vl.add_argument("--cmp", choices=["gte", "lte", "eq"], default="gte")
    vl.add_argument("--asset", default="", help="Optional symbol filter, e.g. ETH or USDC.")
    _add_scan_flags(vl, default_limit=80)

    sv = sub.add_parser("serve", help="Local Trace UI (default http://127.0.0.1:8788).")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8788)

    sub.add_parser("stats", help="Scan counts and enabled chains.")
    ls = sub.add_parser("list", help="List stored scans.")
    ls.add_argument("--limit", type=int, default=20)
    sh = sub.add_parser("show", help="Show a stored scan.")
    sh.add_argument("--id", type=int, required=True)
    sh.add_argument("--json", action="store_true")
    sub.add_parser("chains", help="List enabled chains and how to add one.")

    lb = sub.add_parser("label", help="Teach a counterparty label (wins over the built-in book).")
    lb.add_argument("--address", required=True)
    lb.add_argument("--name", required=True)
    lb.add_argument("--category", required=True)
    lb.add_argument("--chain", required=True, help="eth, sol, or evm for all EVM chains.")
    lb.add_argument("--notes", default="")

    lq = sub.add_parser("labels", help="Search the label book.")
    lq.add_argument("--query", default="")
    lq.add_argument("--limit", type=int, default=40)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    cfg = load_config(Path(args.config))
    if args.command == "funding":
        return cmd_funding(cfg, args)
    if args.command == "cex-links":
        return cmd_cex_links(cfg, args)
    if args.command == "between":
        return cmd_between(cfg, args)
    if args.command == "value":
        return cmd_value(cfg, args)
    if args.command == "serve":
        return cmd_serve(cfg, args)
    if args.command == "stats":
        return cmd_stats(cfg)
    if args.command == "list":
        return cmd_list(cfg, args)
    if args.command == "show":
        return cmd_show(cfg, args)
    if args.command == "chains":
        return cmd_chains(cfg)
    if args.command == "label":
        return cmd_label(cfg, args)
    if args.command == "labels":
        return cmd_labels(cfg, args)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
