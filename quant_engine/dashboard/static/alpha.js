"use strict";
let alphaState = {
  loaded: false,
  loading: false,
  overview: null,
  preview: null,
  error: "",
};
let alphaBusy = false;

function clearAlpha() {
  alphaState = {
    loaded: false,
    loading: false,
    overview: null,
    preview: null,
    error: "",
  };
  alphaBusy = false;
}

async function loadAlpha() {
  if (!accessToken) return;
  const tokenAtStart = accessToken;
  alphaState.loading = true;
  try {
    const overview = await api("/api/alpha/overview");
    if (!accessToken || tokenAtStart !== accessToken) return;
    alphaState = {
      ...alphaState,
      overview,
      loaded: true,
      loading: false,
      error: "",
    };
    if (page === "alpha") render();
  } catch (error) {
    if (!accessToken || tokenAtStart !== accessToken) return;
    alphaState.loading = false;
    alphaState.error = error.message;
    if (page === "alpha") render();
  }
}

function ensureAlphaLoaded() {
  if (!alphaState.loaded && !alphaState.loading && accessToken) {
    loadAlpha();
  }
}

function alphaPct(val) {
  if (val == null || val === "" || Number.isNaN(Number(val))) return "—";
  return escapeHTML(Number(val).toFixed(2)) + "%";
}

function alphaNum(val, digits = 2) {
  if (val == null || val === "" || Number.isNaN(Number(val))) return "—";
  return escapeHTML(Number(val).toFixed(digits));
}

function corrCellClass(val, isDiag) {
  if (isDiag) return "corr-cell corr-diag";
  if (val >= 0.6) return "corr-cell corr-pos-high";
  if (val >= 0.25) return "corr-cell corr-pos-mid";
  if (val <= -0.6) return "corr-cell corr-neg-high";
  if (val <= -0.25) return "corr-cell corr-neg-mid";
  return "corr-cell corr-neutral";
}

function modelLabel(id) {
  if (id === "v1") return "V1 · Equal Risk Allocation";
  if (id === "v2") return "V2 · Constrained Vol & Correlation Adjusted";
  if (id === "v3") return "V3 · Kakushadze–Yu Alpha Combo (Research Only)";
  return pretty(id);
}

function heatmapSection(corr) {
  if (!corr || !corr.alpha_ids || !corr.alpha_ids.length) {
    return '<div class="card"><h2>Strategy correlation heatmap</h2><p>Run a research cycle to generate daily alpha returns and inspect pairwise correlations.</p></div>';
  }
  const ids = corr.alpha_ids;
  const mat = corr.correlation_matrix || [];
  const head = `<tr><th>Alpha</th>${ids.map((id) => `<th>${escapeHTML(id)}</th>`).join("")}</tr>`;
  const body = ids
    .map((rowId, i) => {
      const cells = ids
        .map((_, j) => {
          const v = Number((mat[i] && mat[i][j]) ?? 0);
          return `<td class="${corrCellClass(v, i === j)}">${v.toFixed(2)}</td>`;
        })
        .join("");
      return `<tr><th>${escapeHTML(rowId)}</th>${cells}</tr>`;
    })
    .join("");
  return `<div class="card"><div class="row"><h2>Strategy correlation heatmap</h2><span class="badge">Shrinkage ${alphaNum(corr.shrinkage, 2)} · N_eff ${alphaNum(corr.effective_independent_alphas, 2)}</span></div><p>Pairwise correlation across aligned daily alpha returns. Avg |corr|: ${alphaNum(corr.avg_abs_pairwise_correlation, 2)}${corr.is_singular ? " · Raw covariance singular (regularized)" : ""}.</p><div class="table-wrap"><table class="heatmap"><thead>${head}</thead><tbody>${body}</tbody></table></div></div>`;
}

function ensembleSection(ov) {
  const comp = ov.ensemble_comparison || {};
  const keys = ["v1", "v2", "v3"].filter((k) => comp[k]);
  if (!keys.length) {
    return '<div class="card"><h2>Ensemble performance</h2><p>Run a research cycle to evaluate V1, V2, and V3 ensembles against equal-weight and market benchmarks.</p></div>';
  }
  const ref = comp[ov.selected_model] || comp[keys[0]];
  const rows = [];
  for (const k of keys) {
    const m = comp[k].ensemble_metrics || {};
    rows.push({
      name: modelLabel(k) + (k === ov.selected_model ? " (Selected)" : ""),
      ret: m.total_return_pct,
      sharpe: m.sharpe_ratio,
      dd: m.max_drawdown_pct,
      vol: m.annualized_volatility_pct,
      turn: m.avg_daily_turnover,
      tag: comp[k].research_only ? "Research only" : "Paper candidate",
    });
  }
  if (ref.equal_weight_metrics) {
    const eq = ref.equal_weight_metrics;
    rows.push({
      name: "Equal Weight Benchmark (1/N)",
      ret: eq.total_return_pct,
      sharpe: eq.sharpe_ratio,
      dd: eq.max_drawdown_pct,
      vol: eq.annualized_volatility_pct,
      turn: eq.avg_daily_turnover,
      tag: "Baseline",
    });
  }
  if (ref.benchmark_metrics) {
    const bm = ref.benchmark_metrics;
    rows.push({
      name: "Universe Buy-and-Hold Benchmark",
      ret: bm.total_return_pct,
      sharpe: bm.sharpe_ratio,
      dd: bm.max_drawdown_pct,
      vol: bm.annualized_volatility_pct,
      turn: "0",
      tag: "Benchmark",
    });
  }
  return `<div class="card"><div class="row"><h2>Ensemble performance</h2><span class="badge">Active: ${escapeHTML(ov.selected_model.toUpperCase())}</span></div><p>Causal walk-forward comparison across identical out-of-sample dates and modeled transaction costs.</p><div class="table-wrap"><table><thead><tr><th>Portfolio / Model</th><th>Total return</th><th>Sharpe</th><th>Max drawdown</th><th>Ann. vol</th><th>Avg turnover</th><th>Status</th></tr></thead><tbody>${rows
    .map(
      (r) =>
        `<tr><td>${escapeHTML(r.name)}</td><td>${alphaPct(r.ret)}</td><td>${alphaNum(r.sharpe, 2)}</td><td>${alphaPct(r.dd)}</td><td>${alphaPct(r.vol)}</td><td>${alphaNum(r.turn, 3)}</td><td><span class="badge">${escapeHTML(r.tag)}</span></td></tr>`,
    )
    .join("")}</tbody></table></div></div>`;
}

function allocationSection(ov) {
  const proposed = ov.proposed_allocations || {};
  const current = ov.current_allocation || null;
  const preview = alphaState.preview ? alphaState.preview.allocation : null;
  const alphaIds = Array.from(
    new Set([
      ...Object.keys((current && current.weights) || {}),
      ...Object.keys((proposed.v1 && proposed.v1.weights) || {}),
      ...Object.keys((proposed.v2 && proposed.v2.weights) || {}),
      ...Object.keys((proposed.v3 && proposed.v3.weights) || {}),
    ]),
  ).sort();
  const contrib = ov.strategy_contributions || {};

  const tableHtml = alphaIds.length
    ? `<div class="table-wrap"><table><thead><tr><th>Alpha</th><th>Current saved</th><th>V1 Equal Risk</th><th>V2 Vol/Corr</th><th>V3 Kakushadze–Yu*</th><th>Return contrib</th><th>Dollar contrib</th></tr></thead><tbody>${alphaIds
        .map((id) => {
          const c = contrib[id] || {};
          return `<tr><td>${escapeHTML(id)}</td><td>${alphaPct(Number((current && current.weights && current.weights[id]) || 0) * 100)}</td><td>${alphaPct(Number((proposed.v1 && proposed.v1.weights && proposed.v1.weights[id]) || 0) * 100)}</td><td>${alphaPct(Number((proposed.v2 && proposed.v2.weights && proposed.v2.weights[id]) || 0) * 100)}</td><td>${alphaPct(Number((proposed.v3 && proposed.v3.weights && proposed.v3.weights[id]) || 0) * 100)}</td><td>${alphaPct(c.cumulative_return_contribution_pct)}</td><td>${safeMoney(c.dollar_contribution)}</td></tr>`;
        })
        .join("")}</tbody></table></div><p><small>*V3 (Kakushadze–Yu Section 3.20) is strictly research-only. Unapproved strategy inversion is blocked across all models.</small></p>`
    : "<p>No computed risk allocations yet. Run a research cycle above to populate current and proposed allocations.</p>";

  const previewHtml = preview
    ? `<div class="notice">Preview (${escapeHTML(preview.model.toUpperCase())}) · Turnover ${alphaNum(preview.turnover, 3)}${preview.warnings && preview.warnings.length ? " · " + escapeHTML(preview.warnings.join(" | ")) : ""}</div>`
    : "";

  return `<div class="grid two"><div class="card"><h2>Allocation model selection</h2><p>Saved through the existing authenticated configuration audit log. Does not enable live trading or override Risk Engine limits.</p><form data-action="allocation_model">${label(
    "Selected allocation model",
    `<select name="allocation_model">${["v1", "v2", "v3"]
      .map(
        (m) =>
          `<option value="${m}" ${m === data.state.allocation_model ? "selected" : ""}>${escapeHTML(modelLabel(m))}</option>`,
      )
      .join("")}</select>`,
  )}${reason()}<button>Save allocation model</button></form><div class="rule"></div><h3>Preview constrained allocation</h3><form data-alpha="preview">${label(
    "Preview model",
    `<select name="model">${["v1", "v2", "v3"]
      .map(
        (m) =>
          `<option value="${m}" ${m === ov.selected_model ? "selected" : ""}>${escapeHTML(modelLabel(m))}</option>`,
      )
      .join("")}</select>`,
  )}<div class="grid two">${label("Max alpha weight", '<input name="max_weight" type="number" min="0.05" max="1" step="0.05" value="0.50" required>')}${label("Max turnover", '<input name="max_turnover" type="number" min="0.05" max="2" step="0.05" value="0.50" required>')}</div>${label("Covariance shrinkage (0–1)", '<input name="shrinkage" type="number" min="0" max="1" step="0.05" value="0.25" required>')}<button class="secondary" ${alphaBusy || !alphaIds.length ? "disabled" : ""}>Preview allocation</button></form>${previewHtml}</div><div class="card"><h2>Current & proposed risk allocations · Strategy contribution</h2>${tableHtml}</div></div>`;
}

function individualPerformanceSection(ov) {
  const alphas = ov.alphas || [];
  const indiv = (ov.selected_ensemble && ov.selected_ensemble.individual_metrics) || {};
  if (!alphas.length) {
    return '<div class="card"><h2>Alpha registry & individual alpha performance</h2><p>No versioned alphas recorded yet. Run a research cycle to evaluate the five mathematical strategies from 151 Trading Strategies.</p></div>';
  }
  return `<div class="card"><div class="row"><h2>Alpha registry & individual alpha performance</h2><span class="badge">PAPER / RESEARCH ONLY</span></div><div class="table-wrap"><table><thead><tr><th>Alpha / version</th><th>Portfolio</th><th>Deployment status</th><th>OOS return</th><th>Sharpe</th><th>Max DD</th><th>Holdout trades</th><th>Trials</th><th>Tax character</th></tr></thead><tbody>${alphas
    .map((a) => {
      const m = indiv[a.alpha_id] || (a.metadata && a.metadata.holdout_metrics) || {};
      const holdoutM = (a.metadata && a.metadata.holdout_metrics) || m;
      return `<tr><td><strong>${escapeHTML(a.name)}</strong><br><small>${escapeHTML(a.alpha_id)} · v${a.version} · ${escapeHTML(a.source_id)}</small></td><td>${escapeHTML(pretty(a.portfolio))}</td><td><span class="badge">${escapeHTML(pretty(a.deployment_status))}</span>${a.research_only ? "<br><small>Research only</small>" : ""}</td><td>${alphaPct(holdoutM.total_return_pct)}</td><td>${alphaNum(holdoutM.sharpe_ratio, 2)}</td><td>${alphaPct(holdoutM.max_drawdown_pct)}</td><td>${escapeHTML(holdoutM.trades ?? "—")}</td><td>${escapeHTML((a.metadata && a.metadata.trial_count) ?? "—")}</td><td>${escapeHTML(pretty(holdoutM.tax_character || "short_term"))}</td></tr>`;
    })
    .join("")}</tbody></table></div></div>`;
}

function regimePerformanceSection(ov) {
  const regPerf = ov.regime_performance || {};
  const entries = Object.entries(regPerf);
  if (!entries.length) {
    return '<div class="card"><h2>Market regime performance</h2><p>Regime performance populates after running a research cycle. Manual portfolio regime overrides in Day/Swing/Long-Term portfolios remain unaffected.</p></div>';
  }
  return `<div class="card"><h2>Market regime performance</h2><p>Research-only causal return breakdown across Quant Engine’s six regimes. Does not alter manual portfolio regime controls.</p><div class="table-wrap"><table><thead><tr><th>Strategy / Ensemble</th>${regimes.map((r) => `<th>${escapeHTML(pretty(r))}</th>`).join("")}</tr></thead><tbody>${entries
    .map(
      ([name, byReg]) =>
        `<tr><td>${escapeHTML(name)}</td>${regimes
          .map((r) => {
            const cell = byReg[r];
            return `<td>${cell ? alphaPct(cell.total_return_pct) + "<br><small>n=" + escapeHTML(cell.observations) + "</small>" : "—"}</td>`;
          })
          .join("")}</tr>`,
    )
    .join("")}</tbody></table></div></div>`;
}

function queueAndFailuresSection(ov) {
  const queue = ov.queue || [];
  const failed = ov.failed_experiments || [];
  const budget = ov.budget || {};
  const usage = ov.usage || {};

  const queueRows = queue
    .map(
      (h) =>
        `<tr><td><strong>${escapeHTML(h.title)}</strong><br><small>${escapeHTML(h.strategy_key)} · ${escapeHTML(h.source_id)}</small></td><td>${escapeHTML(pretty(h.portfolio))}</td><td><span class="badge">${escapeHTML(pretty(h.status))}</span></td><td>${h.formula_verified ? "Verified" : "Pending"} / ${h.causality_verified ? "Causal" : "Pending"}</td><td>${h.rejection_reasons && h.rejection_reasons.length ? escapeHTML(h.rejection_reasons[0]) : "—"}</td></tr>`,
    )
    .join("");

  const failedRows = failed.length
    ? failed
        .map(
          (f) =>
            `<tr><td>${escapeHTML(f.alpha_id || f.strategy_key)}<br><small>${escapeHTML(f.experiment_id)}</small></td><td>${escapeHTML(f.dataset_kind || "—")} · ${escapeHTML(f.trial_count ?? "—")} trials</td><td><span class="badge badge-warn">${escapeHTML(f.status)}</span></td><td>${escapeHTML((f.rejection_reasons || []).join(" · ") || "Unspecified")}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="4">No failed or rejected experiments recorded yet.</td></tr>';

  return `<div class="grid two"><div class="card"><div class="row"><h2>Research experiment queue</h2><span class="badge">Budget ${escapeHTML(usage.experiments_run ?? 0)}/${escapeHTML(budget.max_experiments ?? 5)} exps</span></div><p>Continuous AI research pipeline (7-step verification, controlled dev trials, walk-forward + holdout, cost/corr/tax check). Never auto-promotes to paper or live.</p><form data-alpha="run-cycle"><div class="grid two">${label("Hypotheses to process", '<input name="max_steps" type="number" min="1" max="5" step="1" value="5" required>')}${label("Max trials / hypothesis", '<input name="max_trials_per_hypothesis" type="number" min="1" max="10" step="1" value="2" required>')}</div><div class="grid two">${label("Compute budget (seconds)", `<input name="max_compute_seconds" type="number" min="1" max="120" step="1" value="${escapeHTML(budget.max_compute_seconds ?? 25)}" required>`)}${label("AI spend budget ($)", `<input name="max_ai_cost_usd" type="number" min="0" max="100" step="0.25" value="${escapeHTML(budget.max_ai_cost_usd ?? "2.00")}" required>`)}</div><button ${alphaBusy ? "disabled" : ""}>${alphaBusy ? "Running research cycle…" : "Run controlled research cycle (synthetic demo)"}</button></form><div class="table-wrap"><table><thead><tr><th>Hypothesis</th><th>Portfolio</th><th>Status</th><th>Audit</th><th>Latest note</th></tr></thead><tbody>${queueRows}</tbody></table></div></div><div class="card"><h2>Failed experiments & rejection reasons</h2><p>Do not assume published strategies are profitable. Every rejected or failed hypothesis remains auditable with explicit rejection reasons.</p><div class="table-wrap"><table><thead><tr><th>Experiment</th><th>Dataset / trials</th><th>Outcome</th><th>Rejection reasons</th></tr></thead><tbody>${failedRows}</tbody></table></div></div></div>`;
}

function paperPermissionsSection(ov) {
  const approved = (data && data.state && data.state.strategies
    ? data.state.strategies.filter((s) => s.stage === "paper" && s.approved_by)
    : []);
  if (!approved.length) {
    return '<div class="card"><h2>Approved paper-strategy permissions</h2><p>No human-approved paper strategies yet. Advance a validated strategy to the paper stage in Strategy Lab first; once approved, its paper execution switch and permitted regimes can be managed here through authenticated configuration controls.</p></div>';
  }
  return `<div class="card"><div class="row"><h2>Approved paper-strategy permissions</h2><span class="badge">AUTHENTICATED AUDIT</span></div><p>Changes save to the append-only configuration version store with actor and reason. Risk limits and live trading locks remain unchanged.</p>${approved
    .map(
      (s) =>
        `<div class="perm-item"><h3>${escapeHTML(s.name)} <span class="badge">${escapeHTML(sections[s.portfolio])}</span></h3><small>Approved by ${escapeHTML(s.approved_by)} · Currently ${s.enabled ? "Enabled" : "Disabled"}</small><form data-action="strategy_permissions" data-target="${escapeHTML(s.id)}"><label><input type="checkbox" name="enabled" ${s.enabled ? "checked" : ""}>Paper strategy enabled preference</label><div class="regime-grid">${regimes
          .map(
            (r) =>
              `<label class="regime-check"><input type="checkbox" name="regimes" value="${escapeHTML(r)}" ${s.regimes.includes(r) ? "checked" : ""}>${escapeHTML(pretty(r))}</label>`,
          )
          .join("")}</div>${reason()}<button>Save paper permissions</button></form></div>`,
    )
    .join("")}</div>`;
}

function catalogSection(ov) {
  const cat = ov.catalog || [];
  if (!cat.length) return "";
  return `<div class="card"><h2>Verified mathematical strategy library (151 Trading Strategies)</h2><p>Primary reference: Kakushadze &amp; Serur (SSRN 3247865 / arXiv:1912.04492). Adapted for US equities and ETFs with zero look-ahead bias.</p>${cat
    .map(
      (item) =>
        `<details><summary>${escapeHTML(item.name)} · Section ${escapeHTML(item.section)} · ${escapeHTML(pretty(item.portfolio))} · Research only</summary><p><strong>Formula:</strong> ${escapeHTML(item.academic_source.formula_summary)}</p><p><strong>Verification:</strong> ${escapeHTML(item.academic_source.verified_notes)}</p><ul>${(item.academic_source.adaptations_for_us_equities || [])
          .map((a) => `<li>${escapeHTML(a)}</li>`)
          .join("")}</ul></details>`,
    )
    .join("")}</div>`;
}

function alphaPanel() {
  if (!alphaState.loaded) {
    return `<div class="card"><div class="row"><h2>Alpha Center</h2><button type="button" class="secondary" data-alpha-action="load">Load Alpha Center</button></div><p>${alphaState.loading ? "Loading quantitative research library and alpha analytics…" : "Quantitative research library, correlation diagnostics, and V1/V2/V3 alpha combination."}</p>${alphaState.error ? `<p role="alert">${escapeHTML(alphaState.error)}</p>` : ""}</div>`;
  }
  const ov = alphaState.overview;
  const activeCount = (ov.alphas || []).length;
  const rejectedCount = (ov.failed_experiments || []).length;
  const budgetUsage = ov.usage || {};
  return `<div class="notice">${escapeHTML(ov.notice)}</div><div class="grid">${metric("Registered alphas", activeCount, "Versioned · research & paper only")}${metric("Allocation model", escapeHTML(ov.selected_model.toUpperCase()), escapeHTML(modelLabel(ov.selected_model)))}${metric("Rejected / failed", rejectedCount, "Compute used: " + alphaNum(budgetUsage.compute_seconds, 2) + "s")}</div>${queueAndFailuresSection(ov)}${individualPerformanceSection(ov)}${heatmapSection(ov.correlation)}${ensembleSection(ov)}${allocationSection(ov)}${regimePerformanceSection(ov)}${paperPermissionsSection(ov)}${catalogSection(ov)}`;
}

document.addEventListener("click", async (event) => {
  const btn = event.target.closest("[data-alpha-action]");
  if (!btn || alphaBusy) return;
  if (btn.dataset.alphaAction === "load") {
    await loadAlpha();
  }
});

$("content").addEventListener("submit", async (event) => {
  const form = event.target;
  if (!form.dataset.alpha) return;
  event.preventDefault();
  if (alphaBusy) return;
  const btn = form.querySelector("button");
  const fields = Object.fromEntries(new FormData(form));
  const tokenAtStart = accessToken;
  alphaBusy = true;
  if (btn) btn.disabled = true;
  try {
    if (form.dataset.alpha === "run-cycle") {
      const res = await api("/api/alpha/run-cycle", {
        max_steps: Number(fields.max_steps),
        max_trials_per_hypothesis: Number(fields.max_trials_per_hypothesis),
        max_compute_seconds: Number(fields.max_compute_seconds),
        max_ai_cost_usd: fields.max_ai_cost_usd,
        dataset_mode: "synthetic_demo",
      });
      if (!accessToken || tokenAtStart !== accessToken) return;
      alphaState.overview = res.overview;
      alphaState.loaded = true;
      alphaState.preview = null;
      notify(
        "Research cycle completed (" +
          res.cycle.processed +
          " hypotheses processed). No strategies were auto-promoted.",
      );
    } else if (form.dataset.alpha === "preview") {
      const res = await api("/api/alpha/preview-allocation", {
        model: fields.model,
        max_weight: fields.max_weight,
        max_turnover: fields.max_turnover,
        shrinkage: Number(fields.shrinkage),
        reason: "Dashboard allocation preview for " + fields.model,
      });
      if (!accessToken || tokenAtStart !== accessToken) return;
      alphaState.preview = res;
      notify("Allocation preview calculated for " + fields.model.toUpperCase() + ".");
    }
    render();
  } catch (error) {
    if (!accessToken || tokenAtStart !== accessToken) return;
    notify(error.message, true);
  } finally {
    alphaBusy = false;
    if (btn) btn.disabled = false;
  }
});
