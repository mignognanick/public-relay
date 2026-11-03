"""FlowVWAP (SPY 0DTE) entry gates and underlying-price exits.

QQQ_EMA_VWAP does not use this module. Distances are SPY points. King is capped
two $1 strikes before the UW King level. A missing regime input is unverified,
not a skip. A missing King leaves the +1.00 target uncapped.
"""
from __future__ import annotations

from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Never, Optional
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
ENTRY_START = dtime(9, 30)
ENTRY_END = dtime(11, 30)  # entries stop here; an open position flattens at this time
STOP_POINTS = Decimal("0.50")
TARGET_POINTS = Decimal("1.00")
MIN_ROOM = Decimal("0.50")
FLIP_BAND = Decimal("1.00")
STRIKE_WIDTH = Decimal("1")  # SPY strikes are $1 apart
KING_STRIKES = 2
UNDERLYING_EXIT = "exits follow the underlying price"

Side = Literal["BUY", "SELL"]


def _never(value: Never) -> Never:
    raise RuntimeError(f"unhandled variant: {value!r}")


def as_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def _number(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return number


def _price(value: Any) -> Optional[Decimal]:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number


def _when(value: Any) -> Optional[datetime]:
    if value is None or str(value).strip() == "":
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ET)
    return parsed.astimezone(ET)


def _field(bar: dict, *names: str) -> Any:
    for name in names:
        if name in bar and bar.get(name) not in (None, ""):
            return bar.get(name)
    return None


def entry_block(now: datetime) -> Optional[str]:
    """None when a new FlowVWAP entry is allowed. 11:30 is the flatten, not an entry."""
    current = as_et(now)
    if current.weekday() < 5 and ENTRY_START <= current.time() < ENTRY_END:
        return None
    return f"FlowVWAP entries are only 09:30-11:30 ET (now {current:%a %H:%M})"


def flatten_due(now: datetime) -> bool:
    """True at 11:30 ET and after, every day. The monitor uses this to sell the whole position."""
    return as_et(now).time() >= ENTRY_END


def flip_skip(open_930: Optional[Decimal], flip: Optional[Decimal],
              band: Decimal = FLIP_BAND) -> Optional[str]:
    """Skip the whole day when the 9:30 open is within `band` of the UW flip.

    A missing open or a missing flip does not skip. The distance is inclusive.
    """
    if open_930 is None or flip is None:
        return None
    if abs(open_930 - flip) <= band:
        return "FlowVWAP flip skip: the 9:30 open is within 1.00 of the UW flip"
    return None


def regime(open_930: Optional[Decimal], prior_rth_low: Optional[Decimal],
           bias: Optional[str]) -> tuple[Literal["verified", "unverified", "blocked"], list[str]]:
    """(status, missing inputs).

    Verified only when the 9:30 open is strictly below the prior regular-hours low
    and Daybreak bias is TREND_SHORT. If the open, the prior low, or the bias is
    missing, the status is unverified and the caller allows the trade.
    """
    missing: list[str] = []
    if open_930 is None:
        missing.append("open_930")
    if prior_rth_low is None:
        missing.append("prior_rth_low")
    text = "" if bias is None else str(bias).strip()
    if text == "":
        missing.append("daybreak_bias")
    if missing:
        return "unverified", missing
    if open_930 is not None and prior_rth_low is not None and open_930 < prior_rth_low and text.upper() == "TREND_SHORT":
        return "verified", []
    return "blocked", []


def exit_levels(entry: Decimal, side: Side, king: Optional[Decimal],
                stop_points: Decimal = STOP_POINTS, target_points: Decimal = TARGET_POINTS,
                min_room: Decimal = MIN_ROOM, strike_width: Decimal = STRIKE_WIDTH,
                king_strikes: int = KING_STRIKES) -> dict[str, Any]:
    """Stop, target, room, and a skip reason when the capped target is under min_room.

    Long (BUY / call): stop is entry - 0.50, target is entry + 1.00. When King
    is above the entry, the target cannot pass two strikes before King. Short
    (SELL / put) mirrors that and only caps when King is below the entry. A King
    on the other side of the trade does not cap. No King means the +1.00 target.
    """
    match side:
        case "BUY":
            stop = entry - stop_points
            raw = entry + target_points
            if king is None or king <= entry:
                target = raw
            else:
                target = min(raw, king - (strike_width * king_strikes))
            room = target - entry
        case "SELL":
            stop = entry + stop_points
            raw = entry - target_points
            if king is None or king >= entry:
                target = raw
            else:
                target = max(raw, king + (strike_width * king_strikes))
            room = entry - target
        case _ as unreachable:
            _never(unreachable)
    capped = king is not None and target != raw
    skip = None
    if room < min_room:
        skip = "FlowVWAP room to the capped target is under 0.50"
    return {
        "stop": stop,
        "target": target,
        "room": room,
        "capped": capped,
        "skip": skip,
    }


def exit_signal(now: datetime, side: str, price: Optional[Decimal], stop: Optional[Decimal],
                target: Optional[Decimal]) -> Optional[Literal["flatten", "stop", "target"]]:
    """Which underlying exit to take. Flatten wins over a price exit. The whole position is sold."""
    if flatten_due(now):
        return "flatten"
    if price is None or stop is None or target is None:
        return None
    if side == "BUY":
        if price <= stop:
            return "stop"
        if price >= target:
            return "target"
        return None
    if side == "SELL":
        if price >= stop:
            return "stop"
        if price <= target:
            return "target"
        return None
    return None


def _bars(payload: Any) -> list[dict]:
    if not isinstance(payload, dict):
        return []
    regular = payload.get("regularMarket")
    if not isinstance(regular, dict):
        regular = payload.get("regular_market")
    if not isinstance(regular, dict):
        # Alpaca returns {"bars": [...]}. Public nests them under regularMarket.
        raw = payload.get("bars")
        return [bar for bar in raw if isinstance(bar, dict)] if isinstance(raw, list) else []
    raw = regular.get("bars")
    if not isinstance(raw, list):
        return []
    return [bar for bar in raw if isinstance(bar, dict)]


def session_facts(daily: Any, minutes: Any, day: date) -> dict[str, Optional[Decimal]]:
    """9:30 open and the prior regular-hours low from Public or Alpaca bars.

    The open is the 09:30 minute bar when that bar exists, otherwise today's daily
    open. The prior low is the low of the latest daily bar before `day`.
    """
    prior: Optional[Decimal] = None
    prior_day: Optional[date] = None
    daily_open: Optional[Decimal] = None
    for bar in _bars(daily):
        when = _when(_field(bar, "timestamp", "t"))
        if when is None:
            continue
        if when.date() < day and (prior_day is None or when.date() > prior_day):
            low = _price(_field(bar, "low", "l"))
            if low is not None:
                prior = low
                prior_day = when.date()
        if when.date() == day:
            opened = _price(_field(bar, "open", "o"))
            if opened is not None:
                daily_open = opened
    opened_930: Optional[Decimal] = None
    for bar in _bars(minutes):
        when = _when(_field(bar, "timestamp", "t"))
        if when is None or when.date() != day or when.time() != ENTRY_START:
            continue
        opened_930 = _price(_field(bar, "open", "o"))
        if opened_930 is not None:
            break
    return {"open_930": opened_930 if opened_930 is not None else daily_open, "prior_rth_low": prior}


def prior_lookback(day: date) -> date:
    """Calendar start for a short daily-bar request. Weekends are already in the bars."""
    return day - timedelta(days=10)
