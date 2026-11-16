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
- An alert can send a full OCC symbol, unchanged, or just an underlying (`SPY`, `QQQ`, or `MU`). BUY selects a call and
  SELL a put. Defaults: expiry is today (0DTE) in America/New_York, strikes from ATM through 5 points ITM,
  `|delta|` closest to 0.50–0.60 (then to the middle of that band, then to the underlying price). The open
  limit is the bid/ask mid, or the ask if the mid is missing. A flatten, CLOSE, reversal, or opposite-side
  alert cancels a resting take-profit and stop, then sells every contract still open (runners and any
  take-profit contracts that have not filled). It does not open the other side on the same alert.
  The position key is underlying + strategy + mode, so a QQQ flatten cannot close a SPY position.
  `QQQ_EMA_VWAP` keeps its own selection and exit defaults. `FlowVWAP` has its own profile
  (delta 0.50–0.60, 0.50 points ITM, 5 contracts, no premium cap, underlying exits).
  `PremarketES` has its own profile (delta 0.35–0.40, ATM through 5 points OTM, 5 contracts,
  no take-profit and no option stop). `PremarketES_MU` is the same shape for MU (delta
  0.125–0.15, ATM through 70 points OTM, 5 contracts, no take-profit and no option stop).
  A close alert flattens that strategy's position. Neither uses the FlowVWAP monitor.
  `0006_flowvwap_levels.sql` adds the entry underlying prices and `daily_levels`. Apply it
  after `0005` before this version serves FlowVWAP. QQQ opens still record if the new
  columns are left null.
  `0007_flowvwap_limit_and_notifications.sql` adds the resting-limit clock and the
  notification idempotency table. Apply it after `0006` before FlowVWAP limit orders.
  QQQ saves omit the new arguments, so they still record on a database that is only
  through `0006`.
  `underlying_price` (`{{close}}`) sets ATM; if it is missing, the relay uses the equity quote.
  The pick is written to `contract_picks` and returned as `selection`, with the order that would have been
  sent as `order`. Templates are in `sample_alert.json`. Defaults stay MODE=PREFLIGHT.
- After an open is accepted, a partial take-profit is preflighted and reported as `take_profit`. When
  `STOP_LOSS_PERCENT` is set, a stop for the whole position is preflighted and reported as `stop`. Neither
  is sent to `/order`, including when an entry itself is live. See the settings table.
- Public token cached per warm instance, refreshed 2 min before expiry or on 401.
- Tests: `pip install -r requirements.txt -r requirements-dev.txt && pytest -q tests` (Public + Supabase mocked).
- Endpoints: `GET /health` (includes the non-secret `settings` block), `POST /webhook`, `POST /preflight`.

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
| `ALLOWLIST` | empty | Comma-separated underlyings. Empty allows nothing else. `QQQ` is always added, so this does not need an edit for `QQQ_EMA_VWAP`. `PremarketES_MU` accepts `MU` without an edit; other strategies do not. `SPY` stays on this list (production already has it; do not remove it). |
| `MAX_QTY` | `1` | Max contracts on one order (1–100). Partial take-profit needs this at least 2, unless `TP_SINGLE` is on. Overridable per strategy. |
| `MAX_PREMIUM` | `5.00` | Max limit price on an entry. A close or flatten is never blocked by this cap, for any strategy. The take-profit limit and the stop price are also outside this check. Overridable per strategy. |
| `STRATEGY_OVERRIDES` | empty | Optional JSON object keyed by the alert's `strategy` field. Merges on top of the checked-in profiles. Leave unset to use the shipped profiles. A `FlowVWAP` key, if set, replaces those keys. The JSON Nick would apply is in the FlowVWAP section. This repo does not set it. |
| `MAX_ORDERS_PER_DAY` | `3` | Entry orders per ET day, counted separately for PREFLIGHT, PAPER, and LIVE. Shared across strategies. |
| `MAX_DAILY_OPENS` | `1500` | Max dollars of premium on OPEN orders per ET day. The entry spends `limit × full qty × 100`. Shared across strategies. Production sets `1000` explicitly; leave that value as it is. |
| `MAX_EXIT_ORDERS_PER_DAY` | `6` | Take-profit, stop, and flatten orders per ET day. A round trip that attaches both exits uses 3. The default fits two of those. |
| `EXITS_COUNT_TOWARD_ORDER_CAP` | `false` | When `true`, a take-profit, stop, or flatten also consumes `MAX_ORDERS_PER_DAY`. |
| `ENFORCE_MARKET_HOURS` | `true` | Reject outside Mon–Fri 09:31–15:55 ET. Holidays are not detected. |
| `DELTA_BAND_MIN` | `0.50` | Low end of the absolute-delta band (0–1, and `min ≤ max`). Overridable per strategy. |
| `DELTA_BAND_MAX` | `0.60` | High end of that band. Overridable per strategy. |
| `ITM_POINTS` | `5` | How far in the money strikes may be (0–50). 0 is ATM only. Overridable per strategy. |
| `EXPIRY_DAYS` | `0` | Calendar days after today ET (0–30). `0` is 0DTE. The chain must list that date. |
| `OPEN_LIMIT_FALLBACK` | `ask` | When bid/ask mid is missing on an open: `ask`, `bid`, or `reject`. |
| `CLOSE_LIMIT_FALLBACK` | `bid` | Same choice for a flatten. The sell uses the bid/ask mid, then this fallback. |
| `TP_PERCENT` | `20` | Take-profit limit is entry × (1 + this / 100). `20` is 20% above the entry. Overridable per strategy. |
| `TP_FRACTION` | `0.50` | Share of contracts to sell at that limit. Rounded down, and at least 1 when quantity is 2 or more. |
| `TP_SINGLE` | `false` | When `true`, a 1-contract open also gets a take-profit. A later flatten cancels it and sells the contract if it is still open. Overridable per strategy. |
| `STOP_LOSS_PERCENT` | empty | Off when empty. When set, stop price is entry × (1 − this / 100), then rounded to the option tick. `30` on a 1.45 entry is 1.015, which is sent as 1.02. Must be greater than 0 and less than 100. Overridable per strategy. `null` in a profile turns the stop off for that strategy even when this variable is set. |
| `SUPABASE_URL` | empty | Project URL. |
| `SUPABASE_SERVICE_KEY` | empty | Service-role key. Server only. |
| `CRON_SECRET` | empty | Bearer token for `GET /keepalive` and `/flowvwap-monitor`. |
| `NOTIFY_EMAIL_ENABLED` | `false` | Master switch for order emails. Off sends nothing. PAPER only. |
| `RESEND_API_KEY` | empty | Resend API key. Read when email is on. Not logged and not put in `/health`. |
| `NOTIFY_EMAIL_FROM` | empty | From address Resend will accept. Not logged and not put in `/health`. |
| `NOTIFY_EMAIL_TO` | empty | Comma-separated recipients. Not logged and not put in `/health`. |

`/health` returns every tunable above except the secrets. `settings.strategies` is the effective per-strategy block (built-in profiles plus `STRATEGY_OVERRIDES`). A strategy that is not listed there uses the global `settings` values. `FlowVWAP`, `QQQ_EMA_VWAP`, `PremarketES`, and `PremarketES_MU` are listed. `settings.daily_caps` is `shared`.

## Per-strategy settings

Checked-in defaults live in `BUILTIN_STRATEGY_OVERRIDES` in `api/index.py`. That is the source that ships without an env edit. `STRATEGY_OVERRIDES` is optional JSON and replaces individual keys for a strategy; omitted keys keep the built-in value, then the global env. The strategy name must match the alert's `strategy` field exactly. No Vercel variable has to change for `QQQ_EMA_VWAP`.

Overridable keys: `delta_band_min`, `delta_band_max`, `itm_points`, `otm_points`, `max_premium`, `tp_percent`, `tp_single`, `take_profit`, `stop_loss_percent`, `max_qty`, `order_qty`, `flow_rules`, `entry_limit_offset`, `entry_limit_timeout_sec`, `notify_email`. `max_premium: null` removes that strategy's open cap. `otm_points` is how far out of the money a strike may be (0–80). `0` keeps the ATM-through-ITM window. `take_profit: false` skips the premium take-profit for that strategy, including a 5-lot, which `TP_FRACTION` cannot do from a profile. Daily caps, expiry, limit fallbacks, and mode are not overridable.

| Strategy | Delta | Strike window | Max premium (opens) | Exits | Qty |
| --- | --- | --- | --- | --- | --- |
| `FlowVWAP` | `0.50`–`0.60` | ATM through `0.50` ITM | none (`null`) | SPY underlying stop 0.50 / target +1.00, whole position. No premium take-profit or stop. | 5 (`order_qty`) |
| `QQQ_EMA_VWAP` | `0.60`–`0.75` | ATM through `5` ITM | `5.00` | `50%` take-profit, `TP_SINGLE` on, premium stop off | `1` |
| `PremarketES` | `0.35`–`0.40` | ATM through `5` OTM | global `MAX_PREMIUM` | Close alert flattens the whole position. No take-profit, no option stop. | 5 (`order_qty`) |
| `PremarketES_MU` | `0.125`–`0.15` | ATM through `70` OTM | global `MAX_PREMIUM` | Close alert flattens the whole position. No take-profit, no option stop. | 5 (`order_qty`) |
| any other name | global `0.50`–`0.60` | ATM through global `ITM_POINTS` | global `MAX_PREMIUM` | global premium take-profit and stop | global `MAX_QTY` |

Example, only if a shipped value needs a change (not required):

```json
{"QQQ_EMA_VWAP": {"max_premium": "6.00", "stop_loss_percent": null}}
```

Daily caps stay shared on `(day, mode)`, which is how `daily_counters` already works. `MAX_ORDERS_PER_DAY` (3) and `MAX_DAILY_OPENS` (code default 1500; production sets 1000) count FlowVWAP, QQQ, PremarketES, and PremarketES_MU together. A 5-contract open spends `limit × 5 × 100` of that same pool. Splitting the caps would raise the loss limit, so they stay one pool. Positions do not: each open row is `(underlying, strategy, mode)`.

## PremarketES (SPY 0DTE)

Checked-in defaults in `BUILTIN_STRATEGY_OVERRIDES`. A long alert buys a call and a short alert buys a put. Absolute delta is 0.35–0.40, from ATM through 5 points out of the money in the trade's direction. Strikes in the money are not candidates. That is the slightly OTM contract. FlowVWAP and `QQQ_EMA_VWAP` stay ATM through ITM; `otm_points` is 0 for both.

Quantity is 5 (`order_qty`). The alert's `qty` and the global `MAX_QTY` are not used. Opens still use the global `MAX_PREMIUM`. There is no premium take-profit (`take_profit: false`) and no relay-side option stop (`stop_loss_percent: null`), even when `STOP_LOSS_PERCENT` or `TP_PERCENT` is set for other strategies.

`flow_rules` is false. PremarketES does not check the regime, the flip, the King room, or the 09:30–11:30 window, and it has no +1.00 underlying target. `GET` or `POST /flowvwap-monitor` skips these positions. Entries use the existing weekday 09:31–15:55 ET webhook window. An alert at 09:30 is rejected. An alert at 11:45 is allowed.

The Pine script owns the $0.50 SPY stop and the 10:00 ET flatten. It sends a close alert (market position flat). The relay sells every contract still open for that `(underlying, strategy, mode)` row, the same way it flattens FlowVWAP and QQQ. A PremarketES close does not sell a FlowVWAP position, and the FlowVWAP monitor does not sell a PremarketES position.

No env edit and no migration. `SPY` is already on the allowlist. `MODE` stays wherever it already is. This profile places on Alpaca only when that mode is already `PAPER`.

The FlowVWAP monitor sells the whole position and does not consume `MAX_ORDERS_PER_DAY` or `MAX_EXIT_ORDERS_PER_DAY`, so a full exit cap cannot block the 11:30 flatten.

## PremarketES_MU (MU 0DTE)

Checked-in defaults in `BUILTIN_STRATEGY_OVERRIDES`. The alert `strategy` field is `PremarketES_MU` and `symbol` is `MU` (the chart ticker). A long alert buys a call and a short alert buys a put. Absolute delta is 0.125–0.15. The selector uses the same ranking as the other profiles: inside the band first, then closest to the middle of the band (0.1375), then closest to the underlying price. Quantity is 5. The alert's `qty` is ignored. Opens use the global `MAX_PREMIUM` (production is $4.00; this profile does not set its own). There is no premium take-profit and no option stop. `flow_rules` is false, so `/flowvwap-monitor` skips these positions. Unknown alert fields such as `pm_log` are ignored.

The strike window is ATM through 70 points out of the money. Strikes in the money are not candidates. That width is not seven $10 strikes. The relay does not hardcode a strike increment: it asks Public for the chain on today's expiry and keeps strikes whose price distance from the alert's `underlying_price` (or the equity quote) is inside the window. CBOE's listed MU chain for the 2026-11-16 Monday expiry, which is the same OCC series Public returns, is $5 apart from $750 through at least $860, $2.50 from about $482.50 through $750, and $10 only far away ($350–$480 and above about $1160). Seven $5 strikes is 35 points. On that chain, with MU near $761, the 0.15 call was the $820 strike (about $59 out) and the 0.13 call was $825. 70 points reaches those strikes. A 35-point window would stop near a 0.30 delta and the 0.125–0.15 band would never be a candidate.

Expiry is today in America/New_York (`EXPIRY_DAYS` stays 0). The chain request sends that date. Contracts whose OCC symbol is a later expiry are dropped, and a chain that only lists a later expiry is not relabeled as today. The alert is rejected and logged as `expiry_unavailable`. There is no search for the next expiry. MU lists Monday, Wednesday, and Friday expiries; a Tue/Thu alert has nothing to buy. If Public returns an error for that date, the relay still fails closed (`option chain failed`) and does not place an order.

A close alert (market position flat, the same TradingView order-fill shape as PremarketES) sells the open `PremarketES_MU` contracts for `(MU, PremarketES_MU, mode)`. It does not sell a SPY position. Entries use the weekday 09:31–15:55 ET webhook window. In `PAPER`, Alpaca's 0DTE cutoff for names other than SPY and QQQ is 15:15 ET, so an MU entry at or after 15:15 is rejected. SPY and QQQ stay at 15:30.

`MU` is added to the allowlist on this profile only, so `ALLOWLIST` does not need an edit and a different strategy cannot trade MU unless it is already listed. No migration. `MODE` stays wherever it already is. This profile places on Alpaca only when that mode is already `PAPER`.

OCC symbols are the usual root plus date, right, and an 8-digit strike. `MU` at $822.50 on 2026-11-16 is `MU261116C00822500`. A two-letter root fits the existing parser.

## FlowVWAP (SPY 0DTE)

Checked-in defaults in `BUILTIN_STRATEGY_OVERRIDES`. A long alert buys a call and a short alert buys a put, delta 0.50–0.60, from ATM through 0.50 points in the money. Quantity is 5. There is no open premium cap unless `STRATEGY_OVERRIDES` sets `max_premium` back to a number.

Exits follow SPY, not the option premium. They replace a partial premium take-profit and any timed premium exit:

- Stop when SPY trades 0.50 against the entry underlying price.
- Target is +1.00 in the trade's direction, and no farther than two $1 strikes before the UW King. King only caps when it sits in the trade's direction. If the room from the entry to that target is under 0.50, the entry is skipped.
- The stop, the target, and the 11:30 flatten each sell every contract still open.

Entries are weekdays 09:30–11:30 ET. 11:30 is the flatten, not a new entry. This window replaces the global 09:31–15:55 check for FlowVWAP entries only. QQQ still uses 09:31–15:55.

Flip skip: if the 9:30 open is within 1.00 of the UW flip (inclusive), no FlowVWAP entries for the rest of that day. A missing open or a missing flip does not skip.

Regime: enter only when the 9:30 open is strictly below the prior regular-hours low and Daybreak bias is `TREND_SHORT`. If the open, the prior low, or the bias is missing, the entry is allowed and logged as `unverified`.

The prior low and the 9:30 open come from Public.com regular-hours bars (`WEEK/ONE_DAY` and `DAY/ONE_MINUTE`). If that call fails and `MODE=PAPER` has Alpaca keys, the relay falls back to Alpaca IEX bars on `data.alpaca.markets`. It does not send orders there.

PAPER limit entry (FlowVWAP only). `entry_limit_offset` defaults to `0.40` and `entry_limit_timeout_sec` to `300`. After the usual filters and the contract pick, the relay buys a DAY limit at the option mid minus `0.40`, rounded down to the option tick: `$0.01` under `$3`, and `$0.01` at every price when Alpaca's `ppind` flag is true. Otherwise `$0.05` at and above `$3`. SPY is not hard-coded as penny-pilot; `ppind` is the penny program. A price that is positive but under one tick is raised to that tick. If mid minus the offset is `<= 0`, the entry is skipped and logged `limit_non_positive`, and it does not spend the daily cap. `null`, blank, or `0` (or removing the key) restores the immediate buy at the mid. PremarketES, PremarketES_MU, and QQQ_EMA_VWAP do not read the offset.

The working order (Alpaca id, limit, alert id, placed-at clock, SPY at the alert) is stored on the open position. `/flowvwap-monitor` and any later PAPER webhook check it. If it is still unfilled after 300 seconds, it is cancelled and logged `limit_timeout`. The daily order cap was already counted when the limit was placed, and the timeout does not give that slot back. A partial fill is left working until the timeout, a flat alert, or the 11:30 flatten; then the unfilled remainder is cancelled and the filled quantity is the position. A flat alert or the 11:30 flatten while the limit is still working cancels it, and sells whatever has filled.

Stops are measured from SPY at the fill, not from SPY at the alert. The monitor fetches the SPY quote when it first sees the fill. Fill-time is the better anchor for a resting limit: the stop and target are the risk box of the trade you actually have. Alert-time would arm them around a price that can be minutes stale, so a fill after SPY has already moved 0.50 would stop out on the next tick. The room-to-King check still uses SPY at the alert, before the order is sent. A fill is kept even if King has since moved closer.

Order email is off until configured. FlowVWAP's `notify_email` flag is true and the other profiles are false, but `NOTIFY_EMAIL_ENABLED` defaults to false, so nothing is sent. When that flag is true and `RESEND_API_KEY`, `NOTIFY_EMAIL_FROM`, and `NOTIFY_EMAIL_TO` are set, PAPER FlowVWAP events send one Resend email each: limit placed (contract, limit, mid at the alert), filled (fill price, fill time ET), cancelled (`limit_timeout`, `flat`, or `11:30`), and exit (`stop`, `target`, `flat`, or `11:30`) with dollar P&L per contract and total. P&L uses the entry fill against the exit limit, times 100, because the sell is only being placed. A failed send is logged and does not change the order. The same event is not sent twice; a failed send can be retried on a later tick. Passphrases, `CRON_SECRET`, `RELAY_CRON_SECRET`, and API keys are not put in the message or the log. This repo does not set any of those variables.

`POST /levels` stores the day's UW levels. Auth is `Authorization: Bearer $CRON_SECRET` (the same secret as `/keepalive`). Body:

```json
{"symbol": "SPY", "day": "2026-11-03", "flip": 580.25, "king": 585.0, "daybreak_bias": "TREND_SHORT"}
```

`symbol` defaults to SPY and `day` defaults to today in America/New_York. Posting again replaces flip, king, and bias and leaves a cached 9:30 open in place. The relay cannot read `/workspace/uw-gex-<date>/levels-applied.json`.

`GET` or `POST /flowvwap-monitor`, same bearer token, checks open FlowVWAP positions and applies the stop, the target, and the 11:30 flatten. A second call does not sell again: the row is already closed, and a paper sell reuses one Alpaca client order id. QQQ positions are ignored. This is not a Vercel cron. `vercel.json` still has only the weekday `/keepalive` ping.

Outside timer (cron-job.org, GitHub Actions, or any free scheduler), every minute from 09:30 to 11:35 ET on weekdays:

```bash
curl -fsS -H "Authorization: Bearer $CRON_SECRET" \
  "https://<your-deployment>/flowvwap-monitor"
```

And once around 09:15 ET, after the levels file is written, post `/levels` with that day's flip, king, and Daybreak bias. Do not add the path to `vercel.json` `crons` (that needs a paid cron).

`STRATEGY_OVERRIDES` is optional. The code defaults already match the rules. Set this only if the Vercel value already has a `FlowVWAP` object, because those keys replace the code defaults. Merge it into the existing object and keep `QQQ_EMA_VWAP` if it is there. This repo does not set the variable.

```json
{
  "FlowVWAP": {
    "delta_band_min": "0.50",
    "delta_band_max": "0.60",
    "itm_points": "0.50",
    "max_premium": null,
    "max_qty": 5,
    "order_qty": 5,
    "stop_loss_percent": null,
    "flow_rules": true
  }
}
```

`MAX_PREMIUM` applies to opens only. A close, flatten, take-profit, or stop is never rejected for being above the cap, for any strategy.

## Read-only preflight

`POST /preflight` takes the same alert JSON and the same passphrase as `/webhook`. It selects the contract with that strategy's settings (`FlowVWAP`, `QQQ_EMA_VWAP`, `PremarketES`, and `PremarketES_MU` each use their profile), then calls Public.com `preflight/single-leg`. The response has `contract`, `premium`, `delta`, and `passed`. FlowVWAP preflight still applies the flip, regime, and room checks. It does not apply the 09:30–11:30 clock, so a check after the close can still return the pick. PremarketES preflight uses the 0.35–0.40 OTM window and does not apply those FlowVWAP checks. PremarketES_MU preflight uses the 0.125–0.15 window and does not apply them either.

It does not place an order on Public or Alpaca, does not write a position or order row, and does not count toward the daily caps. It ignores the market-hours window, so a check after the close still returns the pick. `/webhook` still rejects outside 09:31–15:55 ET.

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
- The entry is a buy-to-open limit at the mid (or the ask). On a fill, the relay sells `floor(qty × TP_FRACTION)` at the take-profit and, when the strategy's stop is on, a stop-limit for the filled quantity. Prices use the tick above. `QQQ_EMA_VWAP` uses its profile here, including on keepalive.
- The request polls Alpaca about five times, half a second apart (about 2.5s). If the entry is still working, the position is stored as `PENDING` with `exits_placed` false. `GET /keepalive` and the next alert refresh that fill. Keepalive places the take-profit and stop once the entry is filled. The weekday cron stays `0 13 * * 1-5` UTC, which is before the cash open, so a same-day exit that missed the in-request poll needs a manual keepalive. A later flatten sells whatever has filled and cancels a resting entry that never filled.
- A flatten cancels resting take-profit and stop orders, then sells the remaining contracts at the mid (or the bid). It does not place those exits and cancel them in the same alert.
- Positions use `open_positions` with `mode = 'PAPER'`. Daily counters are per mode. In PAPER, exits do not consume `MAX_ORDERS_PER_DAY` even when `EXITS_COUNT_TOWARD_ORDER_CAP=true`. They still use `MAX_EXIT_ORDERS_PER_DAY`.
- New 0DTE entries are rejected at 15:30 ET for SPY and QQQ, and at 15:15 ET for anything else. Alpaca auto-liquidates 0DTE options at 15:45 ET. This relay does not send that liquidation. Exits are not subject to the cutoff.
- `GET /health` reports `mode`, `alpaca_configured`, and `GET /v2/account` status. Equity is included when Alpaca sends it. Cash and buying power are not.
- Secrets are not logged. An OCC open is placed but not stored; the position lifecycle above is the underlying alert (the FlowVWAP template).

`LIVE` is unchanged: it still places only the entry, or a flatten sell, and only when `MODE=LIVE` and `LIVE_CONFIRM=I_UNDERSTAND`. The take-profit and the stop stay reported.

## TradingView alert message

Paste one object into the strategy alert **Message** box. Replace only the passphrase placeholder. Leave `open_close` off so the relay infers open vs close from `market_position` / `prev_market_position`. FlowVWAP, PremarketES, and PremarketES_MU ignore the alert quantity and buy 5. `QQQ_EMA_VWAP` sells its one contract at the 50% take-profit (`TP_SINGLE`). The same PremarketES or PremarketES_MU message is the close: when the script goes flat, `market_position` is `flat` and the relay sells all 5 contracts. Extra keys such as `pm_log` are ignored.

FlowVWAP, usually on SPY:

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

PremarketES, on SPY. The `strategy` value has to be exactly `PremarketES`. Chart SPY. Entries are 09:31 ET or later. The script's close (the $0.50 SPY stop, and the 10:00 ET flatten) uses this same message.

```json
{
  "passphrase": "REPLACE_WITH_WEBHOOK_PASSPHRASE",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": "{{strategy.order.contracts}}",
  "underlying_price": "{{close}}",
  "market_position": "{{strategy.market_position}}",
  "prev_market_position": "{{strategy.prev_market_position}}",
  "strategy": "PremarketES",
  "alert_id": "{{ticker}}-{{strategy.order.id}}-{{timenow}}"
}
```

PremarketES_MU, on MU. The `strategy` value has to be exactly `PremarketES_MU`. Chart MU. The alert only fires on Monday, Wednesday, and Friday, when MU lists a same-day expiry. Entries are 09:31 ET or later, and in paper they also have to be before 15:15 ET. The script's close uses this same message.

```json
{
  "passphrase": "REPLACE_WITH_WEBHOOK_PASSPHRASE",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": "{{strategy.order.contracts}}",
  "underlying_price": "{{close}}",
  "market_position": "{{strategy.market_position}}",
  "prev_market_position": "{{strategy.prev_market_position}}",
  "strategy": "PremarketES_MU",
  "alert_id": "{{ticker}}-{{strategy.order.id}}-{{timenow}}"
}
```

QQQ_EMA_VWAP. The `strategy` value has to be exactly `QQQ_EMA_VWAP`. Any other name uses the global settings, not the FlowVWAP, PremarketES, or PremarketES_MU profile.

```json
{
  "passphrase": "REPLACE_WITH_WEBHOOK_PASSPHRASE",
  "symbol": "{{ticker}}",
  "side": "{{strategy.order.action}}",
  "qty": "{{strategy.order.contracts}}",
  "underlying_price": "{{close}}",
  "market_position": "{{strategy.market_position}}",
  "prev_market_position": "{{strategy.prev_market_position}}",
  "strategy": "QQQ_EMA_VWAP",
  "alert_id": "{{ticker}}-{{strategy.order.id}}-{{timenow}}"
}
```

## 0DTE strategy: QQQ EMA 5/13 with a VWAP filter

`tradingview/qqq_ema_vwap_0dte.pine` is the strategy that came out on top in `research/`. It goes long (call) when EMA(5) is above EMA(13) and the close is above VWAP, goes short (put) when both are below, and goes flat otherwise. It takes at most 3 entries a day, opens nothing at or after 15:30 ET, and flattens at the 15:40 bar close. A flip closes on one bar and enters the other side on the next, because the relay only flattens on a reversal.

Backtest, Sep 28 – Oct 26, 2026 (21 sessions, 1 contract, after modeled costs, $5 cap): 58 trades, 50% wins, $38.90 average per trade, profit factor 2.17, max drawdown $448. Ranking used Sep 28 – Oct 16; the held-out Oct 19 – Oct 26 sessions made $35.40 per trade (15 trades, profit factor 1.76). Fifteen held-out trades is a small sample, and the winner was picked from 135 symbol and signal pairs, so expect less live. Full results: `research/results/summary.json`.

Setup, PAPER first:

1. Chart QQQ on 5-minute bars with extended hours off, and add the Pine script as a strategy.
2. Create an alert on the strategy with the condition set to order fills only, the webhook URL set to the relay's `/webhook`, and the message from "TradingView alert message" above. Change `"strategy": "FlowVWAP"` to `"strategy": "QQQ_EMA_VWAP"` so its positions are tracked apart from other strategies.
3. The relay applies these values only for `QQQ_EMA_VWAP`. They are not global env edits.

| Setting | Value | Why |
| --- | --- | --- |
| Allowlist | `QQQ` is always allowed | Nothing outside the allowlist is traded. SPY stays on `ALLOWLIST`. |
| Delta band | `0.60` / `0.75` | Best band in both periods. `0.50`–`0.60` with TP 50 was a cheaper second choice ($19 per trade held out). |
| ITM points | `5` | Reaches 0.75 delta on QQQ 0DTE. |
| Take-profit | `50%`, single contract | 15–20% take-profits gave back most of the edge. With max qty 1 there is no take-profit otherwise. |
| Stop | off | 30% and 50% stops changed little; the signal exit came first. |
| Max premium | `5.00` on opens | Entries ran $1.31–$4.70. $3 would block half of them. Closes are not capped. |

Per-underlying notes from the same grid: SPY on EMA 5/13 (no VWAP filter) was the runner-up and held up on 14 of 60 contract settings; IWM and XSP did not survive the held-out sessions; SPX premiums ($17 median) are above any sensible cap, so use XSP for S&P exposure.

How the research was done (`research/`, stdlib Python only):

- Data came from the Robinhood MCP (`get_equity_historicals`, `get_index_historicals`, `get_option_instruments` with `state=expired`, `get_option_historicals`). The plan called for the Alpaca MCP, but its sign-in timed out, and Robinhood serves expired 0DTE contracts. `fetch_data.py` prints the calls still needed and ingests the saved responses into `research/data/` (git-ignored).
- `pricing.py` fits a per-timestamp implied-vol smile to real marks for five near-the-money 0DTE contracts per underlying and day, then prices whatever strike the relay would pick. Leave-one-out error against real marks: median $0.02–$0.06.
- `backtest.py` follows the relay: `choose_leg` ranking, mid-price limit, flatten-only reversals, TP_SINGLE take-profit, stop-limit, the PAPER cutoff and 15:40 flatten, and `MAX_PREMIUM` on opens only. `python research/backtest.py` reruns the grid in a few seconds; `pytest -q research/tests` covers the fill logic.
- Bands below 0.50 delta need OTM strikes. The research grid simulated those with a 5-point OTM window. `PremarketES` is the relay profile that trades that window (delta 0.35–0.40, ATM through 5 points OTM). FlowVWAP and `QQQ_EMA_VWAP` still pick ATM through ITM.

Those selection and exit values are the checked-in `QQQ_EMA_VWAP` profile. Do not copy them onto FlowVWAP or onto the global env. `QQQ` is already on the allowlist. Leave `MODE`, `LIVE_CONFIRM`, and the production `MAX_DAILY_OPENS=1000` as they are.
