"use strict";
let research = {
  loaded: false,
  datasets: [],
  runs: [],
  catalog: null,
  report: null,
  error: "",
};
let researchBusy = false;
function clearResearch() {
  research = {
    loaded: false,
    datasets: [],
    runs: [],
    catalog: null,
    report: null,
    error: "",
  };
  researchBusy = false;
}
async function loadResearch() {
  const tokenAtStart = accessToken;
  const [catalog, datasets, runs] = await Promise.all([
    api("/api/research/catalog"),
    api("/api/research/datasets"),
    api("/api/research/runs"),
  ]);
  if (!accessToken || tokenAtStart !== accessToken) return;
  research = { ...research, catalog, datasets, runs, loaded: true, error: "" };
  if (page === "lab") render();
}
const numberInput = (name, title, value, min, max, step) =>
  label(
    title,
    `<input name="${name}" type="number" required value="${value}" min="${min}" max="${max}" step="${step}">`,
  );
function researchPanel() {
  if (!research.loaded)
    return `<div class="card"><h2>Backtest workbench</h2><p>Test the four ORB/VWAP strategies on five-minute SPY or QQQ bars. Simulated underlying-share returns only; no orders and no automatic promotion.</p><button type="button" data-research-action="load">Open backtest workbench</button><p role="alert">${escapeHTML(research.error)}</p></div>`;
  const candidates = data.state.strategies.filter(
    (s) =>
      s.portfolio === "day" &&
      s.stage !== "archived" &&
      Object.values(research.catalog.templates).includes(s.name),
  );
  const eligible = candidates.length && research.datasets.length;
  return `<div class="card"><div class="row"><h2>Backtest workbench</h2><button class="secondary" type="button" data-research-action="load">Refresh research</button></div><p>Underlying shares • Five-minute bars • Independent development / held-out accounts • No orders or automatic approval</p><details><summary>Exact rules and execution assumptions</summary>${Object.entries(
    research.catalog.rules,
  )
    .map(
      ([k, v]) => `<p><strong>${escapeHTML(k)}:</strong> ${escapeHTML(v)}</p>`,
    )
    .join(
      "",
    )}<p>Risk defaults: 0.25% per trade, 0.75% daily loss, 2% weekly loss, 0.5% aggregate planned risk. A weekly kill switch remains latched for the remainder of each simulated split.</p></details><div class="grid two"><div><h3>1. Import historical data</h3><form data-research="alpaca">${label("Symbol", `<select name="symbol">${options(["SPY", "QQQ"], "SPY")}</select>`)}${label("Start date", '<input name="start" type="date" required>')}${label("End date (before today)", '<input name="end" type="date" required>')}<button>Import existing Alpaca IEX history</button><p><small>Uses configured paper-account data access. Maximum 180 calendar days. No subscription purchase or paid-feed fallback. IEX volume is not consolidated market volume; missing bars fail validation.</small></p></form><details><summary>Import CSV instead</summary><p>Columns in order: timestamp,open,high,low,close,volume,session_close. Timestamp is the bar start with timezone. Include every five-minute regular-session bar and the session close timestamp (16:00 or 13:00 New York).</p><form data-research="csv">${label("Symbol", `<select name="symbol">${options(["SPY", "QQQ"], "SPY")}</select>`)}${label("Data source / export details", '<input name="source" required minlength="3" maxlength="300">')}${label("Data kind", '<select name="kind"><option value="historical">User-supplied historical data</option><option value="synthetic">Synthetic / test data</option></select>')}${label("CSV file (3 MB maximum)", '<input name="file" type="file" accept=".csv,text/csv" required>')}<button>Validate and save CSV</button></form></details><button class="secondary" type="button" data-research-action="demo">Load synthetic demo dataset</button><p><small>Demo prices are fabricated to check functionality, never investment evidence.</small></p></div><div><h3>2. Run a versioned test</h3>${!candidates.length ? "<p>Register one of the four named ORB/VWAP ideas below first. These templates do not run the other arsenal ideas.</p>" : ""}<form data-research="run">${label("Registered strategy", `<select name="strategy_id" required>${candidates.map((s) => `<option value="${escapeHTML(s.id)}">${escapeHTML(s.name)}</option>`).join("")}</select>`)}${label("Dataset", `<select name="dataset_id" required>${research.datasets.map((d) => `<option value="${escapeHTML(d.id)}">${escapeHTML(d.symbol + " · " + d.kind + " · " + d.start + " to " + d.end + " · " + d.id.slice(0, 8))}</option>`).join("")}</select>`)}${label("Last development date (later sessions are held out)", '<input name="split_date" type="date" required>')}<p><small>At least two sessions per split; use much longer periods for meaningful research. Inspecting held-out results makes them part of your research history.</small></p><details><summary>Rule parameters and cost assumptions</summary>${label("Opening range minutes (ORB only)", `<select name="opening_minutes">${options(["5", "15", "30"], "15")}</select>`)}${numberInput("ema_period", "Session EMA period (VWAP only)", 20, 5, 50, 1)}${numberInput("stop_pct", "Stop distance (%)", 0.3, 0.05, 2, 0.01)}${numberInput("target_r", "Target / stop distance", 2, 0.5, 5, 0.1)}${numberInput("max_entries", "Maximum entries per session", 3, 1, 5, 1)}${numberInput("initial_equity", "Starting simulated equity ($)", 100000, 1000, 10000000, 0.01)}${numberInput("spread_bps", "Full spread (basis points)", 1, 0, 100, 0.01)}${numberInput("slippage_bps", "Slippage per side (basis points)", 1, 0, 100, 0.01)}${numberInput("commission_per_share", "Commission per share per side ($)", 0.005, 0, 1, 0.0001)}${numberInput("short_borrow_bps_annual", "Assumed annual short borrow (basis points)", 100, 0, 10000, 0.01)}</details><label><input type="checkbox" required>I reviewed this template and split. Each new run counts as a trial; results do not enable trading.</label><button ${eligible && !researchBusy ? "" : "disabled"}>Run backtest</button></form></div></div></div><div class="card"><h2>Saved research runs</h2><p>Showing the latest ${research.runs.length} recorded trials (up to 100; older records remain stored). Each tests one symbol × one strategy × one parameter set. Failed/incomplete attempts remain visible. Compare identical datasets, splits and costs; returns alone are not a ranking.</p><div class="table-wrap"><table><thead><tr><th>Strategy / data</th><th>Split / status</th><th>Development return</th><th>Held-out return</th><th>Held-out trades</th><th>Report</th></tr></thead><tbody>${research.runs.map((r) => `<tr><td>${escapeHTML(r.strategy_snapshot.name)}<br><small>${escapeHTML(r.dataset.symbol + " · " + r.dataset.kind + " · " + r.dataset.sha256.slice(0, 8))}</small></td><td>${escapeHTML(r.request.split_date)}<br>${escapeHTML(r.status)}</td><td>${pct(r.metrics?.development?.net_return_pct)}</td><td>${pct(r.metrics?.holdout?.net_return_pct)}</td><td>${r.metrics?.holdout?.trades ?? "—"}</td><td>${r.status === "incomplete" ? "No result saved" : `<button type="button" class="secondary" data-research-action="report" data-id="${escapeHTML(r.id)}">View</button>`}</td></tr>`).join("")}</tbody></table></div><p><small>Incomplete means the server did not save a terminal result. Retrying the same request ID does not start another run. A new attempt remains a separate trial.</small></p></div>${reportPanel()}`;
}
function pct(value) {
  return value == null ? "—" : escapeHTML(Number(value).toFixed(2)) + "%";
}
function reportPanel() {
  const saved = research.report;
  if (!saved) return "";
  if (saved.payload.status !== "completed")
    return `<div class="card"><h2>Failed research run</h2><p>${escapeHTML(saved.payload.error)}</p></div>`;
  const report = saved.payload.report;
  const rows = [
    ["Return after costs", "net_return_pct", "pct"],
    ["Maximum bar-close drawdown", "max_bar_close_drawdown_pct", "pct"],
    ["Net profit / loss", "net_pnl", "money"],
    ["Average net profit / loss per trade", "expectancy", "money"],
    ["Profit factor", "profit_factor", "number"],
    ["Win rate", "win_rate_pct", "pct"],
    ["Trade count", "trades", "number"],
    ["Exposure (five-minute approximation)", "exposure_pct", "pct"],
    ["Buy-and-hold price benchmark", "benchmark_price_return_pct", "pct"],
  ];
  const fmt = (value, type) =>
    value == null
      ? "—"
      : type === "pct"
        ? pct(value)
        : type === "money"
          ? money(value)
          : escapeHTML(value);
  return `<div class="card"><div class="row"><h2>Backtest report · ${escapeHTML(report.template)}</h2><button type="button" data-research-action="download" data-id="${escapeHTML(saved.id)}">Download full report</button></div><span class="badge">${escapeHTML(report.dataset.kind)} · SIMULATED FILLS</span><p>${escapeHTML(report.dataset.symbol + " · " + report.dataset.start + " to " + report.dataset.end + " · " + report.dataset.source)}</p><div class="table-wrap"><table><thead><tr><th>Metric</th><th>Development</th><th>Held out</th></tr></thead><tbody>${rows.map(([name, key, type]) => `<tr><td>${name}</td><td>${fmt(report.development.metrics[key], type)}</td><td>${fmt(report.holdout.metrics[key], type)}</td></tr>`).join("")}</tbody></table></div><p>${report.minimum_trade_count_met ? "30-trade minimum reached; this is NOT approval or proof of an edge." : "Insufficient historical held-out evidence for the 30-trade minimum."} Strategy stage and switches are unchanged. Profit factor is undefined when there are no losing trades.</p>${report.warnings
    .concat(report.dataset.warnings)
    .map((w) => `<p><small>${escapeHTML(w)}</small></p>`)
    .join(
      "",
    )}<details><summary>Exact parameters, rules, rejected entries and risk status</summary><pre>${escapeHTML(JSON.stringify({ engine: report.engine_version, implementation_sha256: report.implementation_sha256, parameters: report.parameters, rules: report.rules, development_rejections: report.development.rejected_entries, holdout_rejections: report.holdout.rejected_entries, holdout_risk: report.holdout.risk_status }, null, 2))}</pre></details>${["development", "holdout"].map((part) => `<details><summary>${pretty(part)} trade log (${report[part].trades.length})</summary><div class="table-wrap"><table><thead><tr><th>Entry</th><th>Exit bar</th><th>Side / shares</th><th>Fill prices</th><th>Net P/L</th><th>Exit</th></tr></thead><tbody>${report[part].trades.map((t) => `<tr><td>${escapeHTML(t.entry_time)}</td><td>${escapeHTML(t.exit_bar_start)}</td><td>${escapeHTML(t.direction)} / ${t.qty}</td><td>${escapeHTML(t.entry)} → ${escapeHTML(t.exit_price)}</td><td>${money(t.net_pnl)}</td><td>${escapeHTML(t.reason)}</td></tr>`).join("")}</tbody></table></div></details>`).join("")}<button class="secondary" type="button" data-research-action="csv" data-id="${escapeHTML(research.runs.find((r) => r.id === saved.id)?.request.dataset_id || "")}">Download reproducible input CSV</button></div>`;
}
function downloadBlob(content, type, filename) {
  const url = URL.createObjectURL(new Blob([content], { type }));
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-research-action]");
  if (!button || researchBusy) return;
  button.disabled = true;
  const sessionToken = accessToken;
  try {
    const action = button.dataset.researchAction;
    if (action === "load") await loadResearch();
    if (action === "demo") {
      await api("/api/research/demo", {});
      await loadResearch();
      notify("Synthetic demo saved. It cannot establish strategy performance.");
    }
    if (action === "report") {
      const saved = await api(
        "/api/research/runs/" + encodeURIComponent(button.dataset.id),
      );
      if (!accessToken || sessionToken !== accessToken) return;
      research.report = saved;
      render();
    }
    if (action === "download") {
      const trial = research.runs.find((r) => r.id === button.dataset.id);
      downloadBlob(
        JSON.stringify({ trial, result: research.report }, null, 2),
        "application/json",
        "quant-backtest-" + button.dataset.id + ".json",
      );
    }
    if (action === "csv") {
      const response = await fetch(
        "/api/research/datasets/" +
          encodeURIComponent(button.dataset.id) +
          "/csv",
        { headers: { Authorization: "Bearer " + accessToken } },
      );
      if (!response.ok) throw new Error("Dataset export failed");
      downloadBlob(await response.text(), "text/csv", "backtest-input.csv");
    }
  } catch (error) {
    if (!accessToken || sessionToken !== accessToken) return;
    research.error = error.message;
    notify(error.message, true);
  } finally {
    button.disabled = false;
  }
});
$("content").addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.dataset.research) return;
  event.preventDefault();
  if (researchBusy) return;
  const button = form.querySelector("button"),
    values = Object.fromEntries(new FormData(form));
  button.disabled = true;
  researchBusy = true;
  const sessionToken = accessToken;
  try {
    if (form.dataset.research === "csv") {
      const file = form.elements.file.files[0];
      if (file.size > 3000000) throw new Error("CSV exceeds 3 MB");
      await api("/api/research/datasets", {
        symbol: values.symbol,
        source: values.source,
        kind: values.kind,
        csv: await file.text(),
      });
    } else if (form.dataset.research === "alpaca")
      await api("/api/research/alpaca", values);
    else {
      const selected = data.state.strategies.find(
        (s) => s.id === values.strategy_id,
      );
      const template = Object.entries(research.catalog.templates).find(
        ([, name]) => name === selected?.name,
      )?.[0];
      const parameters = {};
      for (const key of ["opening_minutes", "ema_period", "max_entries"])
        parameters[key] = Number(values[key]);
      for (const key of [
        "stop_pct",
        "target_r",
        "initial_equity",
        "spread_bps",
        "slippage_bps",
        "commission_per_share",
        "short_borrow_bps_annual",
      ])
        parameters[key] = values[key];
      const requestId = form.dataset.requestId || crypto.randomUUID();
      form.dataset.requestId = requestId;
      const saved = await api("/api/research/runs", {
        request_id: requestId,
        dataset_id: values.dataset_id,
        strategy_id: values.strategy_id,
        template,
        split_date: values.split_date,
        parameters,
      });
      if (!accessToken || sessionToken !== accessToken) return;
      research.report = saved;
    }
    if (!accessToken || sessionToken !== accessToken) return;
    researchBusy = false;
    await loadResearch();
    notify(
      "Research saved. Trading settings and strategy approvals are unchanged.",
    );
  } catch (error) {
    if (!accessToken || sessionToken !== accessToken) return;
    notify(
      error.message +
        " Refresh research to inspect any recorded trial before retrying.",
      true,
    );
  } finally {
    researchBusy = false;
    button.disabled = false;
  }
});
