# AGENTS.md — public-relay / Quant Engine V1

Read this before changing anything in this repository. It applies to people and to coding agents.

## Objectives

1. Build Quant Engine V1: a paper-first automated trading platform with AI-assisted strategy research and an independent risk manager.
2. Keep the existing relay working. TradingView alerts, the Vercel FastAPI app, Public.com selection and the live PremarketES book, Alpaca paper trading, and the Supabase state all behave exactly as on `main` unless a change is explicitly scoped and reviewed.
3. Prefer capital preservation over opportunity. A missed trade is acceptable. An unbounded loss, an exit that cannot be sent, or an unreviewed live order is not.

## Hard safety rules

These are not configurable and are not waived by a task description.

- Do not enable live trading. Do not set `MODE=LIVE`, `LIVE_CONFIRM`, or `LIVE_STRATEGIES`, and do not widen `LIVE_STRATEGY_CEILING` or the live caps in `api/index.py`.
- Do not place brokerage orders, including paper orders, from an agent session. Tests use the fakes in `tests/` and `quant_engine/tests/`.
- Do not read, print, request, or commit live credentials. `PUBLIC_SECRET`, `PUBLIC_ACCOUNT_ID`, Alpaca keys, `SUPABASE_SERVICE_KEY`, `WEBHOOK_PASSPHRASE`, and `CRON_SECRET` stay in the host's secret manager.
- Do not modify production environment variables, Vercel project settings, or Supabase production schema. New migrations are proposed in a PR and applied by Nick.
- Do not deploy to production and do not merge into `main`. Work lands on `dev/quant-engine` (or a branch off it) and goes through a pull request.
- Quant Engine V1 never approves a `LIVE` entry. Changing that is a separate, human-approved milestone.

## Risk constraints

Initial paper-trading defaults, set in `quant_engine/risk/config.py` and overridable through `RISK_*` env vars:

| Constraint | Default |
| --- | --- |
| Maximum planned account risk per trade | 0.25% of equity |
| Daily loss threshold (stops new entries for the New York day) | 0.75% of day-start equity |
| Weekly loss threshold (stops new entries for the ISO week, engages the kill switch) | 2.00% of week-start equity |
| Aggregate planned open risk | 0.50% of equity |
| Gross entry notional | 100% of equity |

Rules every code path must follow:

1. **Protective exits are never blocked by entry restrictions.** A stop, target, flatten, reversal close, or kill-switch flatten must not be refused because of the kill switch, a loss threshold, an exposure limit, an order-count cap, a spend cap, a premium cap, the entry time window, or an unreadable risk state. Only a malformed request (bad id, non-positive or fractional quantity) may be refused. Exits are clamped to the held quantity, never above it.
2. **Entries fail closed.** Missing, stale, or invalid account data, an unreadable risk state, or any limit at zero headroom rejects the entry.
3. **Risk is reserved atomically.** An approved entry reserves its planned risk inside the same lock that checked the headroom, so concurrent requests cannot both spend it.
4. **Loss lockouts latch.** A breached day or week stays blocked for the rest of that period even if equity recovers.
5. **The kill switch is persistent and manual to clear.** It survives restarts. The engine may engage it; only a named person may disengage it, with a reason.
6. **The risk engine is independent of strategy code.** Strategies request a size; they do not compute or override limits. Limits change only through config, in a reviewed PR.
7. **Money is `Decimal`.** No floats in prices, risk, or P&L.

## Research rules

1. **Out-of-sample or it did not happen.** Pick parameters on one period and report the held-out period separately. Use walk-forward splits for anything with more than a handful of parameters.
2. **No lookahead.** A signal may use only bars closed at or before the decision time. Session facts (prior low, 9:30 open) must be available at the entry timestamp.
3. **Model costs.** Use the bid/ask the relay would pay (mid with the configured fallback), commissions, and the option tick. The research backtester mirrors relay selection and exits (`research/backtest.py`); keep it in sync when relay rules change.
4. **Count every trial.** Record the size of the search (symbols × signals × parameter sets). Report it next to results and discount for it; the QQQ result was chosen from 135 pairs.
5. **Minimum evidence.** Do not propose a strategy for paper trading on fewer than 30 held-out trades, or for anything beyond paper without at least 20 paper sessions inside the risk limits above.
6. **Reproducible.** Results in `research/results/` must regenerate from code in the repo and a named data source. Raw data stays in `research/data/` (git-ignored).
7. **AI assistance is advisory.** Model-generated strategy ideas, code, or analysis are hypotheses. They go through the same backtest, out-of-sample, and paper process, and a person approves promotion.
8. **Size with the engine.** Backtests that report dollar results should size trades with `quant_engine.risk.sizing.size_position` at the defaults above, so research and paper sizing agree.

## Development procedures

### Branches and review

- Base branch for this program: `dev/quant-engine`. Open PRs against it. Do not push to or merge into `main`.
- One logical change per commit. Describe behavior changes and any deviation from the risk rules in the commit message and PR.
- Changes to `api/index.py` must say whether the live book is affected. Any change that touches the live path needs Nick's explicit approval in the PR.

### Tests

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest -q tests research/tests quant_engine/tests
```

- All three suites must pass before a push.
- A change to risk logic must add tests for normal operation, invalid data, concurrent requests, and each limit it touches being breached, and must show that exits stay approved under that breach.
- Concurrency tests use real threads and, for file state, separate processes. Do not replace them with mocks.

### Code style

- Python 3.12, standard library first. The relay's only runtime dependencies are `fastapi` and `httpx`; do not add more without a reason in the PR.
- Imports at the top of the module. No inline imports.
- `match` over enums and literal unions ends in a `_never(value)` case so a new variant fails loudly.
- Comments only for constraints the code cannot show.

### Repository map

| Path | What it is |
| --- | --- |
| `api/index.py`, `api/flowvwap.py`, `app.py` | The production relay (Vercel). |
| `supabase/migrations/` | Ordered SQL; apply in order. |
| `tradingview/` | Pine strategies. |
| `research/` | Backtester, pricing model, results, research-only VectorBT adapter. |
| `requirements-research.txt` | Optional research extras. Not installed on the production relay. |
| `docs/QUANT_RESEARCH_AUDIT.md` | Open-source research audit and phase status. |
| `quant_engine/risk/` | Standalone portfolio risk engine. |
| `docs/ARCHITECTURE.md` | Current architecture and known weaknesses. |
| `docs/RISK_ENGINE.md` | Risk engine design and API. |
| `docs/MILESTONE_1.md` | What milestone 1 changed, test results, open issues. |
