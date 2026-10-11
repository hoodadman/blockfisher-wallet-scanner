"""Etherscan-family public name tags for unlabeled EVM counterparties.

Blockscout often has no nametag even when Blockscan/Etherscan does
(Kraken 44 on Arbitrum is the example that broke a scan). The nametag
API is paid. Blockscan/Arbiscan HTML is Cloudflare-blocked. Etherscan
address pages still return a public ``<title>`` we can parse:

    Kraken 44 | Address: 0x490b1e68... | Etherscan

CEX hot wallets reuse the same 0x across L2s, so a mainnet Etherscan
lookup is the right free source for an Arbitrum deposit.
"""

from __future__ import annotations

import html
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Iterable

from classify import LabelBook, Transfer, normalise_evm

TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
OG_TITLE_RE = re.compile(
    r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\'](.*?)["\']',
    re.I | re.S,
)
UNLABELED_RE = re.compile(r"^(address|ethereum account|eth account)\b", re.I)
BLOCKED_RE = re.compile(
    r"just a moment|attention required|access denied|cloudflare|forbidden|captcha",
    re.I,
)
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)


def parse_etherscan_title(page: str) -> str | None:
    """Return the public name tag from an Etherscan address page, or None."""
    if not page:
        return None
    raw = ""
    m = TITLE_RE.search(page)
    if m:
        raw = m.group(1)
    if not raw:
        m = OG_TITLE_RE.search(page)
        if m:
            raw = m.group(1)
    title = re.sub(r"\s+", " ", html.unescape(raw)).strip()
    if not title or BLOCKED_RE.search(title):
        return None
    first = title.split("|", 1)[0].strip()
    if not first or UNLABELED_RE.match(first):
        return None
    if first.lower() in {"etherscan", "blockscan", "arbiscan"}:
        return None
    if len(first) > 80:
        return None
    return first


def apply_nametags(
    rows: list[Transfer],
    book: LabelBook,
    conn: sqlite3.Connection,
    cfg: dict,
) -> int:
    """Fill empty ``from_label`` from cached / fetched Etherscan name tags.

    Returns how many transfers received a new label this call.
    """
    if cfg.get("etherscan_nametag", True) is False:
        return 0
    needed: list[str] = []
    seen: set[str] = set()

    def _consider(chain: str, address: str, label: str) -> None:
        if label or not (address or "").startswith("0x"):
            return
        if book.lookup(chain, address):
            return
        addr = normalise_evm(address)
        if addr in seen:
            return
        seen.add(addr)
        needed.append(addr)

    for tr in rows:
        _consider(tr.chain, tr.from_address, tr.from_label)
        _consider(tr.chain, tr.to_address, tr.to_label)
    if not needed:
        return 0

    tags = resolve_nametags(needed, conn, cfg)
    tagged = 0
    for tr in rows:
        if not tr.from_label and tr.from_address.startswith("0x"):
            name = tags.get(normalise_evm(tr.from_address))
            if name:
                tr.from_label = name
                tagged += 1
        if not tr.to_label and tr.to_address.startswith("0x"):
            name = tags.get(normalise_evm(tr.to_address))
            if name:
                tr.to_label = name
                tagged += 1
        if tr.counterparty and not tr.counterparty_label and tr.counterparty.startswith("0x"):
            tr.counterparty_label = tags.get(normalise_evm(tr.counterparty), "") or tr.counterparty_label
    return tagged


def resolve_nametags(
    addresses: Iterable[str],
    conn: sqlite3.Connection,
    cfg: dict,
) -> dict[str, str]:
    """address -> name for addresses that have a public Etherscan tag."""
    _ensure_cache(conn)
    ttl_days = int(cfg.get("nametag_ttl_days", 30))
    max_fetch = int(cfg.get("nametag_max_lookups", 40))
    timeout = int(cfg.get("nametag_timeout_sec", 20))
    pause = float(cfg.get("nametag_pause_sec", 0.35))
    template = cfg.get("etherscan_nametag_url") or "https://etherscan.io/address/{address}"
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, ttl_days))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")

    out: dict[str, str] = {}
    missing: list[str] = []
    cached_hit = cached_empty = 0
    for addr in addresses:
        row = conn.execute(
            "SELECT name, fetched_at FROM nametag_cache WHERE address=?",
            (addr,),
        ).fetchone()
        if row and (row["fetched_at"] or "") >= cutoff:
            if row["name"]:
                out[addr] = row["name"]
                cached_hit += 1
            else:
                cached_empty += 1
            continue
        missing.append(addr)

    fetched = tagged_now = 0
    blocked = False
    for addr in missing[:max_fetch]:
        if blocked:
            break
        if fetched:
            time.sleep(pause)
        name, ok, blocked = _fetch_one(template.format(address=addr), timeout)
        fetched += 1
        if not ok:
            continue
        _store(conn, addr, name or "")
        if name:
            out[addr] = name
            tagged_now += 1

    if fetched or cached_hit or cached_empty:
        print(
            f"nametag: {cached_hit} tagged + {cached_empty} empty from cache, "
            f"{fetched} Etherscan lookups, {tagged_now} new tags"
            + (" (stopped: explorer blocked the lookup)" if blocked else ""),
            file=sys.stderr,
        )
    return out


def _fetch_one(url: str, timeout: int) -> tuple[str | None, bool, bool]:
    """Returns (name, ok, blocked). ok=False means do not cache."""
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if raw[:2] == b"\x1f\x8b":
                import gzip
                try:
                    raw = gzip.decompress(raw)
                except OSError:
                    pass
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429, 503):
            print(f"warn: Etherscan nametag lookup blocked ({exc.code})", file=sys.stderr)
            return None, False, True
        print(f"warn: Etherscan nametag lookup failed: {exc}", file=sys.stderr)
        return None, False, False
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"warn: Etherscan nametag lookup failed: {exc}", file=sys.stderr)
        return None, False, False
    page = raw.decode("utf-8", errors="replace")
    if BLOCKED_RE.search(page[:2000]):
        print("warn: Etherscan nametag lookup hit a challenge page", file=sys.stderr)
        return None, False, True
    return parse_etherscan_title(page), True, False


def _store(conn: sqlite3.Connection, address: str, name: str) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    conn.execute(
        """INSERT INTO nametag_cache(address, name, source, fetched_at)
           VALUES (?,?,?,?)
           ON CONFLICT(address) DO UPDATE SET
             name=excluded.name, source=excluded.source, fetched_at=excluded.fetched_at""",
        (address, name, "etherscan", now),
    )
    conn.commit()


def _ensure_cache(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS nametag_cache (
            address    TEXT PRIMARY KEY,
            name       TEXT NOT NULL DEFAULT '',
            source     TEXT NOT NULL DEFAULT 'etherscan',
            fetched_at TEXT NOT NULL
        )"""
    )
    conn.commit()
