# public-relay on Vercel + Supabase (port, not deployed)
- `api/index.py` FastAPI app (all routes rewritten to it by `vercel.json`; region iad1, maxDuration 30s).
- State in Supabase. Apply the migrations in order before deploying this version:
  `0001_relay_state.sql` (processed_alerts, daily_counters), `0002_positions_and_picks.sql`
  (open_positions, contract_picks), then `0003_take_profit_and_exit_caps.sql` (take-profit columns,
  exit-order cap), then `0004_flatten_and_stop.sql` (filled quantities and the optional stop).
  `0003` replaces `relay_try_consume` / `relay_release`, which every alert calls, so OCC alerts need it too.
  `0004` replaces `relay_get_position`, `relay_record_open`, and `relay_record_close`.
  `0005_paper_orders.sql` adds the Alpaca entry columns and replaces `relay_record_open` again, and adds
  `relay_list_open_positions` and `relay_save_paper`. Apply it before `MODE=PAPER`. Fails closed (503)
  if Supabase is unreachable.
- An alert can send a full OCC symbol, unchanged, or just an underlying (`SPY`). BUY selects a call and
  SELL a put. Defaults: expiry is today (0DTE) in America/New_York, strikes from ATM through 5 points ITM,
  `|delta|` closest to 0.50–0.60 (then to the middle of that band, then to the underlying price). The open
  limit is the bid/ask mid, or the ask if the mid is missing. A flatten, CLOSE, reversal, or opposite-side
  alert cancels a resting take-profit and stop, then sells every contract still open (runners and any
  take-profit contracts that have not filled). It does not open the other side on the same alert.
  `underlying_price` (`{{close}}`) sets ATM; if it is missing, the relay uses the equity quote.
  The pick is written to `contract_picks` and returned as `selection`, with the order that would have been
  sent as `order`. Templates are in `sample_alert.json`. Defaults stay MODE=PREFLIGHT.
- After an open is accepted, a partial take-profit is preflighted and reported as `take_profit`. When
  `STOP_LOSS_PERCENT` is set, a stop for the whole position is preflighted and reported as `stop`. Neither
  is sent to `/order`, including when an entry itself is live. See the settings table.
- Public token cached per warm instance, refreshed 2 min before expiry or on 401.
- Tests: `pip install -r requirements.txt -r requirements-dev.txt && pytest -q tests` (Public + Supabase mocked).
- Endpoints: `GET /health` (includes the non-secret `settings` block), `POST /webhook`.

Tunables are read from the environment when the instance starts. Changing one needs a restart (or a new
Vercel deployment that already has the variable). This repo does not set them. `MODE` and `LIVE_CONFIRM`
are unchanged: LIVE still requires both `MODE=LIVE` and `LIVE_CONFIRM=I_UNDERSTAND`.

## Settings

| Variable | Default | What it does |
| --- | --- | --- |
| `PUBLIC_SECRET` | empty | Public.com API secret. Required to call Public. |
| `WEBHOOK_PASSPHRASE` | empty | Shared secret in the TradingView alert. Required. |
| `MODE` | `PREFLIGHT` | `PREFLIGHT` never places an order. `LIVE` is ignored unless `LIVE_CONFIRM` matches. `PAPER` places on Alpaca paper and does not read `LIVE_CONFIRM`. |
| `LIVE_CONFIRM` | empty | Must be exactly `I_UNDERSTAND` together with `MODE=LIVE`. |
| `PUBLIC_ACCOUNT_ID` | empty | Account to trade. Blank picks the first account that reports an options level. |
| `ALLOWLIST` | empty | Comma-separated underlyings. Empty allows nothing. |
| `MAX_QTY` | `1` | Max contracts on one order (1–100). Partial take-profit needs this at least 2, unless `TP_SINGLE` is on. |
| `MAX_PREMIUM` | `2.00` | Max limit price on an entry or a flatten. The take-profit limit and the stop price are outside this check. |
| `MAX_ORDERS_PER_DAY` | `3` | Entry orders per ET day, counted separately for PREFLIGHT, PAPER, and LIVE. |
| `MAX_DAILY_OPENS` | `300` | Max dollars of premium on OPEN orders per ET day. The entry spends `limit × full qty × 100`. |
| `MAX_EXIT_ORDERS_PER_DAY` | `6` | Take-profit, stop, and flatten orders per ET day. A round trip that attaches both exits uses 3. The default fits two of those. |
| `EXITS_COUNT_TOWARD_ORDER_CAP` | `false` | When `true`, a take-profit, stop, or flatten also consumes `MAX_ORDERS_PER_DAY`. |
| `ENFORCE_MARKET_HOURS` | `true` | Reject outside Mon–Fri 09:31–15:55 ET. Holidays are not detected. |
| `DELTA_BAND_MIN` | `0.50` | Low end of the absolute-delta band (0–1, and `min ≤ max`). |
| `DELTA_BAND_MAX` | `0.60` | High end of that band. |
| `ITM_POINTS` | `5` | How far in the money strikes may be (0–50). 0 is ATM only. |
| `EXPIRY_DAYS` | `0` | Calendar days after today ET (0–30). `0` is 0DTE. The chain must list that date. |
| `OPEN_LIMIT_FALLBACK` | `ask` | When bid/ask mid is missing on an open: `ask`, `bid`, or `reject`. |
| `CLOSE_LIMIT_FALLBACK` | `bid` | Same choice for a flatten. The sell uses the bid/ask mid, then this fallback. |
| `TP_PERCENT` | `20` | Take-profit limit is entry × (1 + this / 100). `20` is 20% above the entry. |
| `TP_FRACTION` | `0.50` | Share of contracts to sell at that limit. Rounded down, and at least 1 when quantity is 2 or more. |
| `TP_SINGLE` | `false` | When `true`, a 1-contract open also gets a take-profit. A later flatten cancels it and sells the contract if it is still open. |
| `STOP_LOSS_PERCENT` | empty | Off when empty. When set, stop price is entry × (1 − this / 100), then rounded to the option tick. `30` on a 1.45 entry is 1.015, which is sent as 1.02. Must be greater than 0 and less than 100. |
| `SUPABASE_URL` | empty | Project URL. |
| `SUPABASE_SERVICE_KEY` | empty | Service-role key. Server only. |
| `CRON_SECRET` | empty | Bearer token for `GET /keepalive`. |

`/health` returns every tunable above except the secrets.

## Take-profit, stop, and flatten

For an open of 2 or more contracts, the relay reports a sell-to-close of `floor(qty × TP_FRACTION)` contracts (at least 1) at the take-profit limit and leaves the rest as runners. `qty_open` on that report is the full size still open. `runners` is what would remain if the take-profit later fills.

A flatten cancels any resting take-profit and then sells every contract still open, at the bid/ask mid (or `CLOSE_LIMIT_FALLBACK`). Unfilled take-profit contracts are included in that sell. If the take-profit already covers the whole position and is still resting, the flatten cancels it and sells the whole position. If the take-profit has filled, or filled in part, the sell is only what remains. Fills are clamped so they cannot add up to more than the opened quantity.

`STOP_LOSS_PERCENT` is empty by default, which leaves the stop off. When it is set, the relay reports one `STOP_LIMIT` sell-to-close for every open contract. `stopPrice` and `limitPrice` are both the stop, rounded to a valid option tick: $0.01 under $3, and $0.05 at or above $3. 1.45 with 30 percent is 1.015, so the order price is `1.02`. The same helper prices the take-profit. Public.com's single-leg preflight accepts `STOP` and `STOP_LIMIT`. This uses `STOP_LIMIT` so the protective sell has a price. If price gaps through the limit, that stop can sit unfilled, and the flatten is what closes the contracts. The relay does not switch the entry to a bracket, OCO, or OTO order. PREFLIGHT and LIVE do not know whether a contract is in the penny program, so they use the $3 rule. PAPER uses Alpaca's `ppind` flag and keeps $0.01 above $3 when that flag is true.

The reported stop covers the whole position, so it overlaps the take-profit until a fill. When the take-profit has filled, a later flatten reports `working_quantity` on `cancel_stop`: the stop is reduced to the runners, then cancelled because the flatten is closing them. When the stop has filled the position, the flatten sells nothing and cancels the resting take-profit. PREFLIGHT reports these orders and these cancels. It does not send them.

Exits stay off `MAX_ORDERS_PER_DAY` unless `EXITS_COUNT_TOWARD_ORDER_CAP=true`. Each take-profit, stop, and flatten increments `exit_orders`, capped by `MAX_EXIT_ORDERS_PER_DAY` (default 6). If the exit cap or a take-profit preflight fails, the entry still stands and that take-profit is not stored. A stop that fails the same way is left off, and the entry and any attached take-profit stay.

## Before live exits

On the live path the relay places the order it is considering. Today that is the entry, and a flatten sell when contracts are still open. The take-profit and the stop are preflighted and stored. Cancels are reported. The take-profit, the stop, and those cancels are not submitted.

Before `MODE=LIVE` can send the protective exits:

1. Place the reported take-profit limit after the entry fills, or send the entry as a bracket.
2. Place the reported `STOP_LIMIT`.
3. When the take-profit fills, replace the stop down to the runner quantity. Until that replace, the two orders overlap, and sending both sizes would oversell.
4. When the stop fills, cancel the take-profit.
5. On a flatten, cancel the resting take-profit and the resting stop, then sell the reconciled quantity. The existing live `place()` path already submits that sell.
6. In LIVE, a flatten reads `GET /userapigateway/trading/{accountId}/order/{orderId}` for a resting, partial, or filled take-profit and stop. A 404 means that id was never accepted, and the stored fill count is kept. Any other failure rejects the flatten instead of guessing a quantity. PREFLIGHT does not call that endpoint.
7. Confirm the account accepts an option `STOP_LIMIT` and the contract's price increment. The reported stop is already on the $0.01 / $0.05 tick.
8. Leave `MODE` at `PREFLIGHT` until those exits exist. `LIVE_CONFIRM` stays unset in this repo. `MODE=PAPER` is the path that actually places the take-profit, the stop, and the cancels, on Alpaca's paper host only.

## Paper trading on Alpaca

`MODE=PAPER` runs the same Public.com selection, then places the order on Alpaca paper so fills can be rehearsed. It does not require `LIVE_CONFIRM`. `PREFLIGHT` and `LIVE` do not read the Alpaca variables. This repo does not set `MODE` or `LIVE_CONFIRM` in Vercel.

Nick's setup:

1. Create an Alpaca account and generate a paper API key. Do not use a live trading key.
2. Apply `supabase/migrations/0005_paper_orders.sql` after `0004`.
3. In the Vercel project, add `ALPACA_KEY_ID`, `ALPACA_SECRET_KEY`, and optionally `ALPACA_BASE_URL`. The URL defaults to `https://paper-api.alpaca.markets` and the process refuses to start if it is any other host.
4. Set `MODE=PAPER`. Leave `LIVE_CONFIRM` unset. Redeploy so the new instance reads the variables.

What PAPER does:

- Selection stays on the Public.com chain and greeks. The chosen OCC symbol is sent to Alpaca without space padding, then checked with `GET /v2/options/contracts/{symbol}`. A missing or inactive contract is a 422 and no order is sent.
- The entry is a buy-to-open limit at the mid (or the ask). On a fill, the relay sells `floor(qty × TP_FRACTION)` at the take-profit and, when `STOP_LOSS_PERCENT` is set, a stop-limit for the filled quantity. Prices use the tick above.
- The request polls Alpaca about five times, half a second apart (about 2.5s). If the entry is still working, the position is stored as `PENDING` with `exits_placed` false. `GET /keepalive` and the next alert refresh that fill. Keepalive places the take-profit and stop once the entry is filled. The weekday cron stays `0 13 * * 1-5` UTC, which is before the cash open, so a same-day exit that missed the in-request poll needs a manual keepalive. A later flatten sells whatever has filled and cancels a resting entry that never filled.
- A flatten cancels resting take-profit and stop orders, then sells the remaining contracts at the mid (or the bid). It does not place those exits and cancel them in the same alert.
- Positions use `open_positions` with `mode = 'PAPER'`. Daily counters are per mode. In PAPER, exits do not consume `MAX_ORDERS_PER_DAY` even when `EXITS_COUNT_TOWARD_ORDER_CAP=true`. They still use `MAX_EXIT_ORDERS_PER_DAY`.
- New 0DTE entries are rejected at 15:30 ET for SPY and QQQ, and at 15:15 ET for anything else. Alpaca auto-liquidates 0DTE options at 15:45 ET. This relay does not send that liquidation. Exits are not subject to the cutoff.
- `GET /health` reports `mode`, `alpaca_configured`, and `GET /v2/account` status. Equity is included when Alpaca sends it. Cash and buying power are not.
- Secrets are not logged. An OCC open is placed but not stored; the position lifecycle above is the underlying alert (the FlowVWAP template).

`LIVE` is unchanged: it still places only the entry, or a flatten sell, and only when `MODE=LIVE` and `LIVE_CONFIRM=I_UNDERSTAND`. The take-profit and the stop stay reported.

## TradingView alert message

Paste this into the strategy alert **Message** box. Replace only the passphrase placeholder. Leave `open_close` off so the relay infers open vs close from `market_position` / `prev_market_position`. `qty` comes from the strategy; partial take-profit stays idle while `MAX_QTY` is 1.

```json
{
  "passphrase": "REPLACE_WITH_WEBHOOK_PASSPHRASE",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": "{{strategy.order.contracts}}",
  "underlying_price": "{{close}}",
  "market_position": "{{strategy.market_position}}",
  "prev_market_position": "{{strategy.prev_market_position}}",
  "strategy": "FlowVWAP",
  "alert_id": "{{ticker}}-{{strategy.order.id}}-{{timenow}}"
}
```
