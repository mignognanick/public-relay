# Portfolio risk engine (`quant_engine/risk`)

A standalone, broker-agnostic risk gate for Quant Engine V1. It decides how large a new entry may be, or refuses it, and it approves every well-formed exit. It does not place orders, read credentials, or call any network service. It is not wired into `api/index.py` yet.

## Defaults

All limits are percent of account equity and configurable.

| Limit | Default | Env var | Measured against |
| --- | --- | --- | --- |
| Planned risk per trade | 0.25% | `RISK_MAX_TRADE_RISK_PCT` | current equity |
| Daily loss threshold | 0.75% | `RISK_DAILY_LOSS_LIMIT_PCT` | equity at the start of the America/New_York day |
| Weekly loss threshold | 2.00% | `RISK_WEEKLY_LOSS_LIMIT_PCT` | equity at the start of the ISO week (Mon–Sun, New York) |
| Aggregate planned open risk | 0.50% | `RISK_MAX_OPEN_RISK_PCT` | current equity |
| Gross entry notional | 100% | `RISK_MAX_GROSS_EXPOSURE_PCT` | current equity |
| Account snapshot max age | 120 s | `RISK_MAX_SNAPSHOT_AGE_SECONDS` | engine clock |
| Weekly breach engages kill switch | true | `RISK_KILL_SWITCH_ON_WEEKLY_BREACH` | |

A bad value refuses to start. Per-trade risk may not exceed aggregate open risk, and the daily threshold may not exceed the weekly one. These env vars are read only by `RiskConfig.from_env`; nothing in production reads them yet, and none were added to Vercel.

## Definitions

- **Risk per unit** = `|entry − stop| × multiplier + cost_per_unit`. A LONG with no stop risks the whole entry price (a long option's premium). A SHORT must have a stop.
- **Planned risk** of a position = risk per unit × quantity. Open risk is the sum over every PENDING and OPEN position.
- **Period loss** = `max(0, period start equity − current equity)`. Current equity is mark-to-market, so it already includes realized and unrealized P&L. The start equity is the higher of the broker figure supplied on the snapshot (`day_start_equity`, `week_start_equity`) and the first equity the engine saw in that period, so a loss is never understated.
- **Worst-case headroom** for a period = loss limit − period loss − open risk. A new entry must fit in it, so every open stop could fill and the period would still be inside its limit. This is conservative: a losing open position counts both in equity and in planned risk.

## Entry decision

`RiskEngine.evaluate_entry(EntryRequest, AccountSnapshot) -> RiskDecision`

1. Validate the request and snapshot. Strings from a webhook are accepted; booleans, NaN, infinities, negative or zero prices, stops on the wrong side, fractional quantities, and naive timestamps are `INVALID_REQUEST` or `INVALID_SNAPSHOT`. A snapshot older than the age limit is `STALE_SNAPSHOT`.
2. `mode=LIVE` is always `LIVE_TRADING_DISABLED`. There is no setting that changes this in V1.
3. Under an exclusive lock on the state:
   - A repeated `request_id` returns the original approval (idempotent retries).
   - Kill switch engaged → `KILL_SWITCH`.
   - Weekly loss ≥ limit, or the week already latched → `WEEKLY_LOSS_LIMIT`, latch the week, and engage the kill switch when configured.
   - Daily loss ≥ limit, or the day already latched → `DAILY_LOSS_LIMIT`, latch the day.
   - Quantity = minimum of the per-trade budget, aggregate open-risk headroom, daily and weekly worst-case headroom, gross-exposure headroom, and the requested quantity, each rounded down to whole units.
   - Zero units is a rejection that names every limit that allowed zero. Otherwise the planned risk is reserved as a PENDING position before the lock is released.

A latched day or week stays latched if equity recovers. The day lockout clears at the next New York midnight. The week lockout clears the following Monday. A kill switch the engine engaged stays on until a person turns it off.

## Exit decision

`RiskEngine.evaluate_exit(ExitRequest) -> RiskDecision`

Exits never read the kill switch, loss limits, exposure limits, or headroom, and they never take the write lock or change state. The only rejection is a malformed request: empty or invalid `position_id`, or a quantity that is not a positive whole number. A quantity above what the engine holds is clamped to the held quantity so an exit cannot flip the position. An unknown position, or a state file that cannot be read, is still approved and flagged (`untracked`, `state_unavailable`); the broker position is the source of truth for what can be sold.

## Lifecycle

```
evaluate_entry ─approved─▶ PENDING ─confirm_entry_fill(n)─▶ OPEN(n) ─record_exit_fill(k)─▶ OPEN(n−k) … removed at 0
                              │                    │
                              └─release_entry──────┴─confirm_entry_fill(0)─▶ removed
```

- `confirm_entry_fill(id, qty, fill_price=None)` records the filled size. A worse fill raises the recorded risk and returns `risk_overrun: true`; a fill through the stop keeps the approved per-unit risk and returns `fill_beyond_stop: true`.
- `release_entry(id)` drops a PENDING reservation whose order was rejected or cancelled unfilled.
- `record_exit_fill(id, qty)` reduces open risk. Over-reporting closes the whole position.
- `engage_kill_switch(reason, actor)` and `disengage_kill_switch(reason, actor)` need both fields, are recorded in a bounded history, and the engine's own actor (`risk-engine`) cannot disengage.

Reservations do not expire. A leaked reservation blocks entries, which is the safe direction; clear it with `release_entry`.

## State and failure handling

| Failure | Entries | Exits |
| --- | --- | --- |
| State file missing | Fresh state (kill switch off) | Approved |
| State file unparseable or tampered (bad JSON, unknown version, negative risk, id mismatch) | `STATE_UNAVAILABLE` | Approved, `state_unavailable: true` |
| Write fails (disk full, permissions) | `STATE_UNAVAILABLE`; previous file kept, no partial reservation | Not affected (exits do not write) |

`FileStateStore` writes to a temp file, fsyncs, and renames, so a crash mid-write leaves the previous state. An `fcntl` lock on a sidecar `.lock` file serializes threads and processes on one host. `InMemoryStateStore` is for tests and dry runs.

Neither store is shared across Vercel serverless instances. Before the relay calls this engine, the state needs a Supabase store whose transaction is a single SQL function taking a row lock, the same pattern as `relay_try_consume`.

## Usage

```python
from datetime import datetime, timezone
from quant_engine.risk import AccountSnapshot, EntryRequest, ExitRequest, FileStateStore, RiskConfig, RiskEngine

engine = RiskEngine(RiskConfig.from_env(), FileStateStore("var/risk_state.json"))
snap = AccountSnapshot(equity="100000", as_of=datetime.now(timezone.utc), day_start_equity="100250")
decision = engine.evaluate_entry(EntryRequest(
    request_id="SPY-PremarketES-1", strategy="PremarketES", symbol="SPY", side="LONG",
    entry_price="1.20", multiplier=100, requested_qty=5, mode="PAPER"), snap)
# decision.qty == 2, decision.planned_risk == Decimal("240.00"), reduced from 5
engine.confirm_entry_fill("SPY-PremarketES-1", 2)
engine.evaluate_exit(ExitRequest("SPY-PremarketES-1"))   # always approved
engine.record_exit_fill("SPY-PremarketES-1", 2)
```

Operator commands:

```bash
python -m quant_engine.risk.cli --state var/risk_state.json status
python -m quant_engine.risk.cli --state var/risk_state.json engage --reason "broker outage" --actor nick
python -m quant_engine.risk.cli --state var/risk_state.json disengage --reason "resolved" --actor nick
```

## Sizing impact on the current profiles

On a $100,000 paper account the per-trade budget is $250. A long 0DTE option without a stop risks its full premium, so a $1.20 contract sizes to 2, a $1.45 contract to 1, and anything above $2.50 is refused. The current FlowVWAP, PremarketES, and PremarketES_MU profiles buy 5 contracts. When the engine is wired in, those paper sizes will drop unless the strategy supplies a stop price (which lowers risk per contract) or the limits are raised. That is a product decision for review, not something this milestone changes.
