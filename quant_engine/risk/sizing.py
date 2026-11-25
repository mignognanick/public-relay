"""Fixed-fractional position sizing. Pure functions; no state."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR
from typing import Never, Optional

from quant_engine.risk.models import Side

HUNDRED = Decimal(100)


def pct_of(amount: Decimal, pct: Decimal) -> Decimal:
    return amount * pct / HUNDRED


def risk_per_unit(side: Side, entry: Decimal, stop: Optional[Decimal], multiplier: int,
                  cost_per_unit: Decimal = Decimal(0)) -> Decimal:
    """Dollars lost on one unit if the protective exit fills at its price, plus costs.

    No stop on a LONG means the price can go to zero, so the whole entry is at risk.
    """
    match side:
        case Side.LONG:
            distance = entry if stop is None else entry - stop
        case Side.SHORT:
            if stop is None:
                raise ValueError("a SHORT needs a stop to have a defined risk")
            distance = stop - entry
        case _ as unreachable:
            _never(unreachable)
    if distance <= 0:
        raise ValueError("the stop must be on the losing side of the entry")
    return distance * Decimal(multiplier) + cost_per_unit


def exposure_per_unit(entry: Decimal, multiplier: int) -> Decimal:
    return entry * Decimal(multiplier)


def units_within(budget: Decimal, per_unit: Decimal) -> int:
    """Whole units whose total stays at or under the budget. Zero when the budget is used up."""
    if per_unit <= 0:
        raise ValueError("per-unit amount must be greater than 0")
    if budget <= 0:
        return 0
    return int((budget / per_unit).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class SizingResult:
    qty: int
    risk_per_unit: Decimal
    planned_risk: Decimal
    trade_budget: Decimal


def size_position(equity: Decimal, max_trade_risk_pct: Decimal, side: Side, entry: Decimal,
                  stop: Optional[Decimal], multiplier: int, cost_per_unit: Decimal = Decimal(0),
                  requested_qty: Optional[int] = None) -> SizingResult:
    """Largest quantity whose planned risk is within max_trade_risk_pct of equity.

    A requested quantity can only lower the result. It never raises it past the budget.
    """
    per_unit = risk_per_unit(side, entry, stop, multiplier, cost_per_unit)
    budget = pct_of(equity, max_trade_risk_pct)
    qty = units_within(budget, per_unit)
    if requested_qty is not None:
        qty = min(qty, requested_qty)
    return SizingResult(qty=qty, risk_per_unit=per_unit, planned_risk=per_unit * qty, trade_budget=budget)


def _never(value: Never) -> Never:
    raise RuntimeError(f"unhandled variant: {value!r}")
