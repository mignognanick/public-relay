# Quant Engine dashboard — foundation milestone

This is a separate, authenticated paper-only control plane. It does not import `api/index.py`, connect a brokerage, place even paper orders, or change the production relay. `app.py` and `vercel.json` still serve the existing relay. Run the dashboard explicitly using the entrypoint below.

## Baseline and branch

Inspected remote `dev/quant-engine` at `4c465465d4ff80e381065cf63eace5d833a4cf66`, including Cursor's risk engine commit `423e66d` and milestone documentation. Fresh checkout was clean; 300 baseline tests passed. Unpushed changes in a separate Cursor checkout cannot be inspected here. This work is on `codex/quant-dashboard`, based on that branch, for a PR targeting **dev/quant-engine**, never main.

## Run locally without any service credentials

From the repository root, with Python 3.12 or newer:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dashboard.txt -r requirements-dev.txt
export QE_AUTH_MODE=local
export QE_SQLITE_PATH=var/quant-dashboard.sqlite3
read -rs 'QE_LOCAL_TOKEN?Choose a local access token of at least 32 characters: '
export QE_LOCAL_TOKEN
python -m uvicorn quant_engine.dashboard.app:app --host 127.0.0.1 --port 8765
```

The commands above use macOS's default Zsh. In Bash, replace the `read` line with:

```bash
read -r -s -p 'Choose a local access token of at least 32 characters: ' QE_LOCAL_TOKEN
export QE_LOCAL_TOKEN
```

Open **http://127.0.0.1:8765** and enter that same token. Use a fresh local-only token, never a broker or production secret. Do not source the relay's `.env`. The token remains in browser memory only; refresh/reopening requires sign-in. Sign out clears it. Local auth is a single operator, labeled `local-operator` in history. Bind only to loopback; this mode is not designed for public hosting. Missing/short tokens fail closed. SQLite persists across server restarts at the configured path. Back up that file if you want to retain local research.

## Working features

- Seven responsive sections: Overview, Day Trading, Swing Trading, Long-Term Investing, Strategy Lab, Control Center, Tax Center.
- Authenticated reads and commands; local token mode or Supabase registered-user auth. No public configuration data; the unauthenticated page is just a sign-in shell.
- Shared registry of three independently controlled portfolios and user-created strategies. Empty registry initially: existing relay strategies are not silently imported or approved.
- Append-only configuration snapshots, actor, database timestamp and reason. Optimistic concurrency rejects stale saves with HTTP 409; refresh and review before retrying. Latest 50 versions displayed; all remain stored.
- Per-portfolio manual/hybrid/automatic settings, regime selection, expiring overrides, strategy switches, global and portfolio pause-new-entry preferences.
- Advisory eligibility respects pauses, approval, switches and regimes. Hybrid uses the selected regime; automatic fails closed without a valid temporary override because there is no classifier. Expiry is evaluated on every state read; the UI refresh button updates it. These are saved preferences, not operational trade controls.
- Cursor's existing risk defaults displayed read-only. The dashboard cannot change them.
- Research progression: idea → specified → backtested → validated → paper; archive at any point. Advancement is sequential. Backtested needs recorded evidence, costs and trial count; validated/paper require 30 held-out trades. Paper promotion records the authenticated human actor. Only paper-approved strategies can be enabled. Research definitions are immutable: create a new idea to revise the hypothesis. Evidence is user-attested, not machine verified.
- Decimal federal carryforward scenario calculator, initialized to **$35,000, user-stated and unverified**. Negative inputs represent net losses. Ordinary-income deduction defaults off unless the user explicitly assumes sufficient income. The model uses $3,000/$1,500 limits and aggregates net gains/losses; it does not compute a tax return, tax liability, carryforward character, or a verified next-year figure.

## Explicitly not implemented

No broker data, balances, holdings, performance, orders, paper execution worker, live trading, worker acknowledgements, market feed, classifier, automated backtest runner, AI research jobs, specific-lot optimization, wash-sale engine, state-tax calculator or sale recommendations. No synthetic trading returns are displayed. All tax inputs beyond the unverified starting estimate are user-entered hypothetical scenarios.

The existing risk engine remains independent and unchanged. This dashboard does not yet call it for orders. A future worker must validate configuration and research evidence independently, fetch fresh broker data, obtain atomic risk approval, keep protective exits independent of entry restrictions, and acknowledge the precise saved version. `worker_acknowledged_version` is always null and `execution_active` is always false. Do not wire a broker to this control plane as-is.

## Supabase development persistence and authentication

No production database was accessed or modified. Use a dedicated **non-production** Supabase project/local stack with test accounts. Avoid reusing the relay's production environment.

1. Review and apply only `supabase/migrations/20261125042357_quant_dashboard_versions.sql` in that development database. It is self-contained apart from Supabase's `auth` schema. Do not apply the old relay migrations to obtain this dashboard.
2. Enable email/password authentication and create a registered test user. Anonymous sign-ins are rejected. No registration UI is provided here.
3. Set these dashboard-only environment variables:

```bash
export QE_AUTH_MODE=supabase
export QE_SUPABASE_URL='https://YOUR-DEVELOPMENT-PROJECT.supabase.co'
export QE_SUPABASE_PUBLISHABLE_KEY='YOUR-DEVELOPMENT-PUBLISHABLE-KEY'
python -m uvicorn quant_engine.dashboard.app:app --host 127.0.0.1 --port 8765
```

4. Sign in using the development user's email/password. Supabase handles password authentication and rate limits; each API call verifies the user's token with `/auth/v1/user`. Requests to PostgREST carry that user's JWT and the publishable key. No service-role key is used. Session refresh is deliberately not stored: sign in again after expiration.
5. Verify with two test users: the second must see an empty registry and must not see the first user's audit rows. Save concurrent versions from two sessions and confirm one gets a conflict.

`qe_versions` uses owner RLS and explicit SELECT/INSERT grants. Updates/deletes are not granted. A trigger rejects skipped/stale versions and assigns timestamps; the primary key resolves concurrent duplicate versions. The history is the state store, so an application save cannot omit its audit record. The table enforces PAPER, ownership, version sequence and size, but does not implement the full Python research policy. Direct authenticated database clients can submit their own configuration JSON. Therefore this is **not an execution authorization source**; a future worker must never trust saved JSON as approval to trade.

SQLite is single-host local development only. Supabase is the serverless persistence adapter. Hosted Supabase integration is not end-to-end verified in this milestone; REST interactions have mock tests and the actual migration ran in embedded PostgreSQL with representative roles/auth functions.

## Vercel separation

Nothing was deployed and no Vercel settings were changed. Do not change the existing project's entrypoint to test this UI. The current root `app.py` still loads the production relay.

For a later isolated development preview, prepare a separate Vercel project or deployment directory whose `app.py` exports `quant_engine.dashboard.app.app`, copies `quant_engine/` including static assets, and includes `requirements.txt`. Use the FastAPI framework, **no relay cron**, only the three `QE_*` Supabase settings above, and a non-production Supabase database. Local auth/storage deliberately refuses to start when `VERCEL` is set. This preview setup has not been deployed or verified; production activation is outside this milestone.

## Verification

```bash
python -m pytest -q tests research/tests quant_engine/tests
node --check quant_engine/dashboard/static/app.js
npm ci --prefix quant_engine/tests/browser
node quant_engine/tests/browser/migration.mjs
```

Browser regression runner (optional; start a fresh local test database, never point this at user data):

```bash
QE_LOCAL_TOKEN=local-test-token-not-a-secret-123456789 \
QE_SQLITE_PATH=var/browser-regression.sqlite3 \
python -m uvicorn quant_engine.dashboard.app:app --host 127.0.0.1 --port 8765
# In a second terminal, from the repository root:
npx --prefix quant_engine/tests/browser playwright install chromium
node quant_engine/tests/browser/dashboard.mjs
```

The browser runner expects an empty database and a local test-only token. Use a new filename for each run. Its screenshot defaults to `/private/tmp/qe-control.png`; override `QE_SCREENSHOT` on other systems.

Results in this implementation environment:

- 319 Python tests passed (300 existing + 19 dashboard cases), Python 3.15; the existing Starlette/httpx deprecation warning remains. Python 3.12 was not installed locally.
- JavaScript syntax check passed.
- Actual migration executed successfully in PGlite PostgreSQL: own-row access, cross-user denial, anonymous denial, append-only grants, sequential versions, stale conflict and PAPER constraints passed. This is not a hosted Supabase integration test.
- In-app browser verified sign-in, all seven sections, portfolio settings saving without changing the swing portfolio, strategy creation and stage advance, tax scenario saving/calculation, audit history and persistence after refresh. Browser console showed no errors during those checks.
- Standalone Playwright end-to-end runner is supplied but could not execute: macOS denied Chromium's process startup under this session's permissions. Do not count that runner as passing.

## Sources and assumptions

Federal planning reference: https://www.irs.gov/taxtopics/tc409 (checked November 24, 2026). This does not verify the user's carryforward. Carryforward character, taxable income and IRS worksheets matter; state rules and special adjustments are excluded.

Supabase references checked: https://supabase.com/changelog.md, https://supabase.com/docs/guides/api/securing-your-api, https://supabase.com/docs/reference/javascript/auth-getuser and https://supabase.com/docs/guides/database/functions. The implementation uses existing `httpx`, not an additional Supabase client dependency.
