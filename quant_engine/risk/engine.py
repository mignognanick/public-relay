"""Standalone portfolio risk engine for Quant Engine V1.

Entries are sized and gated. Exits are only validated for shape and clamped so they cannot sell
more than the engine has on record. No kill switch, loss limit, exposure limit, or state failure
ever rejects an exit.

Entry checks, in order. The first hard stop wins; sizing limits then pick the smallest quantity:
  1. Request and account snapshot are valid and the snapshot is fresh.
  2. Mode is not LIVE. Quant Engine V1 never approves a live entry.
  3. The request id is new. A repeated id returns the original approval (idempotent).
  4. The kill switch is off. An unreadable state counts as on.
  5. The day and week are not locked out. A breach latches for the rest of that period.
  6. Quantity is the minimum of:
       per-trade budget          equity x max_trade_risk_pct
       aggregate open risk       equity x max_open_risk_pct - open risk
       daily worst-case headroom day limit - day loss - open risk
       weekly worst-case headroom week limit - week loss - open risk
       gross exposure headroom   equity x max_gross_exposure_pct - gross exposure
       the requested quantity
     Zero is a rejection naming every limit that allowed zero.
An approval reserves its planned risk immediately, so concurrent requests see it.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Optional

from quant_engine.risk.config import RiskConfig
from quant_engine.risk.models import (
    Action, AccountSnapshot, EntryRequest, ExitRequest, InvalidRequest, Mode, Reason, RiskDecision,
    ValidEntry, ValidSnapshot, parse_fill_qty, parse_position_id, parse_price, parse_qty, parse_side,
    validate_entry, validate_snapshot,
)
from quant_engine.risk.sizing import exposure_per_unit, pct_of, risk_per_unit, units_within
from quant_engine.risk.state import (
    KILL_SWITCH_HISTORY_LIMIT, PositionRecord, RiskState, StateStore, StateUnavailable,
)

ENGINE_ACTOR = "risk-engine"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


class RiskEngine:
    def __init__(self, config: RiskConfig, store: StateStore,
                 clock: Callable[[], datetime] = _utc_now):
        self.config = config
        self.store = store
        self.clock = clock

    # ------------------------------------------------------------------ periods
    def _periods(self, now: datetime) -> tuple[str, str]:
        local = now.astimezone(self.config.zone).date()
        year, week, _ = local.isocalendar()
        return local.isoformat(), f"{year}-W{week:02d}"

    @staticmethod
    def _baseline(state: RiskState, name: str, period: str, equity: Decimal,
                  supplied: Optional[Decimal]) -> Decimal:
        """Period start equity. The higher of the stored and supplied figures, so a loss is never understated."""
        stored = state.baselines.get(name)
        current: Optional[Decimal] = None
        if stored is not None and stored.get("period") == period:
            current = Decimal(stored["equity"])
        candidates = [value for value in (current, supplied) if value is not None]
        baseline = max(candidates) if candidates else equity
        state.baselines[name] = {"period": period, "equity": format(baseline, "f")}
        return baseline

    # ------------------------------------------------------------------ entries
    def evaluate_entry(self, request: EntryRequest, snapshot: AccountSnapshot) -> RiskDecision:
        now = self.clock()
        raw_id = request.request_id if isinstance(request, EntryRequest) and isinstance(request.request_id, str) else ""
        try:
            entry = validate_entry(request)
        except InvalidRequest as exc:
            return self._reject(raw_id, Reason.INVALID_REQUEST, str(exc))
        try:
            account = validate_snapshot(snapshot, now)
        except InvalidRequest as exc:
            return self._reject(entry.request_id, Reason.INVALID_SNAPSHOT, str(exc))
        age = (now - account.as_of).total_seconds()
        if age > self.config.max_snapshot_age_seconds:
            return self._reject(entry.request_id, Reason.STALE_SNAPSHOT,
                                f"account snapshot is {int(age)}s old; limit is {self.config.max_snapshot_age_seconds}s")
        if entry.mode == Mode.LIVE:
            return self._reject(entry.request_id, Reason.LIVE_TRADING_DISABLED,
                                "Quant Engine V1 is paper-first and never approves a LIVE entry")
        try:
            with self.store.transaction() as state:
                return self._evaluate_locked(state, entry, account, now)
        except StateUnavailable as exc:
            return self._reject(entry.request_id, Reason.STATE_UNAVAILABLE,
                                f"risk state unavailable; entries fail closed ({exc})")

    def _evaluate_locked(self, state: RiskState, entry: ValidEntry, account: ValidSnapshot,
                         now: datetime) -> RiskDecision:
        existing = state.positions.get(entry.request_id)
        if existing is not None:
            return RiskDecision(
                approved=True, action=Action.ENTRY, request_id=entry.request_id, qty=existing.qty,
                planned_risk=existing.planned_risk, messages=("duplicate request; original approval returned",),
                detail={"duplicate": True, "status": existing.status})
        if state.kill_switch.get("engaged"):
            return self._reject(entry.request_id, Reason.KILL_SWITCH,
                                f"kill switch engaged: {state.kill_switch.get('reason') or 'no reason recorded'}")

        day, week = self._periods(now)
        equity = account.equity
        day_start = self._baseline(state, "day", day, equity, account.day_start_equity)
        week_start = self._baseline(state, "week", week, equity, account.week_start_equity)
        day_loss = max(Decimal(0), day_start - equity)
        week_loss = max(Decimal(0), week_start - equity)
        day_limit = pct_of(day_start, self.config.daily_loss_limit_pct)
        week_limit = pct_of(week_start, self.config.weekly_loss_limit_pct)
        metrics: dict[str, Any] = {
            "day": day, "week": week, "equity": _money(equity),
            "day_start_equity": _money(day_start), "week_start_equity": _money(week_start),
            "day_loss": _money(day_loss), "week_loss": _money(week_loss),
            "day_loss_limit": _money(day_limit), "week_loss_limit": _money(week_limit),
            "open_risk": _money(state.open_risk), "gross_exposure": _money(state.gross_exposure),
        }

        if week_loss >= week_limit or state.lockouts.get("week") == week:
            state.lockouts["week"] = week
            if self.config.kill_switch_on_weekly_breach and not state.kill_switch.get("engaged"):
                self._set_kill_switch(state, True, f"weekly loss limit breached in {week}", ENGINE_ACTOR, now)
            return self._reject(entry.request_id, Reason.WEEKLY_LOSS_LIMIT,
                                f"weekly loss {_money(week_loss)} reached the {_money(week_limit)} limit for {week}",
                                metrics)
        if day_loss >= day_limit or state.lockouts.get("day") == day:
            state.lockouts["day"] = day
            return self._reject(entry.request_id, Reason.DAILY_LOSS_LIMIT,
                                f"daily loss {_money(day_loss)} reached the {_money(day_limit)} limit for {day}",
                                metrics)

        per_unit = risk_per_unit(entry.side, entry.entry_price, entry.stop_price, entry.multiplier,
                                 entry.cost_per_unit)
        per_unit_exposure = exposure_per_unit(entry.entry_price, entry.multiplier)
        open_risk = state.open_risk
        budgets = {
            Reason.TRADE_RISK_LIMIT: pct_of(equity, self.config.max_trade_risk_pct),
            Reason.AGGREGATE_RISK_LIMIT: pct_of(equity, self.config.max_open_risk_pct) - open_risk,
            Reason.DAILY_LOSS_LIMIT: day_limit - day_loss - open_risk,
            Reason.WEEKLY_LOSS_LIMIT: week_limit - week_loss - open_risk,
        }
        limits = {reason: units_within(budget, per_unit) for reason, budget in budgets.items()}
        limits[Reason.GROSS_EXPOSURE_LIMIT] = units_within(
            pct_of(equity, self.config.max_gross_exposure_pct) - state.gross_exposure, per_unit_exposure)
        metrics.update({
            "risk_per_unit": _money(per_unit),
            "budgets": {str(reason): _money(budget) for reason, budget in budgets.items()},
            "unit_limits": {str(reason): qty for reason, qty in limits.items()},
        })
        qty = min(limits.values())
        if entry.requested_qty is not None:
            qty = min(qty, entry.requested_qty)
        if qty < 1:
            blocking = tuple(reason for reason, units in limits.items() if units < 1)
            messages = tuple(
                f"{reason}: one unit risks {_money(per_unit)}"
                + ("" if reason == Reason.GROSS_EXPOSURE_LIMIT else f", budget left {_money(budgets[reason])}")
                for reason in blocking)
            return RiskDecision(approved=False, action=Action.ENTRY, request_id=entry.request_id, qty=0,
                                reasons=blocking, messages=messages, detail=metrics)

        binding = sorted(str(reason) for reason, units in limits.items() if units == qty)
        if entry.requested_qty == qty:
            binding.insert(0, "REQUESTED_QTY")
        record = PositionRecord(
            position_id=entry.request_id, strategy=entry.strategy, symbol=entry.symbol,
            side=str(entry.side), mode=str(entry.mode), status="PENDING", qty=qty,
            entry_price=entry.entry_price, stop_price=entry.stop_price, multiplier=entry.multiplier,
            cost_per_unit=entry.cost_per_unit, risk_per_unit=per_unit, exposure_per_unit=per_unit_exposure,
            opened_at=now.isoformat())
        state.positions[record.position_id] = record
        metrics["binding_limits"] = binding
        metrics["reduced_from"] = entry.requested_qty if entry.requested_qty and entry.requested_qty > qty else None
        return RiskDecision(approved=True, action=Action.ENTRY, request_id=entry.request_id, qty=qty,
                            planned_risk=record.planned_risk, detail=metrics)

    def confirm_entry_fill(self, position_id: str, filled_qty: Any,
                           fill_price: Any = None) -> dict[str, Any]:
        """Turn a reservation into an open position at the filled size. Zero filled releases it.

        A worse fill price than planned raises the recorded risk. The entry has already happened, so
        that is reported as `risk_overrun`, not refused; later entries see the higher open risk.
        """
        key = parse_position_id(position_id)
        filled = parse_fill_qty(filled_qty)
        price = None if fill_price is None else parse_price(fill_price, "fill_price")
        with self.store.transaction() as state:
            record = state.positions.get(key)
            if record is None:
                raise KeyError(f"no reservation {key}")
            if filled > record.qty:
                raise InvalidRequest(f"filled_qty {filled} exceeds the approved {record.qty}")
            if filled == 0:
                del state.positions[key]
                return {"position_id": key, "status": "RELEASED", "qty": 0}
            approved_risk = record.planned_risk
            updated = replace(record, status="OPEN", qty=filled)
            beyond_stop = False
            if price is not None:
                try:
                    per_unit = risk_per_unit(parse_side(record.side), price, record.stop_price,
                                             record.multiplier, record.cost_per_unit)
                except ValueError:
                    # Filled through the stop. Keep the approved per-unit risk; the exit should fire now.
                    per_unit = record.risk_per_unit
                    beyond_stop = True
                updated = replace(updated, entry_price=price, risk_per_unit=per_unit,
                                  exposure_per_unit=exposure_per_unit(price, record.multiplier))
            state.positions[key] = updated
            return {
                "position_id": key, "status": "OPEN", "qty": filled,
                "planned_risk": _money(updated.planned_risk),
                "approved_risk": _money(approved_risk),
                "risk_overrun": updated.risk_per_unit > record.risk_per_unit,
                "fill_beyond_stop": beyond_stop,
            }

    def release_entry(self, position_id: str) -> bool:
        """Drop a reservation whose order was rejected or cancelled before any fill."""
        key = parse_position_id(position_id)
        with self.store.transaction() as state:
            record = state.positions.get(key)
            if record is None or record.status != "PENDING":
                return False
            del state.positions[key]
            return True

    # ------------------------------------------------------------------ exits
    def evaluate_exit(self, request: ExitRequest) -> RiskDecision:
        """Approve any well-formed exit. Risk limits and the kill switch are never consulted.

        Known positions are clamped to what is on record, so an exit cannot flip the position.
        An unknown position, or an unreadable state, is still approved: the broker position is the
        source of truth for what can be sold, and blocking a protective exit is the worse failure.
        """
        if not isinstance(request, ExitRequest):
            return self._reject("", Reason.INVALID_REQUEST, "exit request must be an ExitRequest", action=Action.EXIT)
        try:
            key = parse_position_id(request.position_id)
            wanted = None if request.qty is None else parse_qty(request.qty)
        except InvalidRequest as exc:
            raw = request.position_id if isinstance(request.position_id, str) else ""
            return self._reject(raw, Reason.INVALID_REQUEST, str(exc), action=Action.EXIT)
        detail: dict[str, Any] = {"exit_reason": str(request.reason or "")}
        try:
            record = self.store.read().positions.get(key)
        except StateUnavailable as exc:
            detail.update({"state_unavailable": True, "error": str(exc)})
            return RiskDecision(approved=True, action=Action.EXIT, request_id=key, qty=wanted,
                                messages=("risk state unavailable; exit approved without a position check",),
                                detail=detail)
        if record is None:
            detail["untracked"] = True
            return RiskDecision(approved=True, action=Action.EXIT, request_id=key, qty=wanted,
                                messages=("position is not tracked by the risk engine; exit approved",),
                                detail=detail)
        qty = record.qty if wanted is None else min(wanted, record.qty)
        detail.update({"held_qty": record.qty, "clamped": wanted is not None and wanted > record.qty,
                       "status": record.status})
        return RiskDecision(approved=True, action=Action.EXIT, request_id=key, qty=qty,
                            planned_risk=record.risk_per_unit * qty, detail=detail)

    def record_exit_fill(self, position_id: str, qty: Any) -> dict[str, Any]:
        """Reduce open risk after an exit fills. Over-reporting closes the whole position."""
        key = parse_position_id(position_id)
        closed = parse_qty(qty, "qty")
        with self.store.transaction() as state:
            record = state.positions.get(key)
            if record is None:
                return {"position_id": key, "tracked": False, "remaining": None}
            remaining = max(0, record.qty - closed)
            if remaining == 0:
                del state.positions[key]
            else:
                state.positions[key] = replace(record, qty=remaining)
            return {"position_id": key, "tracked": True, "remaining": remaining,
                    "open_risk": _money(state.open_risk)}

    # ------------------------------------------------------------------ kill switch
    def engage_kill_switch(self, reason: str, actor: str) -> dict[str, Any]:
        return self._change_kill_switch(True, reason, actor)

    def disengage_kill_switch(self, reason: str, actor: str) -> dict[str, Any]:
        """Manual only. The engine engages the switch itself but never turns it off."""
        if str(actor).strip() == ENGINE_ACTOR:
            raise ValueError("the risk engine cannot disengage its own kill switch")
        return self._change_kill_switch(False, reason, actor)

    def _change_kill_switch(self, engaged: bool, reason: str, actor: str) -> dict[str, Any]:
        reason_text = str(reason or "").strip()
        actor_text = str(actor or "").strip()
        if not reason_text or not actor_text:
            raise ValueError("kill switch changes need a reason and an actor")
        with self.store.transaction() as state:
            self._set_kill_switch(state, engaged, reason_text, actor_text, self.clock())
            return dict(state.kill_switch)

    @staticmethod
    def _set_kill_switch(state: RiskState, engaged: bool, reason: str, actor: str, now: datetime) -> None:
        entry = {"engaged": engaged, "reason": reason, "actor": actor, "changed_at": now.isoformat()}
        state.kill_switch = dict(entry)
        state.kill_switch_history.append(entry)
        del state.kill_switch_history[:-KILL_SWITCH_HISTORY_LIMIT]

    # ------------------------------------------------------------------ reporting
    def status(self) -> dict[str, Any]:
        try:
            state = self.store.read()
        except StateUnavailable as exc:
            return {"ok": False, "entries_allowed": False, "error": str(exc),
                    "config": self.config.public_view()}
        day, week = self._periods(self.clock())
        return {
            "ok": True,
            "entries_allowed": not state.kill_switch.get("engaged")
            and state.lockouts.get("day") != day and state.lockouts.get("week") != week,
            "kill_switch": state.kill_switch,
            "day_locked": state.lockouts.get("day") == day,
            "week_locked": state.lockouts.get("week") == week,
            "open_risk": _money(state.open_risk),
            "gross_exposure": _money(state.gross_exposure),
            "positions": {key: record.to_dict() for key, record in sorted(state.positions.items())},
            "config": self.config.public_view(),
        }

    @staticmethod
    def _reject(request_id: str, reason: Reason, message: str, detail: Optional[dict[str, Any]] = None,
                action: Action = Action.ENTRY) -> RiskDecision:
        return RiskDecision(approved=False, action=action, request_id=request_id, qty=0,
                            reasons=(reason,), messages=(message,), detail=detail or {})
