# public-relay architecture review (Quant Engine V1, milestone 1)

Reviewed at `main` commit `5922da0` ("Route live Public.com orders to PremarketES only"). Line numbers refer to `api/index.py` at that commit.

## What exists today

```
TradingView strategy alert ──POST /webhook──▶ Vercel (iad1) FastAPI app: app.py → api/index.py
                                              │
                                              ├─ Settings.from_env      env vars read once at process start
                                              ├─ Store (Supabase RPC)   idempotency, daily caps, positions, picks, levels
                                              ├─ PublicClient           chain, greeks, quotes, preflight, live orders
                                              ├─ AlpacaClient           paper orders only (host pinned to paper-api)
                                              └─ AlpacaData             IEX bars fallback for FlowVWAP session facts

Outside scheduler ──GET /flowvwap-monitor──▶ FlowVWAP underlying stop / target / 11:30 flatten
Vercel cron 13:00 UTC ──GET /keepalive──▶ Supabase ping + paper fill reconcile
Operator ──POST /levels──▶ UW flip / King / Daybreak bias for the day
Operator ──POST /preflight──▶ read-only contract pick + Public preflight
```

| Component | Where | Role |
| --- | --- | --- |
| Relay | `api/index.py` (3,732 lines), `api/flowvwap.py` | Config, validation, contract selection, exit planning, broker clients, routing, HTTP endpoints. One module. |
| State | `supabase/migrations/0001`–`0006` | `processed_alerts` (idempotency), `daily_counters` (orders, open notional, exit orders per day and mode), `open_positions` (one row per underlying + strategy + mode), `contract_picks`, `daily_levels`. Caps are atomic SQL functions (`relay_try_consume`, `relay_release`). |
| Modes | `Settings.from_env` | `PREFLIGHT` (default, never places), `PAPER` (Alpaca paper host only), `LIVE` (needs `MODE=LIVE` and `LIVE_CONFIRM=I_UNDERSTAND`; only `PremarketES`, 1 contract, $2.50 premium ceiling, 1 entry and $250 per day). |
| Strategies | `BUILTIN_STRATEGY_OVERRIDES` | `FlowVWAP`, `QQQ_EMA_VWAP`, `PremarketES`, `PremarketES_MU`, plus any name on the global profile. |
| TradingView | `tradingview/qqq_ema_vwap_0dte.pine`, `sample_alert.json` | Pine strategy and alert templates. |
| Research | `research/` | Stdlib backtester that mirrors relay selection and exits; `research/results/summary.json`. |
| Deploy | `vercel.json`, `app.py` | FastAPI preset, region iad1, one weekday cron. |

### Existing risk controls

- Allowlist of underlyings; per-order `MAX_QTY`; `MAX_PREMIUM` on opens only.
- `MAX_ORDERS_PER_DAY` and `MAX_DAILY_OPENS` (dollars of premium) per ET day and mode, shared across strategies; `MAX_EXIT_ORDERS_PER_DAY` for take-profit, stop, and flatten orders.
- Market-hours window 09:31–15:55 ET; FlowVWAP 09:30–11:30; paper 0DTE entry cutoffs at 15:30 (SPY, QQQ) and 15:15 (others).
- Live ceiling is code, not env: one strategy, one contract, settled-cash check, preflight buying-power check, deterministic `orderId` per alert.
- Supabase unreachable fails closed (503).

These are spend and count controls. None of them measure loss.

## Execution and risk-management weaknesses

Severity is about money at risk on the paper and live books as they run today. "Verified" means it was reproduced against the relay with the existing test fakes during this review.

### High

**H1. Close alerts are rejected outside 09:31–15:55 ET (verified).** The hours check runs before the open/close decision for every strategy except FlowVWAP: `prepare_underlying` line 2044, the paper OCC path line 2901, and the non-paper OCC path line 3575. A `PremarketES` flat alert at 15:56 ET returns `422 outside trading window`. A 0DTE position whose close alert arrives late, or after 15:55, cannot be exited by alert. On paper Alpaca liquidates at 15:45; on the live Public.com book nothing else closes it.

**H2. The exit-order cap rejects flattens (verified).** Closes call `try_consume(..., is_exit=True)` (line 2990 paper, line 3592 non-paper), and `relay_try_consume` returns `MAX_EXIT_ORDERS_PER_DAY reached` once the count is used. With the cap at 1, the second flatten of the day (a different position) returned 422. The default of 6 is shared with take-profit and stop placements (3 per round trip with both), so the third flatten on a day with two bracketed round trips is refused.

**H3. `EXITS_COUNT_TOWARD_ORDER_CAP=true` lets the entry cap block exits.** The same call increments and checks `MAX_ORDERS_PER_DAY` for closes. `tests/test_index.py::test_exits_can_count_toward_the_order_cap` pins this: a close after the cap is full returns 422. Default is off.

**H4. No loss-based limits or equity-relative sizing.** There is no realized or unrealized P&L tracking, no daily or weekly loss stop, and no per-trade risk budget. Paper sizes are fixed (5 contracts for FlowVWAP, PremarketES, and PremarketES_MU) regardless of account equity. FlowVWAP has no premium cap (`max_premium: None`), so its only dollar bound is the shared `MAX_DAILY_OPENS`.

**H5. No runtime kill switch.** Settings are read once at start. Halting entries means editing Vercel env and redeploying. There is no endpoint, flag, or table that stops new entries while leaving exits working.

### Medium

**M1. `validate()` applies `MAX_QTY` to closes (line 989).** A close larger than the current profile's `max_qty` is rejected. Underlying flattens are sized from the stored position, so this bites when `STRATEGY_OVERRIDES` lowers `max_qty` while a position is open, or on an OCC close.

**M2. A rejected alert id cannot be retried (line 3506).** `relay_reserve` runs first and is never released, so a close that failed on a transient 502 returns `200 duplicate` when resent with the same `alert_id`. TradingView ids include `{{timenow}}`, so a manual resend has to use a new id.

**M3. Live protective exits are not at the broker.** The live take-profit and stop are preflighted and reported, not submitted. `PremarketES` depends entirely on its TradingView close alert, and the FlowVWAP monitor skips it. Combined with H1, a missed or late alert leaves the live contract open into expiry.

**M4. The FlowVWAP monitor cannot exit without an option quote (line 3272).** PAPER and LIVE return an error and place nothing; the next minute retries. The every-minute scheduler is external and not in the repo.

**M5. Paper fills that miss the in-request poll wait for `/keepalive`.** A paper entry still working after ~2.5 s is stored `PENDING` without exits. The only cron is 13:00 UTC, before the open, so same-day exits for that position rely on a manual keepalive or the next alert.

**M6. Exchange holidays and half days are not detected (line 995).** On a 13:00 close the 15:55 window is wrong for entries and exits.

**M7. No broker position reconciliation.** Positions are the relay's own rows. A LIVE transport error during `place()` (line 3724) keeps the caps consumed (safe) but records no position, so the close alert then fails with `no open position`. PREFLIGHT records positions as if filled.

### Low

**L1.** Audit writes to `processed_alerts` swallow failures (line 815). The decision stands; only the record is lost.

**L2.** One 3.7k-line module mixes config, clients, sizing, state, and routing, so risk logic cannot be tested without the HTTP stack and fakes. This is why the new engine is a separate package.

**L3.** Option tick rounding in PREFLIGHT and LIVE uses the $3 rule because the penny-program flag is only known on Alpaca.

## What milestone 1 changes

Nothing in `api/`, `supabase/`, `tradingview/`, `vercel.json`, or the env templates. The relay behaves exactly as before. Milestone 1 adds `quant_engine/risk`, a standalone engine that addresses H4 and H5 and is built so it cannot reproduce H1–H3 (see `docs/RISK_ENGINE.md`). Fixing H1–H3 inside the relay is the first integration task in `docs/MILESTONE_1.md`, because it changes live-book behavior and needs a separate review.
