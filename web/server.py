"""Local HTTP UI for Butters Trace."""

from __future__ import annotations

import json
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from queries import between_wallets, cex_links, token_deployments, value_txs

WEB_ROOT = Path(__file__).resolve().parent / "static"
FRAME_ANCESTORS = " ".join(
    [
        "'self'",
        "http://blockfisher.co.uk",
        "https://blockfisher.co.uk",
        "http://www.blockfisher.co.uk",
        "https://www.blockfisher.co.uk",
        "https://*.onrender.com",
        "http://127.0.0.1:8090",
        "http://localhost:8090",
    ]
)


def run_server(cfg: dict[str, Any], host: str, port: int) -> int:
    handler = _handler_for(cfg)
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"Butters Trace UI: {url}", file=sys.stderr)
    print("Blockscan is linked on every result; scans go through each chain indexer.", file=sys.stderr)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
    return 0


def _handler_for(cfg: dict[str, Any]):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
                return
            if path == "/api/meta":
                self._json(200, _meta(cfg))
                return
            rel = path.lstrip("/")
            target = (WEB_ROOT / rel).resolve()
            if WEB_ROOT.resolve() not in target.parents and target != WEB_ROOT.resolve():
                self._json(403, {"error": "forbidden"})
                return
            if target.is_file():
                mime, _ = mimetypes.guess_type(str(target))
                self._file(target, mime or "application/octet-stream")
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            path = urlparse(self.path).path
            if path != "/api/search":
                self._json(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > 100_000:
                self._json(413, {"error": "payload too large"})
                return
            try:
                body = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON"})
                return
            try:
                payload = _run_search(cfg, body)
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                self._json(500, {"error": str(exc)})
                return
            self._json(200, payload)

        def _file(self, path: Path, content_type: str) -> None:
            data = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            if content_type.startswith("text/html"):
                self.send_header("Content-Security-Policy", f"frame-ancestors {FRAME_ANCESTORS}")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            data = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

    return Handler


def _meta(cfg: dict[str, Any]) -> dict[str, Any]:
    from chains import build_chains

    chains = build_chains(cfg)
    return {
        "blockscan": (cfg.get("blockscan") or "https://blockscan.com").rstrip("/"),
        "chains": [
            {
                "id": name,
                "kind": chain.kind,
                "native": chain.native,
                "indexed": bool(getattr(chain, "blockscout", None) or getattr(chain, "rpc", None)),
            }
            for name, chain in chains.items()
        ],
        "scan_groups": cfg.get("scan_groups") or {},
    }


def _run_search(cfg: dict[str, Any], body: dict[str, Any]) -> dict[str, Any]:
    from trace import connect, load_labels, transfer_to_dict

    mode = (body.get("mode") or "").strip().lower()
    address = (body.get("address") or "").strip()
    if not address:
        raise ValueError("Paste a wallet address.")
    chain = (body.get("chain") or "").strip() or None
    token_mode = body.get("tokens") or "stables"
    if token_mode not in ("native", "stables", "all"):
        token_mode = "stables"
    try:
        limit = max(1, min(200, int(body.get("limit") or 60)))
    except (TypeError, ValueError):
        limit = 60

    conn = connect(cfg)
    try:
        book = load_labels(cfg, conn)
        kwargs = {"chain": chain, "limit": limit, "token_mode": token_mode}
        if mode == "cex":
            result = cex_links(cfg, conn, book, address, **kwargs)
        elif mode == "between":
            other = (body.get("other") or "").strip()
            if not other:
                raise ValueError("Paste the second wallet address.")
            result = between_wallets(cfg, conn, book, address, other, **kwargs)
        elif mode == "value":
            try:
                amount = float(body.get("amount"))
            except (TypeError, ValueError):
                raise ValueError("Enter an amount, for example 0.5 or 100.") from None
            if amount < 0:
                raise ValueError("Amount cannot be negative.")
            cmp = (body.get("cmp") or "gte").strip().lower()
            if cmp not in ("gte", "lte", "eq"):
                cmp = "gte"
            asset = (body.get("asset") or "").strip()
            result = value_txs(
                cfg, conn, book, address, amount=amount, cmp=cmp, asset=asset, **kwargs
            )
        elif mode == "deployments":
            result = token_deployments(cfg, conn, book, address, chain=chain, limit=limit)
        else:
            raise ValueError("Choose CEX links, counterparty, value, or token deployments.")
    finally:
        conn.close()

    rows = result.pop("rows")
    return {
        "mode": mode,
        "address": address,
        "scanned": result.get("scanned") or [],
        "skipped": result.get("skipped") or [],
        "errors": result.get("errors") or [],
        "blockscan": result.get("blockscan") or "",
        "blockscan_other": result.get("blockscan_other") or "",
        "other": result.get("other") or "",
        "counterparties": result.get("counterparties") or [],
        "transactions": [transfer_to_dict(tr) for tr in rows],
        "deployments": result.get("deployments") or [],
        "amount": result.get("amount"),
        "cmp": result.get("cmp"),
        "asset_filter": result.get("asset_filter") or "",
    }
