# public-relay on Vercel + Supabase (port, not deployed)
- `api/index.py` FastAPI app (all routes rewritten to it by `vercel.json`; region iad1, maxDuration 30s).
- State in Supabase. Apply the migrations in order before deploying this version:
  `0001_relay_state.sql` (processed_alerts, daily_counters), `0002_positions_and_picks.sql`
  (open_positions, contract_picks), then `0003_take_profit_and_exit_caps.sql` (runner quantity,
  take-profit columns, exit-order cap). `0003` replaces `relay_try_consume` / `relay_release`,
  which every alert calls, so OCC alerts need it too. Fails closed (503) if Supabase is unreachable.
- An alert can send a full OCC symbol, unchanged, or just an underlying (`SPY`). BUY selects a call and
  SELL a put. Defaults: expiry is today (0DTE) in America/New_York, strikes from ATM through 5 points ITM,
  `|delta|` closest to 0.50–0.60 (then to the middle of that band, then to the underlying price). The open
  limit is the bid/ask mid, or the ask if the mid is missing. A flatten, CLOSE, reversal, or opposite-side
  alert sells the **remaining runners** and cancels a resting take-profit. It does not open the other side
  on the same alert. `underlying_price` (`{{close}}`) sets ATM; if it is missing, the relay uses the equity quote.
  The pick is written to `contract_picks` and returned as `selection`, with the order that would have been
  sent as `order`. Templates are in `sample_alert.json`. Defaults stay MODE=PREFLIGHT.
- After an open is accepted, a partial take-profit is preflighted and reported as `take_profit`. It is not
  sent to `/order`, including when an entry itself is live. See the settings table for the fraction and price.
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
| `MODE` | `PREFLIGHT` | `PREFLIGHT` never places an order. `LIVE` is ignored unless `LIVE_CONFIRM` matches. |
| `LIVE_CONFIRM` | empty | Must be exactly `I_UNDERSTAND` together with `MODE=LIVE`. |
| `PUBLIC_ACCOUNT_ID` | empty | Account to trade. Blank picks the first account that reports an options level. |
| `ALLOWLIST` | empty | Comma-separated underlyings. Empty allows nothing. |
| `MAX_QTY` | `1` | Max contracts on one order (1–100). Partial take-profit needs this at least 2, unless `TP_SINGLE` is on. |
| `MAX_PREMIUM` | `2.00` | Max limit price on an entry or a runner close. The take-profit limit is not checked against this. |
| `MAX_ORDERS_PER_DAY` | `3` | Entry orders per ET day, counted separately for PREFLIGHT and LIVE. |
| `MAX_DAILY_OPENS` | `300` | Max dollars of premium on OPEN orders per ET day. The entry spends `limit × full qty × 100`. |
| `MAX_EXIT_ORDERS_PER_DAY` | `6` | Take-profits plus runner closes per ET day. Default is one take-profit and one runner exit for each of 3 entries. |
| `EXITS_COUNT_TOWARD_ORDER_CAP` | `false` | When `true`, a take-profit or runner close also consumes `MAX_ORDERS_PER_DAY`. |
| `ENFORCE_MARKET_HOURS` | `true` | Reject outside Mon–Fri 09:31–15:55 ET. Holidays are not detected. |
| `DELTA_BAND_MIN` | `0.50` | Low end of the absolute-delta band (0–1, and `min ≤ max`). |
| `DELTA_BAND_MAX` | `0.60` | High end of that band. |
| `ITM_POINTS` | `5` | How far in the money strikes may be (0–50). 0 is ATM only. |
| `EXPIRY_DAYS` | `0` | Calendar days after today ET (0–30). `0` is 0DTE. The chain must list that date. |
| `OPEN_LIMIT_FALLBACK` | `ask` | When bid/ask mid is missing on an open: `ask`, `bid`, or `reject`. |
| `CLOSE_LIMIT_FALLBACK` | `bid` | Same choice for a runner close. |
| `TP_PERCENT` | `20` | Take-profit limit is entry × (1 + this / 100). `20` is 20% above the entry. |
| `TP_FRACTION` | `0.50` | Share of contracts to sell at that limit. Rounded down, and at least 1 when quantity is 2 or more. |
| `TP_SINGLE` | `false` | When `true`, a 1-contract open also gets a take-profit. Otherwise that contract waits for the flatten. |
| `SUPABASE_URL` | empty | Project URL. |
| `SUPABASE_SERVICE_KEY` | empty | Service-role key. Server only. |
| `CRON_SECRET` | empty | Bearer token for `GET /keepalive`. |

`/health` returns every tunable above except the secrets.

## Take-profit and the daily caps

For an open of 2 or more contracts, the relay sells `floor(qty × TP_FRACTION)` contracts (at least 1) at the take-profit limit and leaves the rest as runners. A later flatten sells only `qty_open` (the runners) and reports `cancel_take_profit` for a resting take-profit. Contracts in that take-profit are **not** sold by the flatten. If the take-profit already covers every contract (`TP_SINGLE`, or a fraction that rounds to the full size), the flatten only cancels and does not preflight a sell. PREFLIGHT never marks the take-profit filled, so a flatten in preflight always finds it resting.

Exits do **not** count against `MAX_ORDERS_PER_DAY`. That cap stays an entry cap, so a round trip no longer uses two of the three daily orders. Each take-profit and each runner close increments `exit_orders`, capped by `MAX_EXIT_ORDERS_PER_DAY` (default 6). Set `EXITS_COUNT_TOWARD_ORDER_CAP=true` to restore the old rule where closes also use the entry cap. If the exit cap or the take-profit preflight fails, the entry still stands and no take-profit is stored.

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
