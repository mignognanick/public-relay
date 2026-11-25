"""Requests the engine accepts and the decisions it returns.

Request dataclasses take raw values on purpose. The engine validates every field, so a webhook
payload with a string, a NaN, or a missing price is rejected as INVALID_REQUEST instead of raising.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Never, Optional

from quant_engine.risk.config import to_decimal

NAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:@/+-]{1,128}$")
MAX_QTY = 1_000_000
MAX_MULTIPLIER = 10_000
SNAPSHOT_FUTURE_TOLERANCE_S = 5


class Side(StrEnum):
    LONG = "LONG"
    SHORT = "SHORT"


class Mode(StrEnum):
    PAPER = "PAPER"
    PREFLIGHT = "PREFLIGHT"
    LIVE = "LIVE"


class Action(StrEnum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"


class Reason(StrEnum):
    INVALID_REQUEST = "INVALID_REQUEST"
    INVALID_SNAPSHOT = "INVALID_SNAPSHOT"
    STALE_SNAPSHOT = "STALE_SNAPSHOT"
    LIVE_TRADING_DISABLED = "LIVE_TRADING_DISABLED"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"
    KILL_SWITCH = "KILL_SWITCH"
    DAILY_LOSS_LIMIT = "DAILY_LOSS_LIMIT"
    WEEKLY_LOSS_LIMIT = "WEEKLY_LOSS_LIMIT"
    AGGREGATE_RISK_LIMIT = "AGGREGATE_RISK_LIMIT"
    GROSS_EXPOSURE_LIMIT = "GROSS_EXPOSURE_LIMIT"
    TRADE_RISK_LIMIT = "TRADE_RISK_LIMIT"


class InvalidRequest(ValueError):
    pass


@dataclass(frozen=True)
class AccountSnapshot:
    """Mark-to-market account equity at `as_of`, which must be timezone-aware.

    day_start_equity and week_start_equity are optional broker figures (Alpaca's `last_equity`
    is the prior close). When they are missing, the engine latches the first equity it sees in
    the period as the baseline.
    """

    equity: Any
    as_of: Any
    day_start_equity: Any = None
    week_start_equity: Any = None


@dataclass(frozen=True)
class EntryRequest:
    """A new position. `request_id` is the idempotency key and becomes the position id.

    entry_price and stop_price are per unit (per share, or per option contract before the
    multiplier). A LONG without a stop risks the whole entry price, which is the premium on a
    long option. A SHORT must carry a stop because its loss is otherwise unbounded.
    """

    request_id: Any
    strategy: Any
    symbol: Any
    side: Any
    entry_price: Any
    stop_price: Any = None
    requested_qty: Any = None
    multiplier: Any = 1
    cost_per_unit: Any = Decimal(0)
    mode: Any = Mode.PAPER


@dataclass(frozen=True)
class ExitRequest:
    """Reduce or close a position. qty None means every unit the engine has on record."""

    position_id: Any
    qty: Any = None
    reason: Any = ""


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    action: Action
    request_id: str
    qty: Optional[int]
    planned_risk: Decimal = Decimal(0)
    reasons: tuple[Reason, ...] = ()
    messages: tuple[str, ...] = ()
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "action": str(self.action),
            "request_id": self.request_id,
            "qty": self.qty,
            "planned_risk": format(self.planned_risk, "f"),
            "reasons": [str(reason) for reason in self.reasons],
            "messages": list(self.messages),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ValidEntry:
    request_id: str
    strategy: str
    symbol: str
    side: Side
    entry_price: Decimal
    stop_price: Optional[Decimal]
    requested_qty: Optional[int]
    multiplier: int
    cost_per_unit: Decimal
    mode: Mode


@dataclass(frozen=True)
class ValidSnapshot:
    equity: Decimal
    as_of: datetime
    day_start_equity: Optional[Decimal]
    week_start_equity: Optional[Decimal]


def _text(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str):
        raise InvalidRequest(f"{label} must be a string")
    text = value.strip()
    if not pattern.match(text):
        raise InvalidRequest(f"{label} {value!r} has invalid characters or length")
    return text


def _positive(value: Any, label: str) -> Decimal:
    try:
        number = to_decimal(value, label)
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if number <= 0:
        raise InvalidRequest(f"{label} must be greater than 0")
    return number


def _int(value: Any, label: str, lo: int, hi: int) -> int:
    if isinstance(value, bool):
        raise InvalidRequest(f"{label} must be a whole number")
    if isinstance(value, int):
        number = value
    else:
        try:
            parsed = to_decimal(value, label)
        except ValueError as exc:
            raise InvalidRequest(f"{label} must be a whole number") from exc
        if parsed != parsed.to_integral_value():
            raise InvalidRequest(f"{label} must be a whole number")
        number = int(parsed)
    if number < lo or number > hi:
        raise InvalidRequest(f"{label} must be between {lo} and {hi}")
    return number


def parse_qty(value: Any, label: str = "qty") -> int:
    return _int(value, label, 1, MAX_QTY)


def parse_fill_qty(value: Any, label: str = "filled_qty") -> int:
    return _int(value, label, 0, MAX_QTY)


def parse_price(value: Any, label: str) -> Decimal:
    return _positive(value, label)


def parse_side(value: Any) -> Side:
    text = str(value).strip().upper() if isinstance(value, (str, Side)) else ""
    match text:
        case "LONG":
            return Side.LONG
        case "SHORT":
            return Side.SHORT
        case _:
            raise InvalidRequest(f"side must be LONG or SHORT, got {value!r}")


def parse_mode(value: Any) -> Mode:
    text = str(value).strip().upper() if isinstance(value, (str, Mode)) else ""
    match text:
        case "PAPER":
            return Mode.PAPER
        case "PREFLIGHT":
            return Mode.PREFLIGHT
        case "LIVE":
            return Mode.LIVE
        case _:
            raise InvalidRequest(f"mode must be PAPER, PREFLIGHT, or LIVE, got {value!r}")


def parse_position_id(value: Any) -> str:
    return _text(value, "position_id", ID_RE)


def validate_entry(request: EntryRequest) -> ValidEntry:
    if not isinstance(request, EntryRequest):
        raise InvalidRequest("entry request must be an EntryRequest")
    side = parse_side(request.side)
    entry = _positive(request.entry_price, "entry_price")
    stop: Optional[Decimal] = None
    if request.stop_price is not None and not (isinstance(request.stop_price, str) and request.stop_price.strip() == ""):
        stop = _positive(request.stop_price, "stop_price")
    match side:
        case Side.LONG:
            if stop is not None and stop >= entry:
                raise InvalidRequest("a LONG stop must be below the entry price")
        case Side.SHORT:
            if stop is None:
                raise InvalidRequest("a SHORT entry needs a stop; its loss is otherwise unbounded")
            if stop <= entry:
                raise InvalidRequest("a SHORT stop must be above the entry price")
        case _ as unreachable:
            _never(unreachable)
    requested = None if request.requested_qty is None else parse_qty(request.requested_qty, "requested_qty")
    try:
        cost = to_decimal(request.cost_per_unit, "cost_per_unit")
    except ValueError as exc:
        raise InvalidRequest(str(exc)) from exc
    if cost < 0:
        raise InvalidRequest("cost_per_unit cannot be negative")
    return ValidEntry(
        request_id=_text(request.request_id, "request_id", ID_RE),
        strategy=_text(request.strategy, "strategy", NAME_RE),
        symbol=_text(request.symbol, "symbol", NAME_RE).upper(),
        side=side,
        entry_price=entry,
        stop_price=stop,
        requested_qty=requested,
        multiplier=_int(request.multiplier, "multiplier", 1, MAX_MULTIPLIER),
        cost_per_unit=cost,
        mode=parse_mode(request.mode),
    )


def validate_snapshot(snapshot: AccountSnapshot, now: datetime) -> ValidSnapshot:
    if not isinstance(snapshot, AccountSnapshot):
        raise InvalidRequest("snapshot must be an AccountSnapshot")
    equity = _positive(snapshot.equity, "equity")
    as_of = snapshot.as_of
    if not isinstance(as_of, datetime) or as_of.tzinfo is None or as_of.utcoffset() is None:
        raise InvalidRequest("snapshot as_of must be a timezone-aware datetime")
    if (as_of - now).total_seconds() > SNAPSHOT_FUTURE_TOLERANCE_S:
        raise InvalidRequest("snapshot as_of is in the future")
    day_start = None if snapshot.day_start_equity is None else _positive(snapshot.day_start_equity, "day_start_equity")
    week_start = None if snapshot.week_start_equity is None else _positive(snapshot.week_start_equity, "week_start_equity")
    return ValidSnapshot(equity, as_of, day_start, week_start)


def _never(value: Never) -> Never:
    raise RuntimeError(f"unhandled variant: {value!r}")
