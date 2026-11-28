# Quant research audit and VectorBT adapter

Reviewed 27 November 2026 (America/New_York), on top of `5a97c13` (Alpha Center). This branch does not deploy, does not place orders, and does not merge into `main`.

Phase 0 is the audit. Phase 1 is a research-only daily equity/ETF adapter for [vectorbt 1.2.1](https://github.com/polakowo/vectorbt). Phases 2–6 were license-reviewed and are not implemented.

## Plain-language summary

The trading relay, the dashboard, the risk manager, and the Alpha Center are unchanged in behavior. What was added is a separate research tool that can test simple daily stock and ETF ideas quickly, with fees, a held-out period, and a check that the idea is not peeking at future prices. It cannot trade, and it does not pretend to model options. Anything it recommends still needs a person.

The extra software stays out of the production app. Running it costs no added data or model fees. The $30 per month research cap is in the code so a later phase cannot silently spend more.

## Phase 0 — What is already built

| Area | Where | State |
| --- | --- | --- |
| Live relay | `api/index.py`, `app.py` | TradingView alerts, Public.com live PremarketES book, Alpaca paper orders, Supabase state. Not modified. |
| Independent risk engine | `quant_engine/risk/` | Decimal limits, atomic risk reservation, latched loss lockouts, persistent kill switch. Entries fail closed. Protective exits are not blocked by entry limits. |
| Dashboard | `quant_engine/dashboard/` | Sign-in, planning state, Tax Center, manual regime controls, read-only Alpaca paper and Public.com account views. |
| Strategy Lab backtests | `quant_engine/backtesting/` | Four share templates (bull/bear opening-range breakout, bull/bear VWAP pullback) for SPY and QQQ five-minute bars. One parameter set per run. Development date is chosen by the user; the rest is held out. |
| Daily research library | `research/` | Five book strategies, point-in-time feature store, causal next-open simulator, purged walk-forward, reserved holdout, trial count. |
| Alpha Center | `alpha/`, dashboard Alpha Center | Registry, correlation, three allocation methods, ensemble report, experiment queue, authenticated budget and allocation controls. |
| Research loop | `research/orchestrator.py` | Seven-step loop with experiment, time, and AI-spend caps. It does not call an outside model. It cannot approve paper or live trading. |

### Incomplete on purpose

- No historical proof yet. The mathematical strategies and this adapter reject synthetic data. A paper recommendation still needs 30 held-out trades on real history and a person.
- Strategy Lab does not sweep parameters or walk forward. The daily research path does. Those two engines are not the same fill model.
- The orchestrator has a budget for AI tokens and dollars, and no model client.
- Alpha Center has no Factor Lab. Failed experiments are visible for the existing research loop, not for Qlib or RD-Agent.
- Hosted Supabase migrations are not applied by this work. Nick applies reviewed SQL.
- Known live-relay weaknesses in `docs/ARCHITECTURE.md` (late exits, exit-order caps) are unchanged. They need their own review because they touch the live book.

### Research orchestration

`ResearchOrchestrator` selects a queued hypothesis, checks the formula against the cited source, audits causality, sweeps a capped grid on development data, walk-forwards and holds out, then records costs, correlation, and tax impact. `recommended_for_paper` can be true. `paper_approved` is always false. Live credentials are not read.

### Backtesting engines

1. `research/backtest.py` mirrors relay option selection and exits for research. It is not a general equity simulator.
2. `quant_engine/backtesting/` is event-style five-minute share trading for four templates. It uses `size_position` and `RiskEngine`. It does not model option fills. Benchmark is buy-and-hold price return.
3. `research/validator.py` is the daily next-open equity simulator. Signals from the prior close fill at the next open. Size and stops go through the risk engine. Costs are half-spread, slippage, per-share commission, and short borrow.
4. `research/vectorbt_adapter.py` is the new daily vectorized path. See Phase 1. It is not a substitute for 2 or 3.

### Strategy and alpha interfaces

- A research strategy implements `fit` on the training slice and `generate_signals` returning `{date: {symbol: weight}}`. Weights on date *t* may use bars closed at *t* and are filled on the next session.
- `AlphaRegistry.register` versions a PAPER-only definition. `paper_enabled` requires a named approver and `research_only=False`. This adapter never sets that.
- `ExperimentRecord.to_dict()` is the existing result shape. `VectorBTExperiment.to_dict()` is the vectorbt shape (engine, trials, walk-forward, holdout, benchmark, regimes, rejections). Publishing writes holdout daily returns into the same registry.

### Data available without a new purchase

- Synthetic daily bars from `generate_synthetic_us_equity_dataset`. Labeled synthetic. Not evidence.
- Caller-supplied daily OHLCV CSV via `load_daily_bars` or `parse_daily_ohlcv_csv`.
- Dashboard Alpaca **paper** IEX bars for SPY/QQQ, at most 180 calendar days per request, single venue. The new adapter does not call Alpaca.
- No options chain, no consolidated tape, no news archive wired to this adapter.

### Who is allowed to trade

| Path | Can send an order? |
| --- | --- |
| Live relay | Yes, only the existing PremarketES Public.com path. This branch does not change it. |
| Alpaca paper relay | Yes, when the host is in paper mode. This branch does not call it. |
| Dashboard account view | No. Read-only. |
| Risk engine | No. It approves or refuses a request. It does not connect to a broker. |
| VectorBT adapter | No. Local simulation only. |
| Research orchestrator | No. It cannot approve paper trading. |

Quant Engine V1 still cannot approve a LIVE entry.

### Dependency, license, and security review

Reviewed before adding a package. Nothing below was vendored into the repo.

| Project | License checked | Python | Decision |
| --- | --- | --- | --- |
| vectorbt 1.2.1 (`polakowo/vectorbt`, PyPI and `LICENSE.md` at tag v1.2.1) | Apache-2.0 **with Commons Clause** | `>=3.11,<3.15`. This machine is 3.12.3. | **Use, research-only.** Core package only. |
| Microsoft Qlib | MIT | Separate stack | Not installed. |
| Microsoft RD-Agent | MIT | Needs an LLM key | Not installed. |
| QuantConnect LEAN | Apache-2.0 | C# / Docker | Not installed. |
| TradingAgents | Apache-2.0 | LLM app | Not installed. |
| OpenBB (`LICENSE` on `main`) | Apache-2.0 as of this review. Provider terms are separate. | Large app | Not installed. |
| virattt/ai-hedge-fund | MIT | LLM app | Not installed. |
| Kronos | MIT code. Weights and training corpus are separate. | GPU model | Not downloaded. |

Commons Clause on vectorbt means we may use it internally and may not sell vectorbt, or a product whose value comes substantially from vectorbt. It is pinned in `requirements-research.txt` and imported only by the research adapter. `requirements.txt` is still `fastapi` and `httpx`.

The core install resolved, on this machine, to numpy 2.5.5, pandas 3.1.1, numba 0.68.1, scipy 1.18.1, and scikit-learn 1.9.1, plus plotting libraries. Those must not be added to the Vercel relay. The vectorbt `[full]` extra pulls alpaca-py, ccxt, and yfinance and was not installed. Yahoo's terms are a reason to keep yfinance out. The adapter source does not name brokerage secrets or vendor download classes.

There is no GitHub Actions workflow in the repo. The local command remains `python -m pytest -q tests research/tests quant_engine/tests`. VectorBT tests skip if the extra is not installed; with it installed they run.

### Cost

| Item | Incremental cash |
| --- | --- |
| This adapter, local CPU | $0. `estimated_incremental_monthly_usd()` returns 0. |
| Existing Alpaca IEX paper data already configured | $0 added, and this phase does not call it. |
| Default research cap on the adapter | $30. A higher estimate refuses to run. |
| Qlib dump, RD-Agent tokens, LEAN data, Kronos GPU, paid OpenBB providers | Not enabled. Any one of them can pass $30. Do not turn one on without a separate approval. |

Target for later phases: stay inside $30 per month of new spend until Nick raises it.

## Phase 1 — VectorBT adapter

Code: `research/vectorbt_adapter.py`, tests in `research/tests/test_vectorbt_adapter.py`, pin in `requirements-research.txt`.

What it does:

- Daily equity and ETF bars only. Options and any bar frequency other than `1D` raise. Two bars in one session raise.
- Causal signals. A moving-average or trailing-return decision is stored on the close that produced it and filled on the **next** open. Same-bar open fills would look ahead.
- Capped parameter sweep on the development validation slice. The holdout is not passed into selection. Hard cap is 48 trials.
- Percent slippage from half the spread plus the slippage assumption. Per-share commission is not inside vectorbt.
- Purged walk-forward of the already chosen parameters, labeled as development robustness, not a second holdout.
- Regime tags from the existing causal classifier, scored on holdout returns.
- Comparison of the two built-in strategies on development data only.
- Buy-and-hold on the same holdout slice (costed next open, plus close-to-close price return).
- Standardized `VectorBTExperiment` record. Optional publish into `AlphaRegistry` as `candidate` or `rejected`, `research_only=True`, `paper_approved=False`.
- Every holdout path is checked against a small next-open reference. A gap above a tenth of a cent per $1 of float noise, scaled for account size, rejects the run.

### Tests and checks

With `requirements-research.txt` installed: `385 passed` across `tests`, `research/tests`, and `quant_engine/tests`, including 7 new adapter tests. Those tests check the options refusal, prefix invariance of signals, reference-fill agreement, risk-engine entries following the prior close signal, holdout isolation, synthetic rejection, the $30 guard, and the absence of download or credential names.

Verified on synthetic series only. That is not evidence of an edge. The runs were rejected for being synthetic, which is the intended result.

### Limitations

- VectorBT's engine is floating point. Stored metrics are Decimal conversions of those returns. The risk engine's own money path stays Decimal.
- Full notional long/flat inside vectorbt. The risk engine's 0.25% trade risk, share commission, and stops are applied only in `validate_against_research_engine`. Dollar P&L will not match, and is not supposed to.
- Does not model intraday options, assignment, expiration, or the live relay.
- No market-data download. Real evidence needs a daily equity/ETF history the caller supplies, with corporate actions handled before import.
- Two strategies only: SMA crossover and trailing momentum. They are audit vehicles for the adapter, not paper candidates.
- Walk-forward does not re-tune inside each fold. Tuning stays on the development validation slice so the fold tests are not spent on selection.
- Publishing a rejected synthetic result writes a research registry row. It does not enable a strategy in the dashboard.

### Operating cost

$0 per month at the current pin, on a local or existing research machine. No GPU and no new API.

### Next step for Phase 1

Point `load_daily_bars` at a real daily history for one ETF, keep the default trial cap, and read the rejection reasons. Do not promote from that single run.

## Phase 2 — RD-Agent and Qlib

Not implemented.

Both are MIT and fit an isolated environment, not the relay image. Qlib wants its own dataset dump and a heavy ML stack. RD-Agent wants a model API key and will keep spending until a budget stops it. Neither may see live brokerage credentials, edit production code, or promote a strategy.

The factor list (gamma and gamma-flip distance, VWAP deviation, relative volume, opening range, liquidity sweeps, premium versus underlying, cross-market strength, VIX versus realized volatility) is not computed here. Gamma and premium divergence need an options history this repo does not have. VWAP and opening range already have a five-minute share backtester; a Qlib factor should publish through `ExperimentRecord` or `VectorBTExperiment`, not a second executor.

Next step: one isolated virtualenv, no broker environment variables, one factor, point-in-time bars, a chronological split, and a hard dollar cap inside $30. Do not download a Qlib bundle until its redistribution terms are read.

## Phase 3 — Factor Lab

Not implemented.

Alpha Center already shows the registry, queue, correlations, ensemble, and rejections for the current research loop. A Factor Lab screen (versions, hypotheses, data needs, information coefficients, regime strength, feature importance, cost, approval) should wait until a factor run writes real rows. Demonstration coefficients must stay labeled synthetic, the same way synthetic datasets are labeled today. No screen was added that could be mistaken for live factor evidence.

## Phase 4 — LEAN

Not implemented.

LEAN is Apache-2.0 and is the right kind of independent event-driven check for a strategy that has already survived our daily and five-minute backtests. It is a second fill model, not a second broker. US equity-option data, assignment, exercise, and buying power are QuantConnect data-license questions and are not free. This phase must not point LEAN at Alpaca or Public.com.

Next step, after a strategy has 30 held-out trades in our engine: one local LEAN equity run on the same CSV, compared with Strategy Lab and vectorbt, with the fill differences written down. No subscription until Nick approves the data cost against the $30 cap.

## Phase 5 — AI investment committee

Not implemented.

TradingAgents and ai-hedge-fund are usable as ideas for separate bull, bear, fundamental, technical, macro, tax, and risk write-ups. OpenBB's core license file is Apache-2.0; each market-data provider still has its own terms. None of them should emit a brokerage order. The existing read-only account views and Alpaca IEX history are the data to reuse. A new vendor is a new cost and a new terms review.

Next step: a structured memo with citations and an explicit "human review, no order" status, spending inside the orchestrator's AI dollar cap. Not a trading agent.

## Phase 6 — Kronos

Not implemented.

The code is MIT. The weights are a separate download, the training cutoff was not verified in this pass, and a fair test needs a GPU. A foundation model trained on market history can leak into any test window that overlaps its training period. That comparison against a simple baseline and the existing kNN model stays blocked until the cutoff date is written down and the test window starts after it.

No weights were downloaded. No GPU was purchased.

## What this branch does not do

- No production deploy, no Vercel or Supabase setting changes, no merge to `main`.
- No live or paper brokerage order.
- No research path reads live credentials.
- No bypass of the risk engine on the path that sizes real research trades.
- No automatic promotion. `paper_approved` from this adapter is false.
- No edits to approved production strategies in `api/`.
- No unbounded sweep. Trial count is capped.
- No second order router.
- Day, swing, and long-term books stay separate. VectorBT rows are marked swing research, not the day-trading book.
