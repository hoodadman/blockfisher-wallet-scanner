# Butters Trace

On-chain address scanner for crypto financial-crime work. Lives under
**Butters Crypto Manager** with Copytrade.

Second Brain is the knowledge store (ingest/query only). Trace is the live scanner.
They stay separate so a scan cannot be confused with stored intel. Hand a scan
across with `--to-brain` when you want it remembered.

## Why not bake this into Second Brain?

Second Brain answers **only from what has been stored** and never hits the chain.
Funding classification is live I/O, chain-specific, and will grow (hops, cluster
analysis, exposure). A second function on the same agent would break that contract.
Trace writes optional intel *into* Second Brain instead.

## Local UI

Three searches on a wallet, across the EVM family Blockscan covers (Blockscout per chain — Blockscan itself blocks bots). Open:

```powershell
python trace.py serve
```

Then http://127.0.0.1:8788

1. **CEX links** — exchange wallets that sent to or received from the address
2. **Counterparty** — transactions between two wallets
3. **Value** — transactions at least / exactly / at most an amount (optional asset filter)

Same searches from the CLI: `cex-links`, `between`, `value`.

## First function: funding

Inbound deposits into a wallet, classified:

| Bucket | Meaning |
|---|---|
| **CEX** | Centralised exchange (Binance, Coinbase, Kraken, …) |
| **DEFI** | Protocol / app (Uniswap, Jupiter, Hyperliquid, …) |
| **ONCHAIN** | Unlabeled wallet, no org or app link |
| **OTHER** | Mixers, bridges, unlabeled contracts, unclear |

```powershell
python trace.py funding 0xa010d3f2a041c5baa5fb759a5c1639bf0d007170
python trace.py funding 0xa010... --chain eth
python trace.py funding 0xa010... --chain eth-only
python trace.py funding <sol-address> --chain sol
python trace.py funding <address> --to-brain
```

`--chain eth` (and a bare `0x` address) scans the **EVM family** that Blockscan covers: Ethereum, Base, Arbitrum, Optimism, Polygon, BSC, Avalanche, Linea, Blast, Scroll, HyperEVM. `--chain eth-only` is Ethereum mainnet alone.

## Sources

**EVM (in this order)**

1. **Blockscout v2** per chain — free JSON, inbound filter. This is how Trace actually pulls txs. Its nametags are often empty on CEX hot wallets.
2. **Etherscan public name tag** — for unlabeled EVM counterparties, Trace reads the Etherscan address-page title (e.g. `Kraken 44 | Address: 0x…`). CEX wallets reuse the same 0x across L2s, so a mainnet tag applies to an Arbitrum deposit. Cached in `trace.db`. The paid nametag API is not used.
3. **Blockscan** (`https://blockscan.com/address/<addr>` or `/tx/<hash>`) — Etherscan's all-chains page. Linked on every EVM report. Their site blocks bots, so it is not scraped.
4. **Etherscan API v2** — fallback when `etherscan_api_key` is set; one key, `chainid` per network. Needed for BSC / Avalanche / Linea / Blast / HyperEVM until a free Blockscout instance is wired.
5. **Local label book** + `label` command.

**SOL**

1. **Solana JSON-RPC** — `getSignaturesForAddress` + parsed `getTransaction` (public RPC + publicnode fallback).
2. **Solscan** — explorer URLs, optional public-API nametag enrichment.
3. Invoked program IDs (Jupiter, Raydium, …) mark a deposit **DEFI** even when the counterparty account is unlabeled.

### Suggested extras (not wired in yet)

| Tool | Use instead of explorers when… |
|---|---|
| **Arkham** | You need entity attribution (Binance cluster, fund, OTC). Best free labeling. |
| **MetaSleuth** | You want a fund-flow graph, especially cross-chain. |
| **DeBank** | Weak for *who funded this*. Strong for *which DeFi apps this wallet uses* — a later Trace function. |
| **Helius** | Paid Solana RPC; parsed txs and webhooks if public RPC rate-limits you. |
| **SolanaFM** | Alternate SOL explorer with decent program labels. |

Each funding report already prints Arkham / MetaSleuth / DeBank (ETH) or SolanaFM (SOL) links.

## Add a chain

Copy an EVM block in `config.json` and add its id to `scan_groups.eth`.

Hyperliquid deposits live on Arbitrum / HyperEVM — both are already in the `eth` scan group.

## Files

- `trace.py` — CLI
- `queries.py` — CEX / between / value searches (in + out)
- `web/` — local UI (`trace.py serve`)
- `classify.py` — CEX / DEFI / ONCHAIN / OTHER
- `chains/` — adapters (`evm`, `solana`)
- `labels/book.json` — seed CEX + DeFi addresses and name patterns
- `run_trace.ps1` — OpenClaw runner
- `trace.db` — stored scans + user labels (created on first run)

## Other commands

```powershell
python trace.py serve
python trace.py cex-links 0xa010...
python trace.py between 0xa010... 0x490b...
python trace.py value 0xa010... --amount 0.02 --cmp gte --asset ETH
python trace.py stats
python trace.py list
python trace.py show --id 1
python trace.py chains
python trace.py labels --query binance
python trace.py label --chain eth --address 0x... --name "Binance 22" --category CEX
python import_cex.py
```

`import_cex.py` refreshes the label book from Dune's public CEX address registry and the published Etherscan-family exchange label scrape. It only appends addresses that are not already in `labels/book.json`.
