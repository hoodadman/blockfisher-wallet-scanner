const form = document.getElementById("search-form");
const statusEl = document.getElementById("status");
const resultsEl = document.getElementById("results");
const goBtn = document.getElementById("go");
const otherWrap = document.getElementById("other-wrap");
const valueWrap = document.getElementById("value-wrap");
const tokensWrap = document.getElementById("tokens-wrap");
const addrLabel = document.getElementById("addr-a-label");

let mode = "cex";

document.getElementById("address").addEventListener("input", () => {
  const addr = val("address");
  const chain = document.getElementById("chain");
  if (/^0x[a-fA-F0-9]{40}$/.test(addr)) {
    if (chain.value === "sol") chain.value = "eth";
  } else if (addr.length >= 32 && !addr.startsWith("0x")) {
    chain.value = "sol";
  }
});

document.querySelectorAll(".mode").forEach((btn) => {
  btn.addEventListener("click", () => {
    mode = btn.dataset.mode;
    document.querySelectorAll(".mode").forEach((b) => {
      b.classList.toggle("is-on", b === btn);
      b.setAttribute("aria-selected", b === btn ? "true" : "false");
    });
    otherWrap.classList.toggle("is-hidden", mode !== "between");
    valueWrap.classList.toggle("is-hidden", mode !== "value");
    tokensWrap.classList.toggle("is-hidden", mode === "deployments");
    addrLabel.textContent = mode === "between" ? "Wallet A" : "Wallet";
    document.getElementById("other").required = mode === "between";
    document.getElementById("amount").required = mode === "value";
    if (mode === "deployments") {
      const chain = document.getElementById("chain");
      const addr = val("address");
      if (/^0x[a-fA-F0-9]{40}$/.test(addr) && chain.value === "sol") chain.value = "eth";
      else if (addr.length >= 32 && !addr.startsWith("0x") && chain.value !== "sol") chain.value = "sol";
    }
  });
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const payload = {
    mode,
    address: val("address"),
    other: val("other"),
    amount: val("amount"),
    cmp: val("cmp"),
    asset: val("asset"),
    chain: val("chain"),
    tokens: val("tokens"),
    limit: 60,
  };
  setBusy(true, mode === "deployments" ? "Looking for deployed contracts…" : "Scanning chains… this can take a minute.");
  resultsEl.hidden = true;
  try {
    const res = await fetch("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Scan failed");
    render(data);
    setBusy(false, "");
  } catch (err) {
    setBusy(false, err.message || String(err), true);
  }
});

function val(id) {
  return (document.getElementById(id).value || "").trim();
}

function setBusy(busy, message, isError) {
  goBtn.disabled = busy;
  goBtn.textContent = busy ? "Scanning…" : "Run scan";
  if (!message) {
    statusEl.hidden = true;
    return;
  }
  statusEl.hidden = false;
  statusEl.textContent = message;
  statusEl.classList.toggle("is-error", Boolean(isError));
}

function shortAddr(addr) {
  if (!addr || addr.length < 12) return addr || "—";
  return `${addr.slice(0, 6)}…${addr.slice(-4)}`;
}

function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function render(data) {
  const txs = data.transactions || [];
  const peers = data.counterparties || [];
  const deployments = data.deployments || [];
  const titles = {
    cex: "CEX wallets linked to this address",
    between: "Transactions between the two wallets",
    value: "Transactions matching this value",
    deployments: "Token and contract deployments",
  };
  const scanned = (data.scanned || []).map((c) => `<span class="chip">${esc(c)}</span>`).join("");
  const skipped = (data.skipped || []).map((c) => `<span class="chip skip">${esc(c)}</span>`).join("");
  const errors = (data.errors || []).map((c) => `<span class="chip err">${esc(c)}</span>`).join("");
  const links = [];
  if (data.blockscan) links.push(`<a href="${esc(data.blockscan)}" target="_blank" rel="noreferrer">Blockscan</a>`);
  if (data.blockscan_other) links.push(`<a href="${esc(data.blockscan_other)}" target="_blank" rel="noreferrer">Other on Blockscan</a>`);

  let body = "";
  if (data.mode === "cex") {
    body = peers.length
      ? renderPeerTable(peers)
      : `<p class="empty">No labelled CEX counterparties in the scanned window.</p>`;
  } else if (data.mode === "deployments") {
    body = deployments.length
      ? renderDeploymentTable(deployments)
      : `<p class="empty">No contract deployments found on the chains we could scan. Try a single chain if you know where the token was minted.</p>`;
  } else if (!txs.length) {
    body = `<p class="empty">No matching transactions in the scanned window.</p>`;
  }

  const txTable = txs.length ? renderTxTable(txs, data.mode) : "";

  const metaCounts =
    data.mode === "deployments"
      ? `<span>${deployments.length} deployment${deployments.length === 1 ? "" : "s"}</span>`
      : `<span>${txs.length} transaction${txs.length === 1 ? "" : "s"}</span>
        <span>${peers.length === 1 ? "1 counterparty" : `${peers.length} counterparties`}</span>`;

  resultsEl.hidden = false;
  resultsEl.innerHTML = `
    <article class="card">
      <h2>${esc(titles[data.mode] || "Results")}</h2>
      <div class="meta">
        ${metaCounts}
        ${links.join(" · ")}
      </div>
      <div class="chips">${scanned}${skipped}${errors}</div>
    </article>
    <article class="card">${body}${txTable}</article>
  `;
}

function renderDeploymentTable(rows) {
  const body = rows
    .map((row) => {
      const ca = row.contract || "";
      const label = row.name || row.symbol || (row.kind === "token" ? "Token" : "Contract");
      const dex = row.dexscreener
        ? `<a href="${esc(row.dexscreener)}" target="_blank" rel="noreferrer">DexScreener</a>`
        : "—";
      const explorer = row.explorer
        ? `<a href="${esc(row.explorer)}" target="_blank" rel="noreferrer">Explorer</a>`
        : "—";
      const tx = row.explorer_tx
        ? `<a href="${esc(row.explorer_tx)}" target="_blank" rel="noreferrer">Deploy tx</a>`
        : "—";
      return `
        <tr>
          <td>${esc(row.chain)}</td>
          <td>${esc((row.timestamp || "").replace("T", " ").replace(".000000Z", "Z"))}</td>
          <td>${esc(label)}${row.symbol && row.name ? `<span class="sub">${esc(row.symbol)}</span>` : ""}</td>
          <td class="mono" title="${esc(ca)}">${esc(ca)}</td>
          <td>${explorer}</td>
          <td>${dex}</td>
          <td>${tx}</td>
        </tr>
      `;
    })
    .join("");
  return `
    <p class="note">EVM: contract creations from Blockscout (Etherscan when a key is set). Solana: SPL mints this wallet initialized via RPC. DexScreener opens when a pair exists.</p>
    <table>
      <thead>
        <tr>
          <th>Chain</th><th>Time (UTC)</th><th>Name</th><th>Contract (CA)</th>
          <th>Explorer</th><th>DexScreener</th><th>Tx</th>
        </tr>
      </thead>
      <tbody>${body}</tbody>
    </table>
  `;
}

function renderPeerTable(peers) {
  const rows = peers.map((p) => `
    <tr>
      <td class="cat-cex">${esc(p.name || "CEX")}</td>
      <td class="mono" title="${esc(p.address)}">${esc(shortAddr(p.address))}</td>
      <td>${esc((p.chains || []).join(", "))}</td>
      <td>${esc(p.deposited || "—")}<span class="sub">${p.in_count || 0} tx</span></td>
      <td>${esc(p.withdrawn || "—")}<span class="sub">${p.out_count || 0} tx</span></td>
      <td>${esc(p.volume || "—")}</td>
    </tr>
  `).join("");
  return `
    <h2>Exchange counterparties</h2>
    <table>
      <thead><tr><th>Name</th><th>Address</th><th>Chains</th><th>Deposited from CEX</th><th>Withdrawn to CEX</th><th>Volume</th></tr></thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

function renderTxTable(txs, mode) {
  const rows = txs.map((tx) => {
    const dir = tx.direction === "out" ? "out" : "in";
    const peer = tx.counterparty || (dir === "in" ? tx.from : tx.to);
    const txCell = tx.explorer_tx
      ? `<a href="${esc(tx.explorer_tx)}" target="_blank" rel="noreferrer">tx</a>`
      : esc((tx.tx_hash || "").slice(0, 10));
    return `
      <tr>
        <td>${esc(tx.chain)}</td>
        <td>${esc((tx.timestamp || "").replace("T", " ").replace(".000000Z", "Z"))}</td>
        <td class="dir-${dir}">${dir}</td>
        <td>${esc(tx.amount_display)}</td>
        <td>${esc(tx.resolved_name || "—")}</td>
        <td class="mono" title="${esc(peer)}">${esc(shortAddr(peer))}</td>
        <td>${txCell}</td>
      </tr>
    `;
  }).join("");
  const heading = mode === "cex" ? "<h2>Transactions</h2>" : "";
  return `
    ${heading}
    <table>
      <thead>
        <tr>
          <th>Chain</th><th>Time (UTC)</th><th>Dir</th><th>Amount</th>
          <th>Name</th><th>Counterparty</th><th>Tx</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}
