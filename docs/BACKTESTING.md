# Quant Engine backtesting — first executable milestone

The Strategy Lab now runs four explicit, versioned templates: Bullish Opening Range Breakout (ORB), Bearish Opening Range Breakout (ORB), Bullish VWAP Pullback, and Bearish VWAP Pullback. It supports SPY/QQQ **underlying shares** on complete five-minute regular sessions. Other arsenal ideas remain registry entries. A name match selects a template; the template's displayed rules, not free-form strategy prose, determine execution. Review those rules before running.

This branch preserves the existing dashboard and read-only account work. It does not change the production relay, place orders, enable strategies, advance research stages, deploy, or apply hosted migrations.

## Setup for the existing dashboard

1. Review the draft PR targeting `dev/quant-engine`. Its branch is `codex/quant-backtesting`, based on `codex/quant-broker-readonly` at `be787a6`. It includes the preceding dashboard and account-view changes because they are not yet on the development branch. Do not merge into `main` as part of this milestone.
2. For an isolated development preview, use this branch with the dashboard entrypoint `quant_engine.dashboard.app:app`. Preserve the dashboard's current separate deployment packaging; the repository root `app.py` still serves the existing relay. Include the entire `quant_engine` package, including the new `backtesting` directory and `dashboard/static/research.js`. Dependencies remain `requirements-dashboard.txt`; no new runtime packages.
3. In the **dashboard's development Supabase project**, apply `supabase/migrations/20261125184016_quant_backtesting_records.sql` after the existing dashboard migration. Review the SQL first. It adds only `qe_research_records`, ownership policies, a timestamp trigger and an index. It does not alter relay tables. Apply through your normal reviewed migration process or the SQL editor; do not blindly push every repository migration to an unrelated project. This step has not been performed on your hosted database.
4. Preserve existing `QE_AUTH_MODE=supabase`, `QE_SUPABASE_URL`, and `QE_SUPABASE_PUBLISHABLE_KEY` settings. The API uses the signed-in user's token, not a service key. Local-only SQLite setup remains in `docs/DASHBOARD.md`; its research table is created automatically.
5. Historical import can use the dashboard's already configured **paper** data-access settings, `ALPACA_PAPER_KEY_ID` and `ALPACA_PAPER_SECRET_KEY`. There is no new credential requirement if those are present. Never paste credentials into a CSV, strategy definition or chat. Without that access, upload CSV. Public account access does not supply backtest history.
6. Deploy the isolated dashboard preview through your existing process, sign in, open **Strategy Lab → Open backtest workbench**. Test the synthetic demo first, then import actual history. The currently deployed site does not acquire these features until you deploy this code and apply the research migration.

## First functional check

- Load **synthetic demo dataset**. Select one of the four existing registered strategies, set the last development date to **2024-02-12**, review assumptions and run. The manufactured sample spans 12 sessions and must show a conspicuous synthetic label. It is not evidence of profitability.
- Open the saved report. Verify development and held-out results, costs, trade log and unchanged strategy settings.
- Download the full JSON report and input CSV. Sign out/in and verify history persists.

## Actual historical research

Import SPY or QQQ data through the existing Alpaca IEX access, or supply a CSV. Import accepts at most 180 calendar days per provider request, ending before today; saved datasets are limited to 25,000 bars / 3 MB. The importer requests only IEX, never upgrades feeds or purchases access. It checks the provider calendar, including early closes, follows bounded pagination, and rejects missing sessions/bars. IEX is a single venue: its prices/volume and therefore VWAP can differ from consolidated-market data. Incomplete IEX history may fail validation; use a complete, appropriately sourced CSV in that case.

CSV columns must be in this exact order:

```csv
timestamp,open,high,low,close,volume,session_close
2024-02-05T09:30:00-05:00,493.10,493.20,493.00,493.15,10000,2024-02-05T16:00:00-05:00
```

This single row illustrates the format; it is not a usable dataset. Include every five-minute bar from 09:30 through 15:55 New York, or through 12:55 for a 13:00 close. Explicit timezone offsets are required. At least four complete sessions are required, with at least two per split. CSV session schedules and historical provenance are user supplied; omitted entire sessions and holidays cannot be independently detected. Missing intraday bars, duplicates, unsorted timestamps, invalid OHLC, non-finite numbers and unfinished/future bars are rejected. Inputs are normalized, hashed, saved and downloadable.

Reserve a later period before selecting parameters. The split date is the **last development session**; all later sessions are held out. Each partition starts with its own chosen capital and fresh risk state, so returns are not additive. Viewing a held-out result makes it observed research; do not keep tuning to that period and call it unseen. Walk-forward evaluation and automatic parameter searches are not implemented. Each request tests one symbol × one template × one parameter set, and records a trial before computing. Failed and interrupted attempts remain recorded. History shows the latest 100; all older records remain stored. Consider all prior attempts, including external research, when interpreting performance.

## Exact execution model

- ORB uses the first 5, 15 or 30 minutes. A completed bar must cross the opening-range extreme; entry uses the **following bar's open**. The range cannot incorporate future bars.
- VWAP resets each session, using typical price `(high + low + close) / 3` weighted by volume. After the session EMA warmup, the previous close must be on the trend side of prior VWAP; the current bar touches VWAP and closes on the trend side, beyond a rising/falling EMA. Entry is at the following open. These are initial executable definitions, not reproductions of unspecified discretionary rules or the production relay's option selection.
- One position at a time; maximum 1–5 entries per session. No entry in the final 30 minutes. Stops use a fixed percentage of the cost-adjusted entry; targets are a multiple of that distance. If stop and target are touched on one bar, stop wins. Gap stops use the worse opening price; target gaps get no price improvement. Remaining positions flatten at the final bar's open. No overnight positions.
- Spread is the full quoted spread in basis points; half is charged per side, plus slippage per side. Commissions apply per share per side. Shorts charge an assumed annual borrow rate divided by 252 for each intraday trade. This conservative day-charge convention is not a broker's actual borrow invoice. Borrow availability is assumed, not verified. Short-share returns are not put-option returns.
- Sizing uses `size_position` and the existing independent risk engine. Defaults: 0.25% planned risk per trade, 0.75% daily loss headroom, 2% weekly loss headroom, 0.5% aggregate planned open risk and 100% entry notional. Costs are included in planned stop risk. A weekly breach engages the kill switch for the rest of that partition, including later weeks; there is no simulated human reset. Entry checks occur at candidate entries. Protective exits remain available. These independent runs do not model a shared multi-strategy portfolio.
- Dashboard regime preferences and entry-pause switches are not applied to historical research. No regime classifier is implemented. The report explicitly states the absence of a regime filter. The simulation is separate from deployment eligibility.

## Reports and evidence

Each report includes development and held-out net return, net P/L, average net trade P/L, win rate, profit factor, trade count, exposure, daily equity observations, bar-close drawdown, rejected entries, risk status and full trade records. Profit factor is undefined without losing trades. Trade timestamps distinguish signal-bar start, signal completion/entry, and exit-bar start; intrabar exit time is unknown.

The benchmark is simple buy-and-hold **price return**, from the first opening price to the final bar's open, without dividends/costs. Drawdown sampled at bar closes can understate intrabar losses. Gaps can exceed planned risk. Raw-price provider data has no corporate-action adjustment; choose/validate data accordingly. There is no option chain, option fill, margin, locate, capacity, market-impact, tax or combined-portfolio model.

Thirty historical held-out trades is only a minimum-count indicator, not proof of an edge or approval. Synthetic data never satisfies it. Results do not auto-fill evidence or advance the existing manual research lifecycle. Human review remains required. Do not choose a strategy solely by the highest return; compare the same dataset, reserved period, costs and risk assumptions, then inspect drawdown and individual trades.

## Reproduce a downloaded run

From the matching repository revision and installed dashboard dependencies:

```bash
python -m quant_engine.backtesting.cli quant-backtest-RUN_ID.json backtest-input.csv
```

The command performs no network calls. It verifies the input hash and implementation hash, recomputes both partitions and requires exact equality with the saved report. A changed implementation requires checking out the original revision. The export includes the registry snapshot, configuration version, template/parameter specification, dataset provenance and code hashes. Records are immutable through application permissions, but an authenticated owner can submit arbitrary JSON directly through their database access; these are user-owned research records, not independently attested financial results or execution authorization.

## Verification and limitations

Run all three Python suites:

```bash
python -m pytest -q tests research/tests quant_engine/tests
npm ci --prefix quant_engine/tests/browser
node quant_engine/tests/browser/migration.mjs
```

For the DOM workflow regression check, start the fake-credential local preview in one terminal, then run the check in another:

```bash
PYTHONPATH=. python quant_engine/tests/browser/serve_backtesting.py
node quant_engine/tests/browser/backtesting-dom.mjs
```

The preview binds only to loopback, uses a disposable database, seeds four test ideas, and has no broker configuration. DOM tests cover sign-in, loading the workbench, demo import, running/reporting, history and sign-out. They are not visual layout tests. The actual SQL migrations are tested in embedded PostgreSQL with representative auth roles; hosted Supabase integration and real provider access remain unverified. Browser visual verification was attempted but the browser tool timed out.

Runs execute synchronously within bounded input sizes. This milestone has no durable background queue; an interrupted invocation leaves an incomplete trial, and reusing its request ID will not rerun it. Choose shorter periods if the hosting runtime times out, inspect history, and explicitly start a new attempt. A later milestone can add durable jobs, walk-forward tests and additional strategy adapters without changing the live relay.

Provider references: [Alpaca historical stock bars](https://docs.alpaca.markets/us/reference/stockbars) and [trading calendar](https://docs.alpaca.markets/us/reference/legacycalendar).
