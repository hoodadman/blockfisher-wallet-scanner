"""EVM adapter (Ethereum now; Base/Arbitrum/BSC later via config only).

Primary indexer: Blockscout v2 (free, inbound txs). Blockscout nametags are
often empty even when Blockscan/Etherscan has one — Trace fills those after
fetch via ``nametag.py``.
Fallback: Etherscan-compatible account API when an API key is set.
Explorer URLs in the report still point at the chain explorer (or whatever
``explorer`` is configured to).
"""

from __future__ import annotations

import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from classify import Transfer, canonical_asset, normalise_evm

ETH_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")
UA = "butters-trace/0.1 (financial-crime research)"

# Canonical USDT on each chain. Arbitrum and one Optimism deployment use the
# USDT0 ticker, which is still Tether USD. Queried directly so spam token
# transfers cannot crowd them out of the native-ETH window.
DEXSCREENER_SLUG = {
    "eth": "ethereum",
    "base": "base",
    "arb": "arbitrum",
    "op": "optimism",
    "polygon": "polygon",
    "bsc": "bsc",
    "avax": "avalanche",
    "linea": "linea",
    "blast": "blast",
    "scroll": "scroll",
}

USDT_BY_CHAIN = {
    "eth": ["0xdac17f958d2ee523a2206206994597c13d831ec7"],
    "base": ["0xfde4c96c8593536e31f229ea8f37b2ada2699bb2"],
    "op": [
        "0x94b008aa00579c1307b0ef2c499ad98a8ce58e58",
        "0x01bff41798a0bcf287b996046ca68b395dbc1071",
    ],
    "arb": ["0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"],
}


def _http_json(url: str, timeout: int = 30, headers: dict[str, str] | None = None) -> Any:
    """GET JSON. Blockscout sits behind Cloudflare, which rejects urllib.

    ``curl_cffi`` impersonates Chrome's TLS handshake. Fall back to urllib
    when that package is not installed.
    """
    hdrs = {"Accept": "application/json", **(headers or {})}
    try:
        from curl_cffi import requests as creq
    except ImportError:
        creq = None
    if creq is not None:
        # Leave the User-Agent to Chrome impersonation. A custom bot UA
        # makes Cloudflare challenge the request even when TLS looks like Chrome.
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                resp = creq.get(
                    url,
                    headers=hdrs,
                    impersonate="chrome",
                    timeout=timeout if attempt == 0 else max(timeout, 40),
                )
            except Exception as exc:  # noqa: BLE001 — curl timeouts are not urllib errors
                last_exc = exc
                continue
            if resp.status_code >= 400:
                raise urllib.error.HTTPError(
                    url, resp.status_code, resp.reason or "HTTP error", hdrs=None, fp=None
                )
            return resp.json()
        raise urllib.error.URLError(str(last_exc or "blockscout request failed"))
    req = urllib.request.Request(url, headers={"User-Agent": UA, **hdrs})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def _metadata_tag_names(value: dict[str, Any]) -> list[str]:
    """Name tags Blockscout stores under metadata, not in ``name``.

    Kraken and MEXC hot wallets often have ``name: null`` and the label only
    in ``metadata.tags`` (for example ``MEXC 15`` plus a generic ``Exchange``).
    """
    meta = value.get("metadata") or {}
    tags = meta.get("tags") if isinstance(meta, dict) else None
    names: list[str] = []
    if not isinstance(tags, list):
        return names
    for tag in tags:
        if not isinstance(tag, dict):
            continue
        label = (tag.get("name") or tag.get("display_name") or "").strip()
        if not label or label.lower().startswith("note_"):
            continue
        if label not in names:
            names.append(label)
    return names


def _addr_field(value: Any) -> tuple[str, str, bool]:
    """Unpack Blockscout address objects or plain strings -> hash, name, is_contract."""
    if value is None:
        return "", "", False
    if isinstance(value, str):
        return normalise_evm(value), "", False
    hash_ = normalise_evm(value.get("hash") or value.get("address") or "")
    name = (value.get("name") or value.get("ens_domain_name") or "") or ""
    tag_names: list[str] = []
    for tag in value.get("public_tags") or []:
        if isinstance(tag, dict):
            tag_names.append(tag.get("display_name") or tag.get("label") or "")
        else:
            tag_names.append(str(tag))
    tag_names.extend(_metadata_tag_names(value))
    candidates = [x for x in dict.fromkeys([name, *tag_names]) if x]
    label = _best_label(candidates)
    is_contract = bool(value.get("is_contract"))
    return hash_, label, is_contract


_CEX_HINTS = (
    "binance", "coinbase", "kraken", "okx", "okex", "bybit", "kucoin", "mexc",
    "bitget", "gemini", "gate.io", "gateio", "huobi", "htx", "bitfinex",
    "bitstamp", "crypto.com", "robinhood", "revolut",
)
_DEFI_HINTS = ("dex", "router", "pool", "swap", "amm", "uniswap", "sushi")


def _best_label(candidates: list[str]) -> str:
    """Pick one counterparty name. A DEX router beats a bare exchange brand."""
    if not candidates:
        return ""

    def hinted(words: tuple[str, ...]) -> str:
        hits = [c for c in candidates if any(w in c.lower() for w in words)]
        if not hits:
            return ""
        return min(hits, key=len)

    dex = hinted(_DEFI_HINTS)
    cex = hinted(_CEX_HINTS)
    if dex:
        return dex
    return cex or candidates[0]


def _is_cex_label(label: str) -> bool:
    """True for an exchange hot-wallet tag, not a DEX router that mentions the brand."""
    blob = (label or "").lower()
    if not blob or any(word in blob for word in ("dex", "router", "amm", "uniswap", "sushi")):
        return False
    return any(word in blob for word in _CEX_HINTS)


def _cap_by_kind(rows: list[Transfer], limit: int) -> list[Transfer]:
    """Keep up to ``limit`` of each kind, newest first.

    Token transfers must not push an older native CEX deposit out of the window.
    """
    ordered = sorted(rows, key=lambda t: t.timestamp or "", reverse=True)
    kept: list[Transfer] = []
    counts: dict[str, int] = {}
    for row in ordered:
        kind = row.kind or "native"
        if _is_cex_label(row.from_label) or _is_cex_label(row.to_label):
            kept.append(row)
            continue
        if counts.get(kind, 0) >= limit:
            continue
        counts[kind] = counts.get(kind, 0) + 1
        kept.append(row)
    return kept


def _page_query(params: dict[str, Any] | None) -> str:
    if not params:
        return ""
    clean = {k: v for k, v in params.items() if v is not None}
    return urllib.parse.urlencode(clean)


def _page_transfers(fetch_page, parse_item, limit: int, direction: str) -> list[Transfer]:
    """Page Blockscout, keeping exchange-tagged rows past the normal window.

    Newest token spam was hiding older Binance deposits that sat on page 2.
    """
    ordinary: list[Transfer] = []
    flagged: list[Transfer] = []
    params: dict[str, Any] | None = {"filter": "to" if direction == "in" else "from"}
    for _ in range(8):
        data = fetch_page(params)
        items = data.get("items") or []
        if not items:
            break
        for item in items:
            tr = parse_item(item)
            if tr is None:
                continue
            peer = tr.from_label if direction == "in" else tr.to_label
            if _is_cex_label(peer):
                flagged.append(tr)
            elif len(ordinary) < limit:
                ordinary.append(tr)
        params = data.get("next_page_params")
        if not params:
            break
    return _dedupe(flagged + ordinary)


class EvmChain:
    kind = "evm"

    def __init__(self, name: str, cfg: dict[str, Any]) -> None:
        self.name = name
        self.cfg = cfg
        self.native = cfg.get("native", "ETH")
        self.decimals = int(cfg.get("decimals", 18))
        self.explorer = cfg.get("explorer", "").rstrip("/")
        self.blockscout = (cfg.get("blockscout") or "").rstrip("/")
        self.etherscan_api = (cfg.get("etherscan_api") or "").rstrip("/")
        self.etherscan_key = cfg.get("etherscan_api_key") or ""
        self.chain_id = cfg.get("chain_id")
        self.timeout = int(cfg.get("timeout_sec", 30))

    def looks_like(self, address: str) -> bool:
        return bool(ETH_RE.match((address or "").strip()))

    def explorer_address(self, address: str) -> str:
        return f"{self.explorer}/address/{address}" if self.explorer else ""

    def explorer_tx(self, tx_hash: str) -> str:
        return f"{self.explorer}/tx/{tx_hash}" if self.explorer else ""

    def has_activity(self, address: str) -> bool | None:
        """True/False when Blockscout knows; None means 'scan anyway'."""
        address = normalise_evm(address)
        if not self.blockscout:
            return None if self.etherscan_key else False
        try:
            data = _http_json(
                f"{self.blockscout}/addresses/{address}",
                timeout=min(self.timeout, 15),
            )
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return False
            print(f"warn: {self.name} activity probe failed: {exc}", file=sys.stderr)
            return None
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"warn: {self.name} activity probe failed: {exc}", file=sys.stderr)
            return None
        if not isinstance(data, dict):
            return None
        if _int(data.get("coin_balance") or "0") > 0:
            return True
        if data.get("has_token_transfers") or data.get("has_logs") or data.get("has_tokens"):
            return True
        return False

    def fetch_funding(self, address: str, limit: int, token_mode: str) -> list[Transfer]:
        return self.fetch_activity(address, limit, token_mode, direction="in")

    def fetch_activity(
        self,
        address: str,
        limit: int,
        token_mode: str,
        direction: str = "in",
    ) -> list[Transfer]:
        address = normalise_evm(address)
        sides = ["in", "out"] if direction == "both" else [direction]
        rows: list[Transfer] = []
        per_side = max(1, limit)
        for side in sides:
            chunk: list[Transfer] = []
            if self.blockscout:
                chunk.extend(self._from_blockscout(address, per_side, token_mode, side))
            if len(chunk) < per_side and self.etherscan_api and self.etherscan_key:
                chunk.extend(self._from_etherscan(address, per_side, token_mode, side))
            rows.extend(_cap_by_kind(chunk, per_side))
        rows.sort(key=lambda t: t.timestamp or "", reverse=True)
        return _dedupe(rows)

    def fetch_token_deployments(self, address: str, limit: int = 40) -> list[dict[str, Any]]:
        """Contracts this wallet created on this chain (token mints when the indexer knows)."""
        address = normalise_evm(address)
        cap = max(1, min(200, limit))
        rows: list[dict[str, Any]] = []
        if self.blockscout:
            rows.extend(self._deployments_from_blockscout(address, cap))
        elif self.etherscan_api and self.etherscan_key:
            rows.extend(self._deployments_from_etherscan(address, cap))
        return rows[:cap]

    def _deployments_from_blockscout(self, address: str, limit: int) -> list[dict[str, Any]]:
        url = f"{self.blockscout}/addresses/{address}/transactions"
        params: dict[str, Any] | None = {"filter": "from"}
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for _ in range(8):
            if len(out) >= limit:
                break
            query = f"{url}?{_page_query(params)}" if _page_query(params) else url
            try:
                data = _http_json(query, timeout=self.timeout)
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                print(f"warn: {self.name} deployment fetch failed: {exc}", file=sys.stderr)
                break
            for item in data.get("items") or []:
                dep = deployment_from_blockscout_item(item, self, address)
                if not dep:
                    continue
                key = dep["contract"].lower()
                if key in seen:
                    continue
                seen.add(key)
                enrich_token_meta(self, dep)
                out.append(dep)
                if len(out) >= limit:
                    break
            params = data.get("next_page_params")
            if not params:
                break
        return out

    def _deployments_from_etherscan(self, address: str, limit: int) -> list[dict[str, Any]]:
        params = {
            "module": "account",
            "action": "txlist",
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": max(limit, 100),
            "sort": "desc",
            "apikey": self.etherscan_key,
        }
        if self.chain_id:
            params["chainid"] = self.chain_id
        qs = urllib.parse.urlencode(params)
        try:
            data = _http_json(f"{self.etherscan_api}?{qs}", timeout=self.timeout)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"warn: {self.name} etherscan deployment fetch failed: {exc}", file=sys.stderr)
            return []
        result = data.get("result")
        if not isinstance(result, list):
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in result:
            dep = deployment_from_etherscan_item(item, self, address)
            if not dep:
                continue
            key = dep["contract"].lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(dep)
            if len(out) >= limit:
                break
        return out

    def outgoing_peers(self, address: str, limit: int = 6) -> list[tuple[str, str]]:
        """Where this address sent native or ERC-20 value next.

        Exchange deposit addresses are unlabeled, then forward to the hot wallet.
        """
        if not self.blockscout:
            return []
        address = normalise_evm(address)
        found: list[tuple[str, str]] = []
        seen: set[str] = set()
        pages = (
            f"{self.blockscout}/addresses/{address}/transactions?filter=from",
            f"{self.blockscout}/addresses/{address}/token-transfers?filter=from&type=ERC-20",
        )
        for url in pages:
            try:
                data = _http_json(url, timeout=min(self.timeout, 20))
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
                continue
            for item in (data.get("items") or [])[:limit]:
                to_hash, to_label, _ = _addr_field(item.get("to") or {})
                if not to_hash or to_hash == address or to_hash in seen:
                    continue
                if "token" in url:
                    value = _int(((item.get("total") or {}).get("value")) or item.get("amount") or "0")
                else:
                    value = _int(item.get("value") or "0")
                if value <= 0:
                    continue
                seen.add(to_hash)
                found.append((to_hash, to_label))
        return found

    def _from_blockscout(self, address: str, limit: int, token_mode: str, direction: str = "in") -> list[Transfer]:
        out: list[Transfer] = []
        out.extend(self._safe_pages(
            f"{self.blockscout}/addresses/{address}/transactions", address, "native", limit, direction
        ))
        if len(out) < limit:
            out.extend(self._safe_pages(
                f"{self.blockscout}/addresses/{address}/internal-transactions", address, "internal", limit, direction
            ))
        if token_mode != "native":
            out.extend(self._safe_token_pages(address, limit, direction))
            if token_mode == "stables":
                for token in USDT_BY_CHAIN.get(self.name, []):
                    out.extend(self._safe_token_pages(address, limit, direction, token=token))
        return _dedupe(out)

    def _safe_pages(self, url: str, scanned: str, kind: str, limit: int, direction: str = "in") -> list[Transfer]:
        try:
            return self._bs_pages(url, scanned, kind, limit, direction)
        except urllib.error.HTTPError as exc:
            if exc.code in (403, 429, 503):
                raise
            print(f"warn: {self.name} {kind} fetch failed: {exc}", file=sys.stderr)
            return []
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"warn: {self.name} {kind} fetch failed: {exc}", file=sys.stderr)
            return []

    def _safe_token_pages(
        self,
        scanned: str,
        limit: int,
        direction: str = "in",
        token: str = "",
    ) -> list[Transfer]:
        try:
            return self._bs_token_pages(scanned, limit, direction, token=token)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
            print(f"warn: {self.name} token fetch failed: {exc}", file=sys.stderr)
            return []

    def _bs_pages(self, url: str, scanned: str, kind: str, limit: int, direction: str = "in") -> list[Transfer]:
        def _one(item: dict[str, Any]) -> Transfer | None:
            return self._bs_native_item(item, scanned, kind, direction)

        return _page_transfers(
            lambda params: _http_json(
                f"{url}?{_page_query(params)}" if _page_query(params) else url,
                timeout=self.timeout,
            ),
            _one,
            limit,
            direction,
        )

    def _bs_native_item(self, item: dict[str, Any], scanned: str, kind: str, direction: str = "in") -> Transfer | None:
        to_hash, to_label, to_contract = _addr_field(item.get("to") or item.get("created_contract"))
        from_hash, from_label, from_contract = _addr_field(item.get("from"))
        if not from_hash or not to_hash or from_hash == to_hash:
            return None
        if direction == "in" and (to_hash != scanned or from_hash == scanned):
            return None
        if direction == "out" and (from_hash != scanned or to_hash == scanned):
            return None
        value = _int(item.get("value") or item.get("amount") or "0")
        if value <= 0:
            return None
        status = (item.get("status") or "ok").lower()
        if status not in ("ok", "success", "1", ""):
            return None
        tx_hash = item.get("hash") or item.get("transaction_hash") or ""
        return Transfer(
            chain=self.name,
            tx_hash=tx_hash,
            timestamp=_ts(item.get("timestamp")),
            from_address=from_hash,
            to_address=to_hash,
            amount_raw=str(value),
            decimals=self.decimals,
            asset=self.native,
            from_label=from_label,
            from_is_contract=from_contract,
            to_label=to_label,
            to_is_contract=to_contract,
            kind=kind,
            explorer_tx=self.explorer_tx(tx_hash),
        )

    def _bs_token_pages(
        self,
        scanned: str,
        limit: int,
        direction: str = "in",
        token: str = "",
    ) -> list[Transfer]:
        url = f"{self.blockscout}/addresses/{scanned}/token-transfers"
        token = normalise_evm(token) if token else ""

        def _fetch(params: dict[str, Any] | None) -> Any:
            query = dict(params or {})
            query["type"] = "ERC-20"
            if token:
                query["token"] = token
            return _http_json(f"{url}?{_page_query(query)}", timeout=self.timeout)

        def _one(item: dict[str, Any]) -> Transfer | None:
            return self._bs_token_item(item, scanned, direction, forced_asset="USDT" if token else "")

        return _page_transfers(_fetch, _one, limit, direction)

    def _bs_token_item(
        self,
        item: dict[str, Any],
        scanned: str,
        direction: str = "in",
        forced_asset: str = "",
    ) -> Transfer | None:
        to_hash, to_label, to_contract = _addr_field(item.get("to"))
        from_hash, from_label, from_contract = _addr_field(item.get("from"))
        if not from_hash or not to_hash or from_hash == to_hash:
            return None
        if direction == "in" and (to_hash != scanned or from_hash == scanned):
            return None
        if direction == "out" and (from_hash != scanned or to_hash == scanned):
            return None
        token = item.get("token") or {}
        contract = normalise_evm(token.get("address_hash") or token.get("address") or "")
        if contract and contract in USDT_BY_CHAIN.get(self.name, []):
            symbol = "USDT"
        elif forced_asset:
            symbol = forced_asset
        else:
            symbol = canonical_asset(token.get("symbol") or "") or "TOKEN"
        decimals = _int(token.get("decimals") or 18)
        total = item.get("total") or {}
        value = _int(total.get("value") or item.get("amount") or "0")
        if value <= 0:
            return None
        tx_hash = item.get("tx_hash") or item.get("transaction_hash") or ""
        return Transfer(
            chain=self.name,
            tx_hash=tx_hash,
            timestamp=_ts(item.get("timestamp")),
            from_address=from_hash,
            to_address=to_hash,
            amount_raw=str(value),
            decimals=decimals if decimals else 18,
            asset=symbol,
            from_label=from_label,
            from_is_contract=from_contract,
            to_label=to_label,
            to_is_contract=to_contract,
            kind="token",
            explorer_tx=self.explorer_tx(tx_hash),
        )

    def _from_etherscan(self, address: str, limit: int, token_mode: str, direction: str = "in") -> list[Transfer]:
        if not self.etherscan_key:
            return []
        out: list[Transfer] = []
        out.extend(self._es_list("txlist", address, limit, "native", direction))
        out.extend(self._es_list("txlistinternal", address, limit, "internal", direction))
        if token_mode != "native":
            out.extend(self._es_list("tokentx", address, limit, "token", direction))
        return _dedupe(out)

    def _es_list(self, action: str, address: str, limit: int, kind: str, direction: str = "in") -> list[Transfer]:
        params = {
            "module": "account",
            "action": action,
            "address": address,
            "startblock": 0,
            "endblock": 99999999,
            "page": 1,
            "offset": max(limit, 100),
            "sort": "desc",
            "apikey": self.etherscan_key,
        }
        if self.chain_id:
            params["chainid"] = self.chain_id
        qs = urllib.parse.urlencode(params)
        data = _http_json(f"{self.etherscan_api}?{qs}", timeout=self.timeout)
        result = data.get("result")
        if not isinstance(result, list):
            return []
        out: list[Transfer] = []
        for item in result:
            to_hash = normalise_evm(item.get("to") or "")
            from_hash = normalise_evm(item.get("from") or "")
            if not from_hash or not to_hash or from_hash == to_hash:
                continue
            if direction == "in" and (to_hash != address or from_hash == address):
                continue
            if direction == "out" and (from_hash != address or to_hash == address):
                continue
            value = _int(item.get("value") or "0")
            if value <= 0:
                continue
            if str(item.get("isError") or "0") not in ("0", ""):
                continue
            tx_hash = item.get("hash") or item.get("transactionHash") or ""
            symbol = item.get("tokenSymbol") or self.native
            decimals = _int(item.get("tokenDecimal") or self.decimals)
            out.append(Transfer(
                chain=self.name,
                tx_hash=tx_hash,
                timestamp=_unix_ts(item.get("timeStamp")),
                from_address=from_hash,
                to_address=to_hash,
                amount_raw=str(value),
                decimals=decimals if decimals else self.decimals,
                asset=symbol if kind == "token" else self.native,
                from_label="",
                from_is_contract=False,
                kind=kind,
                explorer_tx=self.explorer_tx(tx_hash),
            ))
            if len(out) >= limit:
                break
        return out


def dexscreener_url(chain_name: str, contract: str) -> str:
    slug = DEXSCREENER_SLUG.get(chain_name)
    ca = normalise_evm(contract)
    if slug and ca:
        return f"https://dexscreener.com/{slug}/{ca}"
    if ca:
        return f"https://dexscreener.com/search?q={ca}"
    return "https://dexscreener.com/"


def deployment_from_blockscout_item(
    item: dict[str, Any],
    chain: EvmChain,
    deployer: str,
) -> dict[str, Any] | None:
    from_hash, _, _ = _addr_field(item.get("from"))
    if from_hash != deployer:
        return None
    status = (item.get("status") or "ok").lower()
    if status not in ("ok", "success", "1", ""):
        return None
    created = item.get("created_contract")
    ca = ""
    label = ""
    if isinstance(created, dict):
        ca, label, _ = _addr_field(created)
    if not ca:
        ca = normalise_evm(item.get("contract_address_hash") or item.get("contract_address") or "")
    if not ca or ca == deployer:
        return None
    tx_hash = item.get("hash") or item.get("transaction_hash") or ""
    return {
        "chain": chain.name,
        "contract": ca,
        "name": label or "",
        "symbol": "",
        "timestamp": _ts(item.get("timestamp")),
        "tx_hash": tx_hash,
        "explorer": chain.explorer_address(ca),
        "explorer_tx": chain.explorer_tx(tx_hash),
        "dexscreener": dexscreener_url(chain.name, ca),
        "kind": "contract",
    }


def deployment_from_etherscan_item(
    item: dict[str, Any],
    chain: EvmChain,
    deployer: str,
) -> dict[str, Any] | None:
    from_hash = normalise_evm(item.get("from") or "")
    if from_hash != deployer:
        return None
    if str(item.get("isError") or "0") not in ("0", ""):
        return None
    ca = normalise_evm(item.get("contractAddress") or "")
    to_field = (item.get("to") or "").strip()
    if not ca:
        return None
    if to_field and normalise_evm(to_field):
        return None
    tx_hash = item.get("hash") or item.get("transactionHash") or ""
    return {
        "chain": chain.name,
        "contract": ca,
        "name": "",
        "symbol": "",
        "timestamp": _unix_ts(item.get("timeStamp")),
        "tx_hash": tx_hash,
        "explorer": chain.explorer_address(ca),
        "explorer_tx": chain.explorer_tx(tx_hash),
        "dexscreener": dexscreener_url(chain.name, ca),
        "kind": "contract",
    }


def enrich_token_meta(chain: EvmChain, dep: dict[str, Any]) -> None:
    if not chain.blockscout:
        return
    ca = dep.get("contract") or ""
    if not ca:
        return
    try:
        data = _http_json(f"{chain.blockscout}/tokens/{ca}", timeout=min(chain.timeout, 12))
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, ValueError):
        return
    if not isinstance(data, dict):
        return
    symbol = (data.get("symbol") or "").strip()
    name = (data.get("name") or "").strip()
    if symbol:
        dep["symbol"] = symbol
        dep["kind"] = "token"
    if name:
        dep["name"] = name
    elif symbol and not dep.get("name"):
        dep["name"] = symbol


def _int(value: Any) -> int:
    try:
        return int(str(value).split(".")[0] or "0")
    except (TypeError, ValueError):
        return 0


def _ts(value: Any) -> str:
    if not value:
        return ""
    return str(value).replace(" ", "T")


def _unix_ts(value: Any) -> str:
    try:
        from datetime import datetime, timezone
        return datetime.fromtimestamp(int(value), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError, OSError):
        return str(value or "")


def _dedupe(rows: list[Transfer]) -> list[Transfer]:
    seen: set[tuple[str, str, str, str, str]] = set()
    out: list[Transfer] = []
    for row in rows:
        key = (row.tx_hash, row.from_address, row.to_address, row.amount_raw, row.asset)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    return out
