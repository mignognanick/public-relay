from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from quant_engine.risk import AccountSnapshot, EntryRequest, InMemoryStateStore, RiskConfig, RiskEngine

# Tuesday 2026-11-24 10:00 America/New_York.
NOW = datetime(2026, 11, 24, 15, 0, tzinfo=timezone.utc)
EQUITY = Decimal("100000")


class Clock:
    def __init__(self, now: datetime = NOW):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


def make_engine(store=None, clock=None, **config: Any) -> RiskEngine:
    return RiskEngine(RiskConfig(**config), store or InMemoryStateStore(), clock or Clock())


def snapshot(equity: Any = EQUITY, as_of: Any = None, clock: Clock | None = None, **kwargs: Any) -> AccountSnapshot:
    if as_of is None:
        as_of = clock() if clock is not None else NOW
    return AccountSnapshot(equity=equity, as_of=as_of, **kwargs)


def option_entry(request_id: str = "e1", **overrides: Any) -> EntryRequest:
    """Long 0DTE option, no stop: risk per contract is the whole premium x 100."""
    fields: dict[str, Any] = {
        "request_id": request_id, "strategy": "PremarketES", "symbol": "SPY", "side": "LONG",
        "entry_price": "1.20", "multiplier": 100, "mode": "PAPER",
    }
    fields.update(overrides)
    return EntryRequest(**fields)


def stock_entry(request_id: str = "s1", **overrides: Any) -> EntryRequest:
    """Long stock with a $1 stop: $1 of risk per share."""
    fields: dict[str, Any] = {
        "request_id": request_id, "strategy": "Swing", "symbol": "AAPL", "side": "LONG",
        "entry_price": "200", "stop_price": "199", "multiplier": 1, "mode": "PAPER",
    }
    fields.update(overrides)
    return EntryRequest(**fields)
