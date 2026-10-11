"""Solana adapter.

Transactions come from JSON-RPC (signatures + parsed txs). Solscan is used
as a label enrichment source and as the explorer URL the user asked for.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from classify import Transfer

BASE58_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
UA = "butters-trace/0.1 (financial-crime research)"
SYSTEM_PROGRAM = "11111111111111111111111111111111"
TOKEN_PROGRAMS = {
    "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb",
}


def _http_json(url: str, payload: Any | None = None, timeout: int = 30) -> Any:
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def nonempty_signatures(result: Any) -> list[dict[str, Any]] | None:
    """Successful signature rows, or None when this RPC should be skipped.

    A public endpoint can return HTTP 200 and ``[]`` for a wallet that has
    history. None tells the caller to try the next URL.
    """
    if not isinstance(result, list):
        return None
    rows = [row for row in result if isinstance(row, dict) and not row.get("err")]
    return rows or None


def _rpc(url: str, method: str, params: list[Any], timeout: int, req_id: int = 1) -> Any:
    body = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}
    data = _http_json(url, payload=body, timeout=timeout)
    if data.get("error"):
        raise RuntimeError(f"Solana RPC {method} error: {data['error']}")
    return data.get("result")


def _rpc_batch(url: str, calls: list[tuple[str, list[Any]]], timeout: int) -> list[Any]:
    payload = [
        {"jsonrpc": "2.0", "id": i, "method": method, "params": params}
        for i, (method, params) in enumerate(calls)
    ]
    data = _http_json(url, payload=payload, timeout=timeout)
    if isinstance(data, dict):
        data = [data]
    by_id = {item.get("id"): item for item in data if isinstance(item, dict)}
    out = []
    for i in range(len(calls)):
        item = by_id.get(i) or {}
        out.append(item.get("result") if not item.get("error") else None)
    return out


def _pubkey(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("pubkey") or value.get("publicKey") or ""
    return ""


class SolanaChain:
    kind = "solana"

    def __init__(self, name: str, cfg: dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.native = cfg.get("native", "SOL")
        self.decimals = int(cfg.get("decimals", 9))
        self.explorer = (cfg.get("explorer") or "").rstrip("/")
        self.rpc = cfg.get("rpc") or "https://api.mainnet-beta.solana.com"
        self.rpc_fallbacks = list(cfg.get("rpc_fallbacks") or [])
        self.solscan_public = (cfg.get("solscan_public") or "").rstrip("/")
        self.timeout = int(cfg.get("timeout_sec", 45))
        self._label_cache: dict[str, str] = {}

    def looks_like(self, address: str) -> bool:
        addr = (address or "").strip()
        return bool(BASE58_RE.match(addr)) and not addr.startswith("0x")

    def explorer_address(self, address: str) -> str:
        return f"{self.explorer}/account/{address}" if self.explorer else ""

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}" if self.explorer else ""

    def fetch_funding(self, address: str, limit: int, token_mode: str) -> list[Transfer]:
        return self.fetch_activity(address, limit, token_mode, direction="in")

    def fetch_activity(
        self,
        address: str,
        limit: int,
        token_mode: str,
        direction: str = "in",
    ) -> list[Transfer]:
        address = address.strip()
        rows: list[Transfer] = []
        before: str | None = None
        scanned = 0
        max_sigs = max(limit * 10, 20)
        while len(rows) < (limit * (2 if direction == "both" else 1)) and scanned < max_sigs:
            batch_n = min(20, max_sigs - scanned)
            sigs = self._signatures(address, batch_n, before=before)
            if not sigs:
                break
            txs = self._transactions([s["signature"] for s in sigs if s.get("signature")])
            for sig, tx in zip(sigs, txs):
                if not tx:
                    continue
                rows.extend(self._transfers_from_tx(address, sig, tx, token_mode, direction))
                cap = limit if direction != "both" else limit * 2
                if len(rows) >= cap:
                    break
            before = sigs[-1].get("signature")
            scanned += len(sigs)
            if len(sigs) < batch_n:
                break
            time.sleep(0.1)
        labels_needed = set()
        for r in rows:
            if r.from_address:
                labels_needed.add(r.from_address)
            if r.to_address:
                labels_needed.add(r.to_address)
        self._enrich_labels(labels_needed)
        for row in rows:
            row.from_label = self._label_cache.get(row.from_address, row.from_label)
            row.to_label = self._label_cache.get(row.to_address, row.to_label)
        rows.sort(key=lambda t: t.timestamp or "", reverse=True)
        cap = limit if direction != "both" else limit * 2
        return rows[:cap]

    def fetch_token_deployments(self, address: str, limit: int = 40) -> list[dict[str, Any]]:
        """SPL mints this wallet initialized (mint or freeze authority, or fee payer)."""
        address = address.strip()
        cap = max(1, min(200, limit))
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        before: str | None = None
        max_sigs = min(600, max(cap * 30, 40))
        scanned = 0
        while len(out) < cap and scanned < max_sigs:
            batch_n = min(25, max_sigs - scanned)
            sigs = self._signatures(address, batch_n, before=before)
            if not sigs:
                break
            signatures = [s["signature"] for s in sigs if s.get("signature")]
            txs = self._transactions(signatures)
            for sig, tx in zip(sigs, txs):
                if not tx or len(out) >= cap:
                    continue
                for dep in deployments_from_solana_tx(self, address, sig, tx):
                    mint = dep.get("contract") or ""
                    if not mint or mint in seen:
                        continue
                    seen.add(mint)
                    out.append(dep)
                    if len(out) >= cap:
                        break
            before = sigs[-1].get("signature")
            scanned += len(sigs)
            if len(sigs) < batch_n:
                break
            time.sleep(0.12)
        out.sort(key=lambda row: row.get("timestamp") or "", reverse=True)
        return out[:cap]

    def _rpc_urls(self) -> list[str]:
        return [self.rpc, *[u for u in self.rpc_fallbacks if u and u != self.rpc]]

    def _signatures(self, address: str, limit: int, before: str | None = None) -> list[dict[str, Any]]:
        opts: dict[str, Any] = {"limit": min(max(limit, 1), 1000)}
        if before:
            opts["before"] = before
        last_error: Exception | None = None
        saw_ok = False
        for url in self._rpc_urls():
            try:
                result = _rpc(
                    url,
                    "getSignaturesForAddress",
                    [address, opts],
                    timeout=self.timeout,
                )
            except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                last_error = exc
                continue
            saw_ok = True
            rows = nonempty_signatures(result)
            if rows:
                return rows
        if not saw_ok:
            raise RuntimeError(f"Solana RPC getSignaturesForAddress failed: {last_error}")
        return []

    def _transactions(self, signatures: list[str]) -> list[Any]:
        out: list[Any] = []
        opts = {
            "encoding": "jsonParsed",
            "maxSupportedTransactionVersion": 0,
            "commitment": "confirmed",
        }
        last_error: Exception | None = None
        for i, sig in enumerate(signatures):
            fetched = False
            for url in self._rpc_urls():
                try:
                    result = _rpc(url, "getTransaction", [sig, opts], timeout=self.timeout)
                except (urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as exc:
                    last_error = exc
                    if "429" in str(exc) or "Too Many" in str(exc):
                        time.sleep(1.5)
                    continue
                if result:
                    out.append(result)
                    fetched = True
                    break
            if not fetched:
                out.append(None)
            if i + 1 < len(signatures):
                time.sleep(0.05)
        return out

    def _transfers_from_tx(
        self,
        scanned: str,
        sig: dict[str, Any],
        tx: dict[str, Any],
        token_mode: str,
        direction: str = "in",
    ) -> list[Transfer]:
        meta = tx.get("meta") or {}
        if meta.get("err"):
            return []
        tx_hash = sig.get("signature") or ""
        timestamp = _unix_ts(tx.get("blockTime") or sig.get("blockTime"))
        involved = _program_ids(tx)
        rows: list[Transfer] = []
        keys = _account_keys(tx)
        post_tokens = _token_balances_by_account(meta, keys)
        for ix in _iter_parsed(tx):
            program_id = ix.get("programId") or ""
            program_name = ix.get("program") or ""
            parsed = ix.get("parsed")
            if not isinstance(parsed, dict):
                continue
            info = parsed.get("info") or {}
            ix_type = parsed.get("type") or ""
            if (program_id == SYSTEM_PROGRAM or program_name == "system") and ix_type == "transfer":
                dest = info.get("destination") or ""
                source = info.get("source") or ""
                lamports = int(info.get("lamports") or 0)
                if dest == scanned and source != scanned and lamports > 0 and direction in ("in", "both"):
                    rows.append(Transfer(
                        chain=self.name,
                        tx_hash=tx_hash,
                        timestamp=timestamp,
                        from_address=source,
                        to_address=dest,
                        amount_raw=str(lamports),
                        decimals=self.decimals,
                        asset=self.native,
                        from_is_contract=False,
                        kind="native",
                        explorer_tx=self.explorer_tx(tx_hash),
                        involved=involved,
                    ))
                if source == scanned and dest != scanned and lamports > 0 and direction in ("out", "both"):
                    rows.append(Transfer(
                        chain=self.name,
                        tx_hash=tx_hash,
                        timestamp=timestamp,
                        from_address=source,
                        to_address=dest,
                        amount_raw=str(lamports),
                        decimals=self.decimals,
                        asset=self.native,
                        from_is_contract=False,
                        kind="native",
                        explorer_tx=self.explorer_tx(tx_hash),
                        involved=involved,
                    ))
            elif (program_id in TOKEN_PROGRAMS or program_name in ("spl-token", "spl-token-2022")) and ix_type in ("transfer", "transferChecked"):
                if token_mode == "native":
                    continue
                dest_account = info.get("destination") or ""
                source_auth = info.get("authority") or ""
                source_account = info.get("source") or ""
                entry = post_tokens.get(dest_account) or {}
                dest_owner = entry.get("owner") or ""
                amount = _token_amount(info)
                if amount <= 0:
                    continue
                mint = entry.get("mint") or info.get("mint") or ""
                decimals = int(((entry.get("uiTokenAmount") or {}).get("decimals")) or info.get("decimals") or 6)
                symbol = _short_mint(mint)
                if dest_owner == scanned and source_auth != scanned and direction in ("in", "both"):
                    rows.append(Transfer(
                        chain=self.name,
                        tx_hash=tx_hash,
                        timestamp=timestamp,
                        from_address=source_auth or source_account,
                        to_address=scanned,
                        amount_raw=str(amount),
                        decimals=decimals,
                        asset=symbol,
                        from_is_contract=False,
                        kind="token",
                        explorer_tx=self.explorer_tx(tx_hash),
                        involved=involved,
                    ))
                if source_auth == scanned and dest_owner != scanned and direction in ("out", "both"):
                    rows.append(Transfer(
                        chain=self.name,
                        tx_hash=tx_hash,
                        timestamp=timestamp,
                        from_address=scanned,
                        to_address=dest_owner or dest_account,
                        amount_raw=str(amount),
                        decimals=decimals,
                        asset=symbol,
                        from_is_contract=False,
                        kind="token",
                        explorer_tx=self.explorer_tx(tx_hash),
                        involved=involved,
                    ))
        return rows

    def _enrich_labels(self, addresses: set[str]) -> None:
        if not self.solscan_public or not addresses:
            return
        for addr in addresses:
            if addr in self._label_cache:
                continue
            try:
                data = _http_json(
                    f"{self.solscan_public}/account/{addr}",
                    timeout=min(self.timeout, 15),
                )
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
                continue
            label = ""
            if isinstance(data, dict):
                label = (
                    data.get("accountLabel")
                    or (data.get("data") or {}).get("accountLabel")
                    or data.get("tag")
                    or data.get("name")
                    or ""
                )
            if label:
                self._label_cache[addr] = str(label)


def _program_ids(tx: dict[str, Any]) -> list[str]:
    seen: list[str] = []
    for ix in _iter_parsed(tx):
        pid = ix.get("programId") or ""
        if pid and pid not in seen:
            seen.append(pid)
    return seen


def _account_keys(tx: dict[str, Any]) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = [_pubkey(k) for k in message.get("accountKeys") or []]
    loaded = (tx.get("meta") or {}).get("loadedAddresses") or {}
    keys.extend(_pubkey(k) for k in loaded.get("writable") or [])
    keys.extend(_pubkey(k) for k in loaded.get("readonly") or [])
    return [k for k in keys if k]


def _token_balances_by_account(meta: dict[str, Any], keys: list[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for entry in meta.get("postTokenBalances") or []:
        idx = entry.get("accountIndex")
        if idx is None or idx >= len(keys):
            continue
        out[keys[idx]] = entry
    return out


def _short_mint(mint: str) -> str:
    if not mint:
        return "TOKEN"
    known = {
        "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v": "USDC",
        "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB": "USDT",
        "So11111111111111111111111111111111111111112": "wSOL",
        "USD1ttGY1N17NEEHLmELoaybftRBUSErhqYiQzvEmuB": "USD1",
    }
    return known.get(mint, mint[:4] + ".." + mint[-4:])


def _iter_parsed(tx: dict[str, Any]):
    message = (tx.get("transaction") or {}).get("message") or {}
    for ix in message.get("instructions") or []:
        yield ix
    for inner in (tx.get("meta") or {}).get("innerInstructions") or []:
        for ix in inner.get("instructions") or []:
            yield ix


def _token_amount(info: dict[str, Any]) -> int:
    if "tokenAmount" in info and isinstance(info["tokenAmount"], dict):
        try:
            return int(info["tokenAmount"].get("amount") or 0)
        except (TypeError, ValueError):
            return 0
    try:
        return int(info.get("amount") or 0)
    except (TypeError, ValueError):
        return 0


def _unix_ts(value: Any) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(value or "")


MINT_IX_TYPES = frozenset({"initializeMint", "initializeMint2"})


def dexscreener_url_solana(mint: str) -> str:
    addr = (mint or "").strip()
    if addr:
        return f"https://dexscreener.com/solana/{addr}"
    return "https://dexscreener.com/"


def _is_fee_payer(address: str, tx: dict[str, Any]) -> bool:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys") or []
    if not keys:
        return False
    return _pubkey(keys[0]) == address


def deployment_from_mint_instruction(
    chain: SolanaChain,
    wallet: str,
    tx_hash: str,
    timestamp: str,
    ix_type: str,
    info: dict[str, Any],
    *,
    fee_payer: bool,
) -> dict[str, Any] | None:
    if ix_type not in MINT_IX_TYPES:
        return None
    mint = (info.get("mint") or "").strip()
    if not mint:
        return None
    mint_auth = (info.get("mintAuthority") or info.get("authority") or "").strip()
    freeze = (info.get("freezeAuthority") or "").strip()
    if mint_auth and mint_auth != wallet and freeze != wallet and not fee_payer:
        return None
    symbol = _short_mint(mint)
    return {
        "chain": chain.name,
        "contract": mint,
        "name": "",
        "symbol": symbol if symbol != "TOKEN" else "",
        "timestamp": timestamp,
        "tx_hash": tx_hash,
        "explorer": chain.explorer_address(mint),
        "explorer_tx": chain.explorer_tx(tx_hash),
        "dexscreener": dexscreener_url_solana(mint),
        "kind": "token",
    }


def deployments_from_solana_tx(
    chain: SolanaChain,
    wallet: str,
    sig: dict[str, Any],
    tx: dict[str, Any],
) -> list[dict[str, Any]]:
    meta = tx.get("meta") or {}
    if meta.get("err"):
        return []
    wallet = wallet.strip()
    tx_hash = sig.get("signature") or ""
    timestamp = _unix_ts(tx.get("blockTime") or sig.get("blockTime"))
    fee_payer = _is_fee_payer(wallet, tx)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ix in _iter_parsed(tx):
        program_id = ix.get("programId") or ""
        program_name = ix.get("program") or ""
        if program_id not in TOKEN_PROGRAMS and program_name not in ("spl-token", "spl-token-2022"):
            continue
        parsed = ix.get("parsed")
        if not isinstance(parsed, dict):
            continue
        ix_type = parsed.get("type") or ""
        info = parsed.get("info") or {}
        dep = deployment_from_mint_instruction(
            chain,
            wallet,
            tx_hash,
            timestamp,
            ix_type,
            info,
            fee_payer=fee_payer,
        )
        if not dep:
            continue
        mint = dep["contract"]
        if mint in seen:
            continue
        seen.add(mint)
        out.append(dep)
    return out
