"""Risk limits. Every percentage is a percent of account equity: 0.25 means 0.25%, not 25%."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

ENV_KEYS = {
    "max_trade_risk_pct": "RISK_MAX_TRADE_RISK_PCT",
    "daily_loss_limit_pct": "RISK_DAILY_LOSS_LIMIT_PCT",
    "weekly_loss_limit_pct": "RISK_WEEKLY_LOSS_LIMIT_PCT",
    "max_open_risk_pct": "RISK_MAX_OPEN_RISK_PCT",
    "max_gross_exposure_pct": "RISK_MAX_GROSS_EXPOSURE_PCT",
    "max_snapshot_age_seconds": "RISK_MAX_SNAPSHOT_AGE_SECONDS",
    "kill_switch_on_weekly_breach": "RISK_KILL_SWITCH_ON_WEEKLY_BREACH",
}


def to_decimal(value: Any, label: str) -> Decimal:
    """Strict number parse. Booleans, None, NaN, and infinities are refused."""
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{label} must be a number")
    if isinstance(value, Decimal):
        number = value
    elif isinstance(value, (int, float, str)):
        try:
            number = Decimal(str(value).strip())
        except InvalidOperation as exc:
            raise ValueError(f"{label} must be a number") from exc
    else:
        raise ValueError(f"{label} must be a number")
    if not number.is_finite():
        raise ValueError(f"{label} must be a finite number")
    return number


def _pct(value: Any, label: str, hi: Decimal) -> Decimal:
    number = to_decimal(value, label)
    if number <= 0 or number > hi:
        raise ValueError(f"{label} must be greater than 0 and at most {hi}")
    return number


def _bool(value: Any, label: str) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{label} must be true or false")


@dataclass(frozen=True)
class RiskConfig:
    """Paper-trading defaults for Quant Engine V1.

    max_trade_risk_pct      planned loss to the protective stop on one new entry.
    daily_loss_limit_pct    loss from the ET day's starting equity that stops new entries for the day.
    weekly_loss_limit_pct   loss from the ISO week's starting equity that stops new entries for the week.
    max_open_risk_pct       sum of planned risk across every open and pending entry.
    max_gross_exposure_pct  sum of entry notional across open and pending entries (100 = no leverage).
    """

    max_trade_risk_pct: Decimal = Decimal("0.25")
    daily_loss_limit_pct: Decimal = Decimal("0.75")
    weekly_loss_limit_pct: Decimal = Decimal("2.00")
    max_open_risk_pct: Decimal = Decimal("0.50")
    max_gross_exposure_pct: Decimal = Decimal("100")
    max_snapshot_age_seconds: int = 120
    kill_switch_on_weekly_breach: bool = True
    timezone: str = "America/New_York"

    def __post_init__(self) -> None:
        hundred = Decimal(100)
        object.__setattr__(self, "max_trade_risk_pct", _pct(self.max_trade_risk_pct, "max_trade_risk_pct", hundred))
        object.__setattr__(self, "daily_loss_limit_pct", _pct(self.daily_loss_limit_pct, "daily_loss_limit_pct", hundred))
        object.__setattr__(self, "weekly_loss_limit_pct", _pct(self.weekly_loss_limit_pct, "weekly_loss_limit_pct", hundred))
        object.__setattr__(self, "max_open_risk_pct", _pct(self.max_open_risk_pct, "max_open_risk_pct", hundred))
        object.__setattr__(self, "max_gross_exposure_pct",
                           _pct(self.max_gross_exposure_pct, "max_gross_exposure_pct", Decimal(1000)))
        age = self.max_snapshot_age_seconds
        if isinstance(age, bool) or not isinstance(age, int) or age < 1 or age > 86_400:
            raise ValueError("max_snapshot_age_seconds must be an integer from 1 to 86400")
        object.__setattr__(self, "kill_switch_on_weekly_breach",
                           _bool(self.kill_switch_on_weekly_breach, "kill_switch_on_weekly_breach"))
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
            raise ValueError(f"timezone {self.timezone!r} is not a known IANA zone") from exc
        if self.max_trade_risk_pct > self.max_open_risk_pct:
            raise ValueError("max_trade_risk_pct cannot exceed max_open_risk_pct")
        if self.daily_loss_limit_pct > self.weekly_loss_limit_pct:
            raise ValueError("daily_loss_limit_pct cannot exceed weekly_loss_limit_pct")

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, Any]] = None) -> "RiskConfig":
        """Blank or missing variables keep the defaults. A bad value refuses to start."""
        source: Mapping[str, Any] = {} if env is None else env
        values: dict[str, Any] = {}
        for name, key in ENV_KEYS.items():
            raw = source.get(key)
            if raw is None or str(raw).strip() == "":
                continue
            if name == "max_snapshot_age_seconds":
                text = str(raw).strip()
                if not text.isdigit():
                    raise ValueError(f"{key} must be a whole number of seconds")
                values[name] = int(text)
            else:
                values[name] = raw
        return cls(**values)

    def public_view(self) -> dict[str, Any]:
        return {
            "max_trade_risk_pct": format(self.max_trade_risk_pct, "f"),
            "daily_loss_limit_pct": format(self.daily_loss_limit_pct, "f"),
            "weekly_loss_limit_pct": format(self.weekly_loss_limit_pct, "f"),
            "max_open_risk_pct": format(self.max_open_risk_pct, "f"),
            "max_gross_exposure_pct": format(self.max_gross_exposure_pct, "f"),
            "max_snapshot_age_seconds": self.max_snapshot_age_seconds,
            "kill_switch_on_weekly_breach": self.kill_switch_on_weekly_breach,
            "timezone": self.timezone,
        }
