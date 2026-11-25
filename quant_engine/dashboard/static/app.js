"use strict";
let accessToken = "",
  data = null,
  page = "overview",
  history = [],
  accounts = { execution_active: false, accounts: [] };
const sections = {
  overview: "Overview",
  day: "Day Trading",
  swing: "Swing Trading",
  long_term: "Long-Term Investing",
  lab: "Strategy Lab",
  control: "Control Center",
  tax: "Tax Center",
};
const regimes = [
  "strong_bull",
  "bull",
  "chop",
  "bear",
  "strong_bear",
  "high_volatility",
];
const stages = [
  "idea",
  "specified",
  "backtested",
  "validated",
  "paper",
  "archived",
];
const $ = (id) => document.getElementById(id);
const escapeHTML = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const pretty = (value) => String(value).replaceAll("_", " ");
const money = (value) =>
  Number(value).toLocaleString("en-US", { style: "currency", currency: "USD" });
const safeMoney = (value) =>
  value == null || value === "" || Number.isNaN(Number(value))
    ? "—"
    : money(value);
const options = (values, selected) =>
  values
    .map(
      (v) =>
        `<option value="${escapeHTML(v)}" ${v === selected ? "selected" : ""}>${escapeHTML(pretty(v))}</option>`,
    )
    .join("");
const label = (title, input) => `<label>${title}${input}</label>`;
const reason = () =>
  label(
    "Reason for this change",
    '<input name="reason" required minlength="3" maxlength="500" placeholder="Describe why you are changing this">',
  );
async function api(path, body) {
  const response = await fetch(path, {
    method: body ? "POST" : "GET",
    headers: {
      Authorization: "Bearer " + accessToken,
      ...(body ? { "Content-Type": "application/json" } : {}),
    },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path != "/api/login") signOut();
    throw new Error(
      typeof result.detail === "string"
        ? result.detail
        : "Check the supplied values and try again.",
    );
  }
  return result;
}
function notify(message, error = false) {
  $("message").textContent = message;
  $("message").className = error ? "error" : "";
}
async function refresh() {
  const next = await api("/api/state");
  const log = await api("/api/history");
  data = next;
  history = log;
  try {
    accounts = await api("/api/accounts");
  } catch {
    accounts = {
      execution_active: false,
      accounts: [
        {
          id: "alpaca_paper",
          name: "Alpaca Paper",
          connected: false,
          status: "not connected",
          message: "not connected",
          balances: null,
          positions: [],
          orders: [],
        },
        {
          id: "public",
          name: "Public.com",
          connected: false,
          status: "not connected",
          message: "not connected",
          balances: null,
          positions: [],
          orders: [],
        },
      ],
    };
  }
  render();
}
async function change(action, target, values, why) {
  data = await api("/api/commands", {
    version: data.version,
    action,
    target,
    values,
    reason: why,
  });
  render();
  notify("Saved version " + data.version + ". Execution remains disconnected.");
  try {
    history = await api("/api/history");
    if (page === "control") render();
  } catch {
    notify(
      "Settings saved, but audit history could not refresh. Use Refresh.",
      true,
    );
  }
}
function signOut() {
  accessToken = "";
  data = null;
  history = [];
  accounts = { execution_active: false, accounts: [] };
  $("content").replaceChildren();
  $("workspace").hidden = true;
  $("login").hidden = false;
}
function metric(title, value, caption) {
  return `<div class="card"><span class="eyebrow">${title}</span><div class="metric">${value}</div><small>${caption}</small></div>`;
}
function registry(filter) {
  const rows = data.state.strategies.filter(
    (s) => !filter || s.portfolio === filter,
  );
  if (!rows.length)
    return '<div class="empty"><h3>Your registry starts here</h3><p>Add a research idea in Strategy Lab. Existing relay strategies have not been imported or approved automatically.</p></div>';
  return `<div class="table-wrap"><table><thead><tr><th>Strategy</th><th>Portfolio</th><th>Stage</th><th>Entry review</th></tr></thead><tbody>${rows.map((s) => `<tr><td>${escapeHTML(s.name)}<br><small>Switch ${s.enabled ? "on" : "off"}</small></td><td>${sections[s.portfolio]}</td><td>${s.stage}</td><td>${data.eligibility[s.id].eligible_for_risk_review ? "Eligible for independent risk review" : escapeHTML(data.eligibility[s.id].reasons.join(" · "))}<br><small>No execution worker</small></td></tr>`).join("")}</tbody></table></div>`;
}
function accountCard(account) {
  const connected = account && account.connected;
  const balances = (account && account.balances) || {};
  const positions = (account && account.positions) || [];
  const orders = (account && account.orders) || [];
  const status = escapeHTML((account && (account.message || account.status)) || "not connected");
  const rows = positions.length
    ? positions
        .map(
          (row) =>
            `<tr><td>${escapeHTML(row.symbol || "—")}</td><td>${escapeHTML(row.qty || "—")}</td><td>${safeMoney(row.market_value)}</td><td>${row.unrealized_pl != null && row.unrealized_pl !== "" ? safeMoney(row.unrealized_pl) : "—"}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="4">No positions</td></tr>';
  const orderRows = orders.length
    ? orders
        .slice(0, 20)
        .map(
          (row) =>
            `<tr><td>${escapeHTML(row.submitted_at ? new Date(row.submitted_at).toLocaleString() : "—")}</td><td>${escapeHTML(row.symbol || "—")}</td><td>${escapeHTML([row.side, row.qty, row.type].filter(Boolean).join(" "))}</td><td>${escapeHTML(row.status || "—")}</td></tr>`,
        )
        .join("")
    : "";
  const orderTable =
    account && account.id === "alpaca_paper"
      ? `<div class="table-wrap"><table><thead><tr><th>When</th><th>Symbol</th><th>Order</th><th>Status</th></tr></thead><tbody>${orderRows || '<tr><td colspan="4">No recent orders</td></tr>'}</tbody></table></div>`
      : "";
  return `<div class="card"><div class="row"><h2>${escapeHTML((account && account.name) || "Account")}</h2><span class="badge">${connected ? "Connected · read only" : "Not connected"}</span></div><p><small>${status}. This dashboard cannot place or cancel orders.</small></p>${
    connected
      ? `<div class="row"><span>Equity</span><strong>${safeMoney(balances.equity)}</strong></div><div class="row"><span>Cash</span><strong>${safeMoney(balances.cash)}</strong></div><div class="row"><span>Buying power</span><strong>${safeMoney(balances.buying_power)}</strong></div><div class="table-wrap"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Market value</th><th>Unrealized P/L</th></tr></thead><tbody>${rows}</tbody></table></div>${orderTable}`
      : "<p>Connect this account by adding its read-only credentials to the dashboard environment.</p>"
  }</div>`;
}
function accountsPanel() {
  const list = (accounts && accounts.accounts) || [];
  const cards = list.length
    ? list.map(accountCard).join("")
    : accountCard({
        name: "Accounts",
        connected: false,
        status: "not connected",
        message: "not connected",
      });
  return `<div class="card"><div class="row"><h2>Accounts</h2><span class="badge">READ ONLY</span></div><p>Alpaca paper and Public.com balances and positions. Execution stays disconnected; these views never submit or cancel orders.</p><div class="grid two">${cards}</div></div>`;
}
function overview() {
  return `<div class="grid">${metric("Execution", "Disconnected", "Paper-only · no orders can be sent")}${metric("Research registry", data.state.strategies.length, "User-entered research, not trading results")}${metric("New entries", data.state.paused ? "Paused" : "Requested on", "Saved preference · not applied to relay")}</div>${accountsPanel()}<div class="grid">${data.state.portfolios.map((p) => `<div class="card"><span class="eyebrow">PORTFOLIO</span><h2>${p.name}</h2><p>${pretty(p.mode)} · ${pretty(p.regime)}</p><div class="rule"></div><div class="row"><span>Account equity</span><strong>Not connected</strong></div><p><small>Portfolio controls are not linked to a broker.</small></p><button class="secondary" data-page="${p.id}">Open portfolio →</button></div>`).join("")}</div><div class="card"><div class="row"><h2>Strategy registry</h2><span class="badge">Shared workspace</span></div>${registry()}</div>`;
}
function portfolio(id) {
  const p = data.state.portfolios.find((x) => x.id === id);
  return `<div class="grid">${metric("Account equity", "—", "No broker connected")}${metric("Returns", "—", "No real or simulated performance loaded")}${metric("Configuration", p.paused ? "Paused" : "Requested on", "Risk limits remain independent")}</div><div class="card"><h2>Portfolio posture</h2><p>Separate regime and entry preferences for this portfolio. Automatic mode fails closed until a classifier is connected. Hybrid uses your selected regime.</p><form data-action="portfolio" data-target="${p.id}"><div class="grid two">${label("Regime mode", `<select name="mode">${options(["manual", "hybrid", "automatic"], p.mode)}</select>`)}${label("Selected regime", `<select name="regime">${options(regimes, p.regime)}</select>`)}${label("Temporary override", `<select name="override"><option value="">No override</option>${options(regimes, p.override)}</select>`)}${label("Override expires (your local time)", `<input name="override_until" type="datetime-local" value="${p.override_until ? localDate(p.override_until) : ""}">`)}</div><label><input name="paused" type="checkbox" ${p.paused ? "checked" : ""}>Pause new entries for this portfolio</label><p><small>Expired overrides fall back to the selected regime, or block automatic entries. Existing positions are not closed by these controls.</small></p>${reason()}<button>Save portfolio settings</button></form></div><div class="card"><h2>Portfolio strategies</h2>${registry(id)}</div>`;
}
function localDate(iso) {
  const d = new Date(iso);
  return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
    .toISOString()
    .slice(0, 16);
}
function lab() {
  return `<div class="card"><h2>From hypothesis to paper approval</h2><p>Record your strategy, reproducible evidence and held-out results. Each stage requires an explicit human action. Backtests and research agents do not run from this dashboard.</p><div class="steps">${stages.map((s) => `<span class="step">${s}</span>`).join("")}</div></div><div class="card"><h2>Register a strategy idea</h2><form data-action="create_strategy">${label("Strategy name", '<input name="name" required maxlength="100">')}${label("Portfolio", `<select name="portfolio">${["day", "swing", "long_term"].map((p) => `<option value="${p}">${sections[p]}</option>`).join("")}</select>`)}${label("Hypothesis and entry / exit rules", '<textarea name="thesis" required maxlength="4000"></textarea>')}${label("Initial permitted regime", `<select name="regime">${options(regimes, "chop")}</select>`)}${reason()}<button>Add research idea</button></form></div>${data.state.strategies.map((s) => `<div class="card"><div class="row"><h2>${escapeHTML(s.name)}</h2><span class="badge">${sections[s.portfolio]}</span></div><p>${escapeHTML(s.thesis)}</p><small>Permitted regimes: ${escapeHTML(s.regimes.map(pretty).join(", "))}. Definitions are fixed; create a new idea to revise the hypothesis.</small><div class="steps">${stages.map((stage) => `<span class="step ${stage === s.stage ? "current" : ""}">${stage}</span>`).join("")}</div>${s.stage === "archived" ? "<p>Archived. Register a new idea to resume research.</p>" : `<form data-action="strategy" data-target="${s.id}">${label("Stage", `<select name="stage">${options([s.stage, ...(s.stage === "paper" ? [] : [stages[stages.indexOf(s.stage) + 1]]), "archived"], s.stage)}</select>`)}${label("Evidence: dataset, code revision, split dates, costs, result location", '<textarea name="evidence" maxlength="4000">' + escapeHTML(s.evidence) + "</textarea>")}<div class="grid two">${label("Held-out trades", `<input name="held_out_trades" type="number" min="0" step="1" required value="${s.held_out_trades}">`)}${label("Total trials tested", `<input name="trials" type="number" min="0" step="1" required value="${s.trials}">`)}</div><label><input type="checkbox" name="costs_modeled" ${s.costs_modeled ? "checked" : ""}>Costs modeled in recorded results</label><label><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}>Enable paper strategy preference (requires paper stage)</label><p><small>Validation needs at least 30 held-out trades. Saving the paper stage records your account as approver. Evidence is user-attested; no results are verified automatically.</small></p>${reason()}<button>Save research review</button></form>`}</div>`).join("")}`;
}
function control() {
  return `<div class="grid two"><div class="card"><h2>Pause new entries</h2><p>Saved controls are not yet acknowledged by a worker. Pausing entries never requests a position closure or disables protective exits.</p><form data-action="global_pause"><label><input type="checkbox" name="paused" ${data.state.paused ? "checked" : ""}>Pause new entries across all portfolios</label>${reason()}<button>Save entry control</button></form></div><div class="card"><h2>Independent risk defaults</h2><p>Read-only policy from Cursor’s existing risk engine. These limits cannot be raised here.</p><div class="table-wrap"><table>${Object.entries(
    data.risk_limits,
  )
    .filter(([k]) => k.endsWith("_pct"))
    .map(
      ([k, v]) =>
        `<tr><td>${pretty(k.replace("_pct", ""))}</td><td>${v}%</td></tr>`,
    )
    .join(
      "",
    )}</table></div><p><small>Live execution is locked. Broker execution and worker acknowledgement are not implemented.</small></p></div></div><div class="card"><h2>Configuration audit</h2><p>Latest 50 versions. Each saved snapshot includes the authenticated actor, timestamp and reason. Earlier versions remain in storage.</p>${history.length ? history.map((h) => `<details><summary>v${h.version} · ${escapeHTML(new Date(h.created_at).toLocaleString())} · ${escapeHTML(h.reason)}</summary><p>Actor: ${escapeHTML(h.owner)}</p><pre>${escapeHTML(JSON.stringify(h.state, null, 2))}</pre></details>`).join("") : "<p>No saved changes yet.</p>"}</div>`;
}
function tax() {
  const t = data.state.tax,
    p = data.tax_plan;
  return `<div class="notice">Federal planning illustration · User-stated carryforward is unverified · No tax lots, sale recommendations, state taxes or wash-sale analysis</div><div class="grid">${metric("Starting carryforward", money(t.carryforward), "User stated · unverified; character/year unknown")}${metric("Scenario gains offset", money(p.gains_offset), "Hypothetical net capital gains")}${metric("Next carryforward", money(p.illustrative_next_carryforward), "Illustration only, not a tax return figure")}</div><div class="grid two"><div class="card"><h2>Explore a scenario</h2><form data-action="tax">${label("Unverified federal carryforward ($)", `<input name="carryforward" type="number" min="0" max="1000000000" step=".01" required value="${t.carryforward}">`)}${label("Hypothetical net short-term gains / losses ($)", `<input name="short_term_net" type="number" step=".01" required value="${t.short_term_net}">`)}${label("Hypothetical net long-term gains / losses ($)", `<input name="long_term_net" type="number" step=".01" required value="${t.long_term_net}">`)}${label("Filing status assumption", `<select name="filing">${options(["other", "married_filing_separately"], t.filing)}</select>`)}<label><input type="checkbox" name="sufficient_income" ${t.sufficient_income ? "checked" : ""}>Assume sufficient taxable income for the ordinary-income deduction</label>${reason()}<button>Save and calculate scenario</button></form></div><div class="card"><h2>Illustrative federal netting</h2><table><tr><td>Net gains after carryforward</td><td>${money(p.remaining_net_gains)}</td></tr><tr><td>Losses before deduction</td><td>${money(p.losses_before_deduction)}</td></tr><tr><td>Assumed ordinary-income deduction</td><td>${money(p.illustrative_deduction)}</td></tr><tr><td>Illustrative next carryforward</td><td>${money(p.illustrative_next_carryforward)}</td></tr></table><p>Assumes all scenario losses are deductible. The $35,000 has not been verified against a return or split into short- and long-term carryforwards. This simplified total does not determine tax liability.</p><p>Actual carryforward calculations depend on the IRS worksheet, income and loss character. State rules, wash sales, special-rate gains, options adjustments and transactions in other accounts are excluded.</p><a href="https://www.irs.gov/taxtopics/tc409" target="_blank" rel="noopener noreferrer">IRS capital gains and losses →</a></div></div>`;
}
function render() {
  $("page-title").textContent = sections[page];
  $("version").textContent = "Saved v" + data.version;
  $("nav").innerHTML = Object.entries(sections)
    .map(
      ([id, title]) =>
        `<button data-page="${id}" class="${id === page ? "active" : ""}" ${id === page ? 'aria-current="page"' : ""}>${title}</button>`,
    )
    .join("");
  $("content").innerHTML =
    page === "overview"
      ? overview()
      : page === "lab"
        ? lab()
        : page === "control"
          ? control()
          : page === "tax"
            ? tax()
            : portfolio(page);
}
document.addEventListener("click", (event) => {
  const target = event.target.closest("[data-page]");
  if (target) {
    page = target.dataset.page;
    notify("");
    render();
  }
});
$("content").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    fields = Object.fromEntries(new FormData(form)),
    action = form.dataset.action;
  const why = fields.reason;
  delete fields.reason;
  let values = fields;
  if (action === "global_pause")
    values = { paused: form.elements.paused.checked };
  if (action === "portfolio")
    values = {
      ...fields,
      paused: form.elements.paused.checked,
      override: fields.override || null,
      override_until: fields.override_until
        ? new Date(fields.override_until).toISOString()
        : null,
    };
  if (action === "create_strategy") {
    values = { ...fields, regimes: [fields.regime] };
    delete values.regime;
  }
  if (action === "strategy")
    values = {
      ...fields,
      held_out_trades: Number(fields.held_out_trades),
      trials: Number(fields.trials),
      enabled: form.elements.enabled.checked,
      costs_modeled: form.elements.costs_modeled.checked,
    };
  if (action === "tax")
    values = {
      ...fields,
      sufficient_income: form.elements.sufficient_income.checked,
    };
  const button = form.querySelector("button");
  button.disabled = true;
  try {
    await change(action, form.dataset.target || "", values, why);
  } catch (error) {
    notify(error.message, true);
    button.disabled = false;
  }
});
$("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = event.target,
    button = form.querySelector("button");
  button.disabled = true;
  $("login-error").textContent = "";
  try {
    const result = await api(
      "/api/login",
      Object.fromEntries(new FormData(form)),
    );
    accessToken = result.access_token;
    form.reset();
    await refresh();
    $("login").hidden = true;
    $("workspace").hidden = false;
  } catch (error) {
    accessToken = "";
    $("login-error").textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
$("logout").addEventListener("click", signOut);
$("refresh").addEventListener("click", async () => {
  try {
    await refresh();
    notify("Loaded the latest saved settings.");
  } catch (error) {
    notify(error.message, true);
  }
});
fetch("/api/session")
  .then((r) => r.json())
  .then((s) => {
    if (s.auth_mode === "local") $("email-label").hidden = true;
    else $("password-label").firstChild.textContent = "Password";
  })
  .catch(() => {
    $("login-error").textContent = "Service unavailable. Try reloading.";
  });
