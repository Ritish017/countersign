const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, opts = {}) => {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  const d = await r.json();
  if (!r.ok) throw new Error(d.detail || r.statusText);
  return d;
};
let selected = null;
const LABEL = { paid: "Paid", payout_pending: "Payout pending", awaiting_countersign: "Needs countersign",
  awaiting_buyer_approval: "Approve on PayPal", blocked: "Blocked", rejected: "Rejected", failed: "Failed", extracted: "Processing" };
const HEAD = { AUTO_PAY: "Paid within your mandate", NEEDS_COUNTERSIGN: "Waiting for your countersignature", BLOCK: "Blocked by your mandate" };
const STATUS_HEAD = { awaiting_buyer_approval: "Order created: approve it on PayPal", rejected: "Rejected by you", failed: "PayPal call failed" };
const money = (v, c) => `${Number(v).toFixed(2)} ${c || ""}`;

async function loadHeader() {
  const h = await api("/api/health");
  $("#modes").innerHTML = `<span class="chip">LLM <b>${esc(h.llm)}${h.llm_model ? " · " + esc(h.llm_model) : ""}</b></span>
    <span class="chip">PayPal <b>${esc(h.paypal)}</b></span>`;
}
async function loadSamples() {
  const s = await api("/api/samples");
  $("#samples").innerHTML = s.map((x, i) => `<button data-i="${i}">${esc(x.label)}</button>`).join("");
  $("#samples").querySelectorAll("button").forEach((b) => (b.onclick = () => ($("#raw").value = s[b.dataset.i].text)));
}
async function loadMandate() {
  const m = await api("/api/mandate");
  $("#policy").value = m.policy_text;
  const r = m.rules;
  const chips = [
    `auto-pay ≤ ${r.auto_approve_max}`, `hard block > ${r.hard_block_above}`,
    `currencies: ${r.allowed_currencies.join(", ")}`,
    r.new_vendor_requires_approval ? "new vendor → countersign" : "new vendors allowed",
    r.payee_change_requires_approval ? "payee change → countersign" : null,
    ...Object.entries(r.monthly_budgets).map(([k, v]) => `${k === "*" ? "overall" : k} ≤ ${v}/mo`),
    ...r.blocked_categories.map((c) => `never: ${c}`),
  ].filter(Boolean);
  $("#rules").innerHTML = chips.map((c) => `<span class="rule">${esc(c)}</span>`).join("") +
    `<div class="muted" style="width:100%;font-size:11px;margin-top:4px">compiled by ${esc(m.compiled_by)}</div>`;
}
async function loadLedger() {
  const l = await api("/api/ledger");
  $("#budgets").innerHTML = l.budgets.map((b) => {
    const pct = Math.min(100, (100 * b.spent) / b.cap);
    return `<div style="font-size:12.5px;display:flex;justify-content:space-between"><span>${b.category === "*" ? "Overall" : esc(b.category)}</span><span>${b.spent.toFixed(2)} / ${b.cap}</span></div>
      <div class="meter"><i class="${pct > 85 ? "hot" : ""}" style="width:${pct}%"></i></div>`;
  }).join("") || '<p class="muted">No budgets in mandate.</p>';
  $("#events").innerHTML = l.events.map((e) => `<li><span class="who">${esc(e.actor)}</span> · ${esc(e.kind)}${e.case_id ? " #" + e.case_id : ""}<br><span class="muted">${esc(e.detail).slice(0, 160)}</span></li>`).join("");
}
async function loadCases() {
  const cs = await api("/api/cases");
  $("#cases").innerHTML = cs.map((c) => `<li data-id="${c.id}" class="${c.id === selected ? "sel" : ""}">
      <span>#${c.id} ${esc(c.invoice?.vendor_name || "…")}<br><span class="muted">${c.invoice ? money(c.invoice.amount, c.invoice.currency) : ""}</span></span>
      <span class="pill s-${c.status}">${LABEL[c.status] || c.status}</span></li>`).join("");
  $("#cases").querySelectorAll("li").forEach((li) => (li.onclick = () => showCase(+li.dataset.id)));
}
function checkIcon(c) { return c.passed ? '<span class="ic ok">✓</span>' : c.effect === "block" ? '<span class="ic block">✕</span>' : '<span class="ic countersign">!</span>'; }
async function showCase(id) {
  selected = id;
  const c = await api(`/api/cases/${id}`);
  const inv = c.invoice || {}, d = c.decision || { outcome: "", checks: [] }, r = c.risk || { signals: [], score: 0, level: "" }, pp = c.paypal || {};
  let ppHtml = '<p class="muted">No PayPal call (not authorised by mandate or human).</p>';
  if (pp.rail === "payout") ppHtml = `<div class="kv"><span>API</span><span>Payouts v1</span><span>Batch</span><span>${esc(pp.payout_batch_id)}</span>
      <span>Status</span><span>${esc(pp.batch_status)}</span><span>Txn</span><span>${esc(pp.transaction_id || "-")}</span><span>Idempotency</span><span style="font-size:11px">${esc(pp.idempotency_key)}</span></div>`;
  if (pp.rail === "checkout") ppHtml = `<div class="kv"><span>API</span><span>Orders v2</span><span>Order</span><span>${esc(pp.order_id)}</span>
      <span>Status</span><span>${esc(pp.order_status)}</span><span>Capture</span><span>${esc(pp.capture_id || "-")}</span></div>
      ${c.status === "awaiting_buyer_approval" ? `<a class="btnlink" id="approve-link" href="${esc(pp.approve_url)}">Approve on PayPal →</a>` : ""}`;
  if (pp.error) ppHtml = `<p style="color:var(--bad)">${esc(pp.error)}</p>`;
  $("#case").className = "";
  $("#case").innerHTML = `
    <div class="banner b-${d.outcome}"><h2>#${c.id} · ${c.status === "paid" ? (d.outcome === "NEEDS_COUNTERSIGN" ? "Countersigned by you and paid" : "Paid within your mandate") : STATUS_HEAD[c.status] || HEAD[d.outcome] || c.status}</h2><p>${esc(c.brief)}</p>
      <p class="muted" style="margin-top:6px;font-size:12.5px">Status now: <b>${LABEL[c.status] || c.status}</b>${pp.capture_id ? " · PayPal capture " + esc(pp.capture_id) : ""}${pp.transaction_id ? " · PayPal txn " + esc(pp.transaction_id) : ""}</p>
      ${c.status === "awaiting_countersign" ? `<div class="actions"><button class="ok" id="approve">Countersign &amp; pay</button><button class="bad" id="reject">Reject</button></div>` : ""}</div>
    <div class="cards">
      <div class="card"><h3 style="margin-top:0">Extracted by AI</h3><div class="kv">
        <span>Vendor</span><span>${esc(inv.vendor_name)}</span><span>Payee</span><span>${esc(inv.payee_email || "-")}</span>
        <span>Amount</span><span><b>${money(inv.amount, inv.currency)}</b></span><span>Invoice</span><span>${esc(inv.invoice_number || "-")}</span>
        <span>Category</span><span>${esc(inv.category)}</span><span>Rail</span><span>${inv.rail === "checkout" ? "PayPal checkout (Orders)" : "PayPal Payouts"}</span></div></div>
      <div class="card"><h3 style="margin-top:0">Risk ${r.score}/100 · ${esc(r.level)}</h3>
        <div class="meter"><i class="${r.score >= 70 ? "hot" : ""}" style="width:${r.score}%"></i></div>
        ${r.signals.map((s) => `<div class="sig"><span class="sev">+${s.severity}</span>${esc(s.detail)}</div>`).join("") || '<p class="muted">No risk signals.</p>'}</div>
      <div class="card"><h3 style="margin-top:0">Mandate checks (deterministic)</h3><ul class="checks">
        ${d.checks.map((k) => `<li>${checkIcon(k)}<span>${esc(k.detail)}</span></li>`).join("")}</ul></div>
      <div class="card"><h3 style="margin-top:0">PayPal (${esc((await api("/api/health")).paypal)})</h3>${ppHtml}</div>
    </div>
    <h3>Agent trace</h3><ol class="trace">${(c.trace || []).map((t) => `<li><b>${esc(t.node)}</b>${esc(t.summary)}</li>`).join("")}</ol>`;
  if ($("#approve")) $("#approve").onclick = () => act(id, true);
  if ($("#reject")) $("#reject").onclick = () => act(id, false);
  await Promise.all([loadCases(), loadLedger()]);
}
async function act(id, approve) {
  await api(`/api/cases/${id}/countersign`, { method: "POST", body: JSON.stringify({ approve, approver: "owner" }) });
  await showCase(id);
}
$("#process").onclick = async () => {
  const text = $("#raw").value.trim();
  if (!text) return;
  $("#process").disabled = true; $("#process").textContent = "Agent working…";
  try { const c = await api("/api/cases", { method: "POST", body: JSON.stringify({ text }) }); await showCase(c.id); }
  catch (e) { alert(e.message); }
  finally { $("#process").disabled = false; $("#process").textContent = "Run agent on this document"; }
};
$("#compile").onclick = async () => {
  $("#compile").textContent = "Compiling…";
  try { await api("/api/mandate", { method: "POST", body: JSON.stringify({ policy_text: $("#policy").value }) }); await loadMandate(); await loadLedger(); }
  finally { $("#compile").textContent = "Compile with AI"; }
};
// ---------------------------------------------------------------- AG Grid panels
let labGrid = null, ledgerGrid = null;
const OUT_STYLE = { AUTO_PAY: "s-paid", NEEDS_COUNTERSIGN: "s-awaiting_countersign", BLOCK: "s-blocked" };
const pillCell = (p) => p.value ? `<span class="pill ${OUT_STYLE[p.value] || "s-" + p.value}">${esc(p.value)}</span>` : "";
function openPanel(id) { ["lab", "ledger"].forEach((x) => $("#" + x).classList.toggle("hidden", x !== id)); $("#" + id).scrollIntoView({ behavior: "smooth" }); }
document.querySelectorAll("[data-close]").forEach((b) => (b.onclick = () => $("#" + b.dataset.close).classList.add("hidden")));
$("#open-lab").onclick = () => openPanel("lab");
$("#open-ledger").onclick = async () => { openPanel("ledger"); await loadLedgerGrid(); };
$("#run-lab").onclick = async () => {
  $("#run-lab").disabled = true; $("#run-lab").textContent = "Attacking…";
  try {
    const r = await api("/api/lab/run", { method: "POST" });
    const s = r.summary;
    $("#lab-summary").innerHTML = `
      <div class="m ${s.attacks_paid ? "bad" : "good"}"><b>${s.attacks_stopped}/${s.attacks}</b><span>attacks stopped before any money moved</span></div>
      <div class="m"><b>${s.attacks_blocked} · ${s.attacks_held_for_countersign}</b><span>blocked · held for your countersignature</span></div>
      <div class="m ${s.money_moving_calls_on_attacks ? "bad" : "good"}"><b>${s.money_moving_calls_on_attacks}</b><span>PayPal payout/order calls made for attacks</span></div>
      <div class="m ${s.controls_auto_paid === s.controls ? "good" : "bad"}"><b>${s.controls_auto_paid}/${s.controls}</b><span>legitimate controls still auto-paid</span></div>`;
    const rows = r.results.map((x) => ({ ...x, kind: x.control ? "control" : "attack", signals: x.signals.join(", "), failed_checks: x.failed_checks.join(", ") }));
    const cols = [
      { field: "kind", width: 95, filter: true },
      { field: "technique", flex: 2, minWidth: 260, filter: true, tooltipField: "technique" },
      { field: "outcome", width: 175, cellRenderer: pillCell, filter: true },
      { field: "verdict", width: 125, cellRenderer: (p) => `<span class="v-${p.value}">${esc(p.value)}</span>`, filter: true },
      { field: "risk_score", headerName: "Risk", width: 85, sort: "desc" },
      { field: "signals", headerName: "Signals fired", flex: 1.5, minWidth: 220, tooltipField: "signals" },
      { field: "money_moved", headerName: "Money moved", width: 125 },
    ];
    if (labGrid) labGrid.setGridOption("rowData", rows);
    else labGrid = agGrid.createGrid($("#lab-grid"), { rowData: rows, columnDefs: cols, defaultColDef: { sortable: true, resizable: true }, animateRows: true, tooltipShowDelay: 300 });
    await loadLedger();
  } catch (e) { alert(e.message); }
  finally { $("#run-lab").disabled = false; $("#run-lab").textContent = "Run the attack corpus"; }
};
async function loadLedgerGrid() {
  const cs = await api("/api/cases");
  const rows = cs.map((c) => ({
    id: c.id, created: (c.created_at || "").replace("T", " ").slice(0, 16), vendor: c.invoice?.vendor_name,
    amount: c.invoice?.amount, currency: c.invoice?.currency, category: c.invoice?.category,
    rail: c.invoice?.rail === "checkout" ? "Orders v2" : "Payouts", decision: c.decision?.outcome, status: LABEL[c.status] || c.status,
    risk: c.risk?.score, paypal_ref: c.paypal?.payout_batch_id || c.paypal?.order_id || "", txn: c.paypal?.transaction_id || c.paypal?.capture_id || "",
  }));
  const cols = [
    { field: "id", width: 70 }, { field: "created", width: 140 }, { field: "vendor", flex: 1, minWidth: 160, filter: true },
    { field: "amount", width: 105, type: "numericColumn", valueFormatter: (p) => p.value == null ? "" : Number(p.value).toFixed(2) },
    { field: "currency", width: 95 }, { field: "category", width: 115, filter: true }, { field: "rail", width: 105 },
    { field: "decision", width: 175, cellRenderer: pillCell, filter: true }, { field: "status", width: 150, filter: true },
    { field: "risk", width: 80 }, { field: "paypal_ref", headerName: "PayPal batch / order", flex: 1, minWidth: 180 }, { field: "txn", headerName: "Txn / capture", width: 160 },
  ];
  if (ledgerGrid) ledgerGrid.setGridOption("rowData", rows);
  else ledgerGrid = agGrid.createGrid($("#ledger-grid"), { rowData: rows, columnDefs: cols, defaultColDef: { sortable: true, resizable: true }, pagination: true, paginationPageSize: 20 });
}
$("#export-ledger").onclick = () => ledgerGrid && ledgerGrid.exportDataAsCsv({ fileName: "countersign-ledger.csv" });

(async () => {
  await Promise.all([loadHeader(), loadSamples(), loadMandate(), loadLedger(), loadCases()]);
  const q = new URLSearchParams(location.search).get("case");
  if (q) showCase(+q);
})();
