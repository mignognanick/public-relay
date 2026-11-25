# Quant Engine V1 — milestone 1

Branch: `dev/quant-engine`, off `main` at `5922da0`. Not merged into `main`.

## Scope delivered

| Requirement | Where |
| --- | --- |
| Document the existing architecture; identify execution and risk weaknesses | `docs/ARCHITECTURE.md` |
| Project objectives, research rules, risk constraints, development procedures | `AGENTS.md` |
| Standalone portfolio risk engine | `quant_engine/risk/` (`engine.py`, `config.py`, `models.py`, `sizing.py`, `state.py`, `cli.py`) |
| Configurable sizing, daily and weekly loss thresholds, aggregate exposure limits, persistent kill switch | `RiskConfig`, `RiskEngine`, `FileStateStore`; design in `docs/RISK_ENGINE.md` |
| Protective exits never blocked by entry restrictions | `RiskEngine.evaluate_exit`; tests under "protective exits" and the concurrency race test |
| Tests: normal operation, invalid data, concurrent requests, limit breaches | `quant_engine/tests/` (176 tests) |
| Run existing and new tests | Results below |

Defaults are the requested paper values: 0.25% planned risk per trade, 0.75% daily loss, 2% weekly loss, 0.50% aggregate planned open risk. A 100% gross-notional cap was added as a no-leverage backstop; it does not bind for option entries at these risk levels.

## What did not change

- `api/`, `app.py`, `supabase/`, `tradingview/`, `research/`, `vercel.json`, `.env.example`, and `requirements*.txt` are untouched. Relay behavior on PREFLIGHT, PAPER, and LIVE is identical to `main`.
- No live trading was enabled, no orders were placed (paper or live), no credentials were read, no environment variables were changed, nothing was deployed.
- `.gitignore` gains `var/`, the suggested location for a local risk state file.

## Design decisions to review

1. **Standalone, not wired in.** The relay serves a live PremarketES book. Calling the engine from `/webhook` changes live behavior and needs a shared (Supabase) state store first, so integration is the next milestone.
2. **Worst-case loss headroom.** A new entry must fit in `loss limit − current loss − open planned risk`, for both the day and the week. This is stricter than checking current loss alone, and it counts a losing open position twice (in equity and in planned risk).
3. **Higher baseline wins.** Period start equity is the higher of the broker figure and the first equity the engine saw, so a loss is never understated. A deposit mid-period is not detected (see open issues).
4. **Weekly breach engages the kill switch.** The daily lockout clears itself the next New York day. The weekly breach also turns on the kill switch, which only a named person can turn off. `RISK_KILL_SWITCH_ON_WEEKLY_BREACH=false` makes the week clear itself on Monday instead.
5. **Exits fail open; entries fail closed.** An unreadable state rejects entries and approves exits. An exit for a position the engine does not track is approved.
6. **LIVE entries are always refused** by the engine, with no setting to change it.

## Test results

Run on Python 3.12.3, fastapi 0.146.0, httpx 0.28.1, pytest 9.1.1:

```
python -m pytest -q tests research/tests quant_engine/tests
300 passed, 1 warning
```

| Suite | Tests | Result |
| --- | --- | --- |
| `tests/` (existing relay) | 113 | pass, unchanged from `main` |
| `research/tests/` (existing backtester) | 11 | pass |
| `quant_engine/tests/test_risk_config.py` | 20 | pass |
| `quant_engine/tests/test_risk_sizing.py` | 15 | pass |
| `quant_engine/tests/test_risk_engine.py` | 131 | pass |
| `quant_engine/tests/test_risk_concurrency.py` | 7 | pass (15 of 15 repeated runs) |
| `quant_engine/tests/test_risk_cli.py` | 3 | pass |

The warning is Starlette's existing `httpx` deprecation notice from `fastapi.testclient`, present on `main`.

Static checks on the new package: `mypy --strict quant_engine/risk` reports no issues; `pyflakes quant_engine` is clean.

What the new tests cover:

- **Normal operation:** sizing by premium and by stop distance, requested quantity only lowering size, webhook-style string inputs, idempotent duplicates, fill confirmation (full, partial, zero, slippage, fill through the stop), release, exit fills freeing headroom.
- **Invalid data:** 40 bad entry fields (ids, names, sides, NaN, infinity, booleans, wrong-side stops, fractional and out-of-range quantities, multipliers, costs, modes), 11 bad snapshots (zero, negative, or NaN equity; naive, string, missing, or future timestamps; bad baselines), stale snapshots with an inclusive age limit, a short without a stop, 10 tampered state files, and malformed exits.
- **Concurrency:** 32 threads racing for 4 slots on the in-memory and file stores; 4 spawned processes sharing one state file; 20 concurrent duplicates reserving once; entries, exits, and a kill-switch trip racing together. A throwaway check that removed the file lock let the same 32-thread race approve 32 entries instead of 4, so these tests do detect over-spending.
- **Limit breaches:** per-trade budget below one unit, aggregate cap, aggregate and daily headroom shrinking size, daily breach latching through a recovery, the New York (not UTC) day boundary, the next-day reset, supplied versus observed baselines, weekly breach engaging the kill switch through the next week until a person clears it, the weekly reset without the auto kill switch, gross exposure, and every blocking limit being named.
- **Protective exits:** approved under the kill switch, the daily lockout, the weekly lockout, full aggregate risk, a corrupt in-memory state, and a corrupt state file. Untracked positions are approved, over-sized exits are clamped, and evaluating an exit leaves state unchanged.
- **Persistence:** kill switch, positions, and lockouts survive a restart; a failed write keeps the previous file and leaves no temp files or partial reservation.

## Unresolved issues

Relay (from `docs/ARCHITECTURE.md`, not changed in this milestone):

- **H1** Close alerts outside 09:31–15:55 ET are rejected for every strategy but FlowVWAP. Reproduced: a PremarketES close at 15:56 ET returns 422.
- **H2** `MAX_EXIT_ORDERS_PER_DAY` rejects flattens. Reproduced with the cap at 1: the second flatten returns 422.
- **H3** `EXITS_COUNT_TOWARD_ORDER_CAP=true` lets the entry order cap reject closes (pinned by an existing test).
- **M1–M7** `MAX_QTY` on closes, non-retryable rejected alert ids, live protective exits not at the broker, monitor exits need a quote, paper exits waiting for keepalive, no holiday calendar, no broker position reconciliation.

Risk engine:

- No shared state for serverless. `FileStateStore` is single-host. Wiring into Vercel needs a Supabase store (one SQL function per transaction with a row lock).
- Deposits and withdrawals are not distinguished from P&L. A mid-period deposit hides a loss; a withdrawal looks like one. Needs a cash-flow feed or manual baseline reset.
- Planned risk assumes the stop fills at its price. 0DTE gaps and STOP_LIMIT orders that do not fill can lose more; `cost_per_unit` is the only slippage allowance.
- Reservations do not expire. A crashed caller leaves a PENDING reservation that blocks headroom until `release_entry`.
- No per-strategy or per-symbol sub-limits, correlation grouping, or max open positions count.
- Exit approvals for untracked positions rely on the broker to refuse an oversell.

## Recommended next steps

1. **Fix H1–H3 in the relay behind tests.** Skip the hours window, the exit-order cap, and `MAX_QTY` for closes, and keep `EXITS_COUNT_TOWARD_ORDER_CAP` from refusing an exit. This touches the live close path, so it needs explicit approval. Rewrite `test_exits_can_count_toward_the_order_cap` to assert the close is accepted.
2. **Supabase risk store.** Add migration `0007_risk_engine.sql` (risk state table plus `risk_try_reserve` / `risk_release` / `risk_kill_switch` functions that lock a row) and a `SupabaseStateStore`. Port the concurrency tests to it with the fake PostgREST transport.
3. **Wire the engine into PAPER only.** On `/webhook` in `MODE=PAPER`, call `evaluate_entry` with Alpaca `/v2/account` equity and `last_equity`, size the order from the decision, and call `confirm_entry_fill`, `release_entry`, and `record_exit_fill` from the existing fill paths. Route every close through `evaluate_exit` without letting it block. Keep PREFLIGHT and LIVE untouched.
4. **Kill switch endpoint.** `POST /risk/kill-switch` and `GET /risk/status` behind `CRON_SECRET`, plus a `/flowvwap-monitor`-style job that flattens on kill.
5. **Decide paper sizing.** At 0.25% of a $100k account a stopless 0DTE contract sizes to 1–2, not 5. Either give strategies a stop price so risk per contract is smaller, or approve different limits.
6. **Research alignment.** Size `research/backtest.py` trades with `size_position` and report results in percent of equity against the daily and weekly thresholds.
7. **Market calendar.** Add NYSE holidays and half days for both entry windows and the engine's period boundaries.
