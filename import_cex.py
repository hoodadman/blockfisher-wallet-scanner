"""Pull public CEX address lists into labels/book.json.

Two sources, both public:

* Dune Spellbook's maintained registry (cex_evms_addresses and
  cex_solana_addresses). This is the same list the book was originally
  seeded from, and it keeps growing.
* The published Etherscan-family label scrape in
  brianleect/etherscan-labels (exchange / hot-wallet / brand files).
  Live explorer pages are behind Cloudflare, so the scrape dump is the
  copy we can actually read.

Existing book rows are left as they are. New addresses are appended.
"""

from __future__ import annotations

import argparse
import json
import re
import urllib.request
from pathlib import Path
from typing import Iterable

from classify import _looks_like_dex, normalise_evm

ROOT = Path(__file__).resolve().parent
BOOK_PATH = ROOT / "labels" / "book.json"

DUNE_EVM = (
    "https://raw.githubusercontent.com/duneanalytics/spellbook/main/"
    "dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/"
    "cex_evms_addresses.sql"
)
DUNE_SOL = (
    "https://raw.githubusercontent.com/duneanalytics/spellbook/main/"
    "dbt_subprojects/hourly_spellbook/models/_sector/cex/addresses/chains/"
    "solana/cex_solana_addresses.sql"
)
BITFINEX_WALLETS = "https://raw.githubusercontent.com/bitfinexcom/pub/main/wallets.txt"
SCRAPE_API = "https://api.github.com/repos/brianleect/etherscan-labels/contents/data"
SCRAPE_RAW = "https://raw.githubusercontent.com/brianleect/etherscan-labels/main/data"
SCRAPE_CHAINS = (
    "etherscan",
    "bscscan",
    "polygonscan",
    "arbiscan",
    "optimism",
    "avalanche",
    "ftmscan",
)
GENERIC_STEMS = {"exchange", "hot-wallet", "hotwallet"}
SKIP_STEM_PARTS = ("pegged", "deposit", "token", "delegate", "canary", "fiat")
REJECT_NAME = ("phishing", "fake", "airdrop", "claim", "pegged", "token")

EVM_ROW = re.compile(
    r"\(0x(?P<addr>[0-9a-fA-F]{40})\s*,\s*'(?P<cex>(?:[^']|'')*)'\s*,\s*'(?P<name>(?:[^']|'')*)'"
)
BITFINEX_SOL_ROW = re.compile(
    r"^([1-9A-HJ-NP-Za-km-z]{32,44})\s+-\s+(?P<label>.*Solana.*)$",
    re.I,
)
SOL_ROW = re.compile(
    r"\('solana'\s*,\s*'(?P<addr>[1-9A-HJ-NP-Za-km-z]{32,44})'\s*,\s*"
    r"'(?P<cex>(?:[^']|'')*)'\s*,\s*'(?P<name>(?:[^']|'')*)'"
)
# Public CEX lists sometimes include a DEX contract under an exchange brand.
# Book lookup would then outrank the live "DEX Router" label.
DENY_ADDRESSES = {
    "0x6b2c0c7be2048daa9b5527982c29f48062b34d58",  # OKX: DEX Router, listed as OKX 138
}
UA = {"User-Agent": "butters-trace/cex-import"}


def _sql_text(value: str) -> str:
    return value.replace("''", "'").strip()


def parse_dune_evm(sql: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in EVM_ROW.finditer(sql):
        addr = "0x" + match.group("addr").lower()
        if addr in seen or addr in DENY_ADDRESSES or addr == "0x" + "0" * 40:
            continue
        seen.add(addr)
        name = _sql_text(match.group("name")) or _sql_text(match.group("cex"))
        if not name or _rejected(name):
            continue
        rows.append({"chain": "evm", "address": addr, "name": name, "category": "CEX"})
    return rows


def parse_dune_sol(sql: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for match in SOL_ROW.finditer(sql):
        addr = match.group("addr")
        if addr in seen:
            continue
        seen.add(addr)
        name = _sql_text(match.group("name")) or _sql_text(match.group("cex"))
        if not name or _rejected(name):
            continue
        rows.append({"chain": "sol", "address": addr, "name": name, "category": "CEX"})
    return rows


def _norm_label(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _cex_needles(patterns: list[dict]) -> list[str]:
    needles = []
    for pat in patterns:
        if (pat.get("category") or "").upper() != "CEX":
            continue
        needle = _norm_label(pat.get("match") or "")
        if needle:
            needles.append(needle)
    return needles


def _rejected(name: str) -> bool:
    blob = name.lower()
    return _looks_like_dex(blob) or any(word in blob for word in REJECT_NAME)


def _matches_cex(name: str, needles: list[str]) -> bool:
    blob = _norm_label(name)
    return any(needle in blob for needle in needles)


def accept_scrape_row(stem: str, name: str, needles: list[str]) -> bool:
    """Keep a scraped nametag when the file or the name is a known exchange."""
    if not name or _rejected(name):
        return False
    stem_norm = _norm_label(stem)
    if stem_norm in {_norm_label(s) for s in GENERIC_STEMS}:
        return _matches_cex(name, needles)
    return _matches_cex(stem, needles) or _matches_cex(name, needles)


def parse_bitfinex_sol(text: str) -> list[dict[str, str]]:
    """Bitfinex publishes its own Solana hot and cold wallets."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        match = BITFINEX_SOL_ROW.match(line.strip())
        if not match:
            continue
        addr = match.group(1)
        if addr in seen:
            continue
        seen.add(addr)
        label = re.sub(r"\s+", " ", match.group("label")).strip()
        rows.append({
            "chain": "sol",
            "address": addr,
            "name": f"Bitfinex {label}",
            "category": "CEX",
        })
    return rows


def parse_scrape_json(stem: str, payload: dict, needles: list[str]) -> list[dict[str, str]]:
    rows = []
    if not isinstance(payload, dict):
        return rows
    for address, name in payload.items():
        if not isinstance(address, str) or not address.startswith("0x") or len(address) != 42:
            continue
        if normalise_evm(address) in DENY_ADDRESSES:
            continue
        label = str(name or "").strip()
        if not accept_scrape_row(stem, label, needles):
            continue
        rows.append({
            "chain": "evm",
            "address": normalise_evm(address),
            "name": label,
            "category": "CEX",
        })
    return rows


def merge_new(existing: list[dict], incoming: Iterable[dict]) -> list[dict]:
    """Append addresses the book does not already contain. Never rename a row."""
    known: set[tuple[str, str]] = set()
    for row in existing:
        chain = (row.get("chain") or "").lower()
        addr = row.get("address") or ""
        if addr.startswith("0x"):
            known.add(("evm", normalise_evm(addr)))
        else:
            known.add((chain, addr))
    added: list[dict] = []
    for row in incoming:
        chain = row["chain"]
        addr = row["address"]
        key = ("evm", addr) if chain == "evm" else (chain, addr)
        if key in known:
            continue
        known.add(key)
        added.append(row)
    added.sort(key=lambda row: (row["name"].lower(), row["chain"], row["address"]))
    return added


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def _scrape_files(needles: list[str]) -> list[tuple[str, str]]:
    """Return (stem, raw_url) for brand and generic exchange label files."""
    generic = {_norm_label(s) for s in GENERIC_STEMS}
    found: list[tuple[str, str]] = []
    for chain in SCRAPE_CHAINS:
        listing = json.loads(_get(f"{SCRAPE_API}/{chain}/accounts").decode("utf-8"))
        for item in listing:
            name = item.get("name") or ""
            if not name.endswith(".json"):
                continue
            stem = name[:-5].lower()
            if any(part in stem for part in SKIP_STEM_PARTS):
                continue
            stem_norm = _norm_label(stem)
            if stem_norm not in generic and not any(needle in stem_norm for needle in needles):
                continue
            found.append((stem, f"{SCRAPE_RAW}/{chain}/accounts/{name}"))
    return found


def collect(patterns: list[dict]) -> tuple[list[dict], dict[str, int]]:
    needles = _cex_needles(patterns)
    evm = parse_dune_evm(_get(DUNE_EVM).decode("utf-8"))
    sol = parse_dune_sol(_get(DUNE_SOL).decode("utf-8"))
    try:
        sol.extend(parse_bitfinex_sol(_get(BITFINEX_WALLETS).decode("utf-8")))
    except Exception:
        pass
    scraped: list[dict] = []
    files = 0
    for stem, url in _scrape_files(needles):
        try:
            payload = json.loads(_get(url).decode("utf-8"))
        except Exception:
            continue
        rows = parse_scrape_json(stem, payload, needles)
        if rows:
            files += 1
            scraped.extend(rows)
    # Dune wins when both sources label the same new address.
    ordered = evm + sol + scraped
    stats = {
        "dune_evm": len(evm),
        "dune_sol": len(sol),
        "scrape_files": files,
        "scrape_rows": len(scraped),
    }
    return ordered, stats


def _dump_book(path: Path, data: dict) -> None:
    lines = ["{"]
    lines.append(f'  "_comment": {json.dumps(data.get("_comment") or "", ensure_ascii=False)},')
    lines.append('  "addresses": [')
    addresses = data.get("addresses") or []
    for i, entry in enumerate(addresses):
        comma = "," if i < len(addresses) - 1 else ""
        lines.append("    " + json.dumps(entry, ensure_ascii=False, separators=(", ", ": ")) + comma)
    lines.append("  ],")
    lines.append('  "patterns": [')
    patterns = data.get("patterns") or []
    for i, entry in enumerate(patterns):
        comma = "," if i < len(patterns) - 1 else ""
        lines.append("    " + json.dumps(entry, ensure_ascii=False, separators=(", ", ": ")) + comma)
    lines.append("  ]")
    lines.append("}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import public CEX addresses into the label book.")
    parser.add_argument("--book", type=Path, default=BOOK_PATH)
    parser.add_argument("--dry-run", action="store_true", help="Download and report, do not write.")
    args = parser.parse_args(argv)

    book = json.loads(args.book.read_text(encoding="utf-8"))
    incoming, stats = collect(book.get("patterns") or [])
    added = merge_new(book.get("addresses") or [], incoming)
    print(
        f"Dune EVM {stats['dune_evm']}, Dune Solana {stats['dune_sol']}, "
        f"scrape {stats['scrape_rows']} from {stats['scrape_files']} files"
    )
    print(f"Skipped (already in the book or repeated): {len(incoming) - len(added)}")
    print(f"New addresses: {len(added)}")
    if added:
        brands: dict[str, int] = {}
        for row in added:
            brand = row["name"].split(":", 1)[0].split()[0]
            brands[brand] = brands.get(brand, 0) + 1
        top = sorted(brands.items(), key=lambda item: (-item[1], item[0]))[:12]
        print("Top new labels: " + ", ".join(f"{name} {count}" for name, count in top))
    if args.dry_run:
        print("Dry run; book not written.")
        return 0
    book["addresses"] = list(book.get("addresses") or []) + added
    book["_comment"] = (
        "Exact addresses win over name patterns. Add via trace.py label, edit this file, "
        "or run python import_cex.py. chain=evm applies to every EVM chain. "
        "CEX rows come from Dune Spellbook cex_evms_addresses / cex_solana_addresses "
        "and the public Etherscan-family exchange label scrape (brianleect/etherscan-labels)."
    )
    _dump_book(args.book, book)
    print(f"Wrote {args.book}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
