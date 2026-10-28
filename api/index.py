"""TradingView webhook -> Public.com single-leg options relay.

Vercel + Supabase port. Review-only. Defaults to MODE=PREFLIGHT, which never places an order.
LIVE requires BOTH MODE=LIVE and LIVE_CONFIRM=I_UNDERSTAND. MODE=PAPER does not. It places the
selected contract on Alpaca's paper host and does not use LIVE_CONFIRM.

A full OCC symbol is forwarded as sent. An underlying such as SPY selects a call (BUY) or put
(SELL) for the configured expiry, or sells the stored runners when the alert flattens or flips.
Positions are one row per underlying + strategy + mode, so a QQQ flatten cannot close SPY.
Per-strategy selection and exit overrides live in BUILTIN_STRATEGY_OVERRIDES; STRATEGY_OVERRIDES
(JSON) replaces individual keys. Daily order and spend caps stay shared across strategies.
Selection stays on Public.com. PAPER then checks the OCC symbol at Alpaca and places the entry,
take-profit, and optional stop there. LIVE still places only the order under consideration.
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import re
import sys
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable, Literal, Never, Optional
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

ET = ZoneInfo("America/New_York")
API_BASE = "https://api.public.com"
TOKEN_PATH = "/userapiauthservice/personal/access-tokens"
ACCOUNT_PATH = "/userapigateway/trading/account"
TOKEN_VALIDITY_MIN = 15
TOKEN_REFRESH_MARGIN_S = 120  # refresh 2 min before expiry
OCC_RE = re.compile(r"^([A-Z]{1,6})(\d{6})([CP])(\d{8})$")
UNDERLYING_RE = re.compile(r"^[A-Z]{1,6}$")
STRATEGY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
MARKET_OPEN = dtime(9, 31)
MARKET_CLOSE = dtime(15, 55)
# Alpaca rejects new 0DTE entries before the cash close. SPY and QQQ have a later cutoff.
# QQQ is always accepted so QQQ_EMA_VWAP needs no ALLOWLIST edit. SPY stays on ALLOWLIST.
BUILTIN_UNDERLYINGS = frozenset({"QQQ"})
PAPER_HOST = "paper-api.alpaca.markets"
PAPER_BASE_URL = "https://paper-api.alpaca.markets"
PAPER_SPY_QQQ_CUTOFF = dtime(15, 30)
PAPER_OTHER_CUTOFF = dtime(15, 15)
PAPER_FILL_POLLS = 5
PAPER_FILL_WAIT_S = 0.5
# Defaults. Each one is overridable from the environment; see Settings.from_env.
ITM_POINTS = Decimal("5")
DELTA_LO = Decimal("0.50")
DELTA_HI = Decimal("0.60")
LimitFallback = Literal["ask", "bid", "reject"]
REDACT_KEYS = {"passphrase", "secret", "authorization", "accesstoken", "access_token",
               "public_secret", "webhook_passphrase", "apikey", "supabase_service_key",
               "alpaca_key_id", "alpaca_secret_key", "apca-api-key-id", "apca-api-secret-key"}


# --------------------------------------------------------------------------- config
def _env_text(env: dict, key: str) -> str:
    raw = env.get(key, "")
    return str(raw).strip() if raw is not None else ""


def _env_int(env: dict, key: str, default: int, lo: int, hi: int) -> int:
    text = _env_text(env, key)
    value = default if text == "" else int(text)
    if value < lo or value > hi:
        raise ValueError(f"{key} must be between {lo} and {hi}")
    return value


def _env_decimal(env: dict, key: str, default: str) -> Decimal:
    text = _env_text(env, key)
    return Decimal(default if text == "" else text)


def _env_bool(env: dict, key: str, default: bool) -> bool:
    text = _env_text(env, key).lower()
    if text == "":
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{key} must be true or false")


def _env_fallback(env: dict, key: str, default: LimitFallback) -> LimitFallback:
    text = _env_text(env, key).lower()
    if text == "":
        return default
    match text:
        case "ask" | "bid" | "reject":
            return text
        case _:
            raise ValueError(f"{key} must be ask, bid, or reject")


def _env_stop_percent(env: dict, key: str) -> Optional[Decimal]:
    """Empty disables the stop. A number is the percent below the entry, strictly between 0 and 100."""
    text = _env_text(env, key)
    if text == "":
        return None
    value = Decimal(text)
    if value <= 0 or value >= 100:
        raise ValueError(f"{key} must be between 0 and 100, or empty to disable")
    return value


# Fields a strategy profile may replace. Anything else (caps, expiry, fallbacks, mode) stays global.
STRATEGY_OVERRIDE_KEYS = (
    "delta_band_min",
    "delta_band_max",
    "itm_points",
    "max_premium",
    "tp_percent",
    "tp_single",
    "stop_loss_percent",
    "max_qty",
)
# Checked-in defaults. An absent strategy uses the global env. STRATEGY_OVERRIDES merges per key
# on top of these, so FlowVWAP is unchanged unless its name is listed there.
BUILTIN_STRATEGY_OVERRIDE_RAW: dict[str, dict[str, Any]] = {
    "QQQ_EMA_VWAP": {
        "delta_band_min": "0.60",
        "delta_band_max": "0.75",
        "itm_points": "5",
        "max_premium": "5.00",
        "tp_percent": "50",
        "tp_single": True,
        "stop_loss_percent": None,
        "max_qty": 1,
    },
}


def _override_decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"{label} must be a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not number.is_finite():
        raise ValueError(f"{label} must be a number")
    return number


def _bounded_decimal(value: Any, label: str, lo: Decimal, hi: Decimal) -> Decimal:
    number = _override_decimal(value, label)
    if number < lo or number > hi:
        raise ValueError(f"{label} must be between {lo} and {hi}")
    return number


def _parse_stop_override(value: Any, label: str) -> Optional[Decimal]:
    """Null or blank turns the stop off for that strategy. A number is the percent below entry."""
    if value is None:
        return None
    if isinstance(value, str) and value.strip() == "":
        return None
    number = _override_decimal(value, label)
    if number <= 0 or number >= 100:
        raise ValueError(f"{label} must be between 0 and 100, or null to disable")
    return number


def _parse_override_fields(name: str, fields: Any, source: str) -> dict[str, Any]:
    label = f"{source}[{name}]"
    if not isinstance(fields, dict):
        raise ValueError(f"{label} must be an object")
    unknown = sorted(set(fields) - set(STRATEGY_OVERRIDE_KEYS))
    if unknown:
        raise ValueError(f"{label} has unknown keys: {', '.join(unknown)}")
    parsed: dict[str, Any] = {}
    if "delta_band_min" in fields:
        parsed["delta_band_min"] = _bounded_decimal(
            fields["delta_band_min"], f"{label}.delta_band_min", Decimal(0), Decimal(1))
    if "delta_band_max" in fields:
        parsed["delta_band_max"] = _bounded_decimal(
            fields["delta_band_max"], f"{label}.delta_band_max", Decimal(0), Decimal(1))
    if "itm_points" in fields:
        parsed["itm_points"] = _bounded_decimal(
            fields["itm_points"], f"{label}.itm_points", Decimal(0), Decimal(50))
    if "max_premium" in fields:
        premium = _override_decimal(fields["max_premium"], f"{label}.max_premium")
        if premium <= 0:
            raise ValueError(f"{label}.max_premium must be > 0")
        parsed["max_premium"] = premium
    if "tp_percent" in fields:
        parsed["tp_percent"] = _bounded_decimal(
            fields["tp_percent"], f"{label}.tp_percent", Decimal(0), Decimal(500))
    if "tp_single" in fields:
        flag = fields["tp_single"]
        if not isinstance(flag, bool):
            raise ValueError(f"{label}.tp_single must be true or false")
        parsed["tp_single"] = flag
    if "stop_loss_percent" in fields:
        parsed["stop_loss_percent"] = _parse_stop_override(
            fields["stop_loss_percent"], f"{label}.stop_loss_percent")
    if "max_qty" in fields:
        qty = fields["max_qty"]
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1 or qty > 100:
            raise ValueError(f"{label}.max_qty must be an integer from 1 to 100")
        parsed["max_qty"] = qty
    return parsed


def _builtin_strategy_overrides() -> dict[str, dict[str, Any]]:
    return {
        name: _parse_override_fields(name, fields, "builtin strategy")
        for name, fields in BUILTIN_STRATEGY_OVERRIDE_RAW.items()
    }


BUILTIN_STRATEGY_OVERRIDES = _builtin_strategy_overrides()


def _parse_strategy_overrides(text: str) -> dict[str, dict[str, Any]]:
    """STRATEGY_OVERRIDES is optional JSON: {\"StrategyName\": {\"max_premium\": \"5.00\", ...}}."""
    if text == "":
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("STRATEGY_OVERRIDES must be a JSON object") from exc
    if not isinstance(data, dict):
        raise ValueError("STRATEGY_OVERRIDES must be a JSON object")
    parsed: dict[str, dict[str, Any]] = {}
    for name, fields in data.items():
        if not isinstance(name, str) or not STRATEGY_RE.match(name):
            raise ValueError(f"STRATEGY_OVERRIDES key {name!r} is not a valid strategy name")
        parsed[name] = _parse_override_fields(name, fields, "STRATEGY_OVERRIDES")
    return parsed


def _allowlist(env: dict) -> set[str]:
    listed = {item.strip().upper() for item in env.get("ALLOWLIST", "").split(",") if item.strip()}
    return listed | set(BUILTIN_UNDERLYINGS)


def _paper_base_url(env: dict) -> str:
    """PAPER mode may talk only to Alpaca's paper host. The live trading host is refused."""
    text = _env_text(env, "ALPACA_BASE_URL") or PAPER_BASE_URL
    parsed = urlparse(text)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != PAPER_HOST or parsed.port not in (None, 443):
        raise ValueError(f"ALPACA_BASE_URL must be {PAPER_BASE_URL}")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError(f"ALPACA_BASE_URL must be {PAPER_BASE_URL}")
    return PAPER_BASE_URL


def _effective_mode(requested: str, live_ok: bool) -> str:
    if requested == "PAPER":
        return "PAPER"
    if live_ok:
        return "LIVE"
    return "PREFLIGHT"


@dataclass
class Settings:
    public_secret: str = ""
    webhook_passphrase: str = ""
    mode: str = "PREFLIGHT"           # effective mode, after LIVE_CONFIRM check
    requested_mode: str = "PREFLIGHT"
    account_id: str = ""              # optional; auto-discovered if blank
    max_qty: int = 1
    max_premium: Decimal = Decimal("5.00")  # OPEN limit only; exits are never capped
    max_orders_per_day: int = 3
    max_daily_opens: Decimal = Decimal("1500")  # $ spent on OPEN orders per ET day
    allowlist: set[str] = field(default_factory=set)
    supabase_url: str = ""
    supabase_key: str = ""
    enforce_hours: bool = True
    delta_band_min: Decimal = DELTA_LO
    delta_band_max: Decimal = DELTA_HI
    itm_points: Decimal = ITM_POINTS
    expiry_days: int = 0
    open_limit_fallback: LimitFallback = "ask"
    close_limit_fallback: LimitFallback = "bid"
    tp_percent: Decimal = Decimal("20")
    tp_fraction: Decimal = Decimal("0.50")
    tp_single: bool = False
    stop_loss_percent: Optional[Decimal] = None  # empty = off
    exits_count_toward_order_cap: bool = False
    max_exit_orders_per_day: int = 6
    alpaca_key_id: str = ""
    alpaca_secret_key: str = ""
    alpaca_base_url: str = ""
    # Env layer only. Built-in profiles are applied in for_strategy and are not stored here.
    strategy_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def overridable_view(self) -> dict[str, Any]:
        """Effective selection and exit fields a strategy profile can replace. No secrets."""
        return {
            "delta_band_min": format(self.delta_band_min, "f"),
            "delta_band_max": format(self.delta_band_max, "f"),
            "itm_points": format(self.itm_points, "f"),
            "max_premium": format(self.max_premium, "f"),
            "max_qty": self.max_qty,
            "tp_percent": format(self.tp_percent, "f"),
            "tp_single": self.tp_single,
            "stop_loss_percent": None if self.stop_loss_percent is None else format(self.stop_loss_percent, "f"),
        }

    def for_strategy(self, name: str) -> "Settings":
        """Global settings with that strategy's built-in profile, then STRATEGY_OVERRIDES, applied.

        Unknown strategy names return this object unchanged. Caps, expiry, and mode are not copied
        from a profile, so every strategy shares MAX_ORDERS_PER_DAY and MAX_DAILY_OPENS.
        """
        merged = dict(BUILTIN_STRATEGY_OVERRIDES.get(name, {}))
        merged.update(self.strategy_overrides.get(name, {}))
        if not merged:
            return self
        return replace(self, **merged)

    def strategy_public_view(self) -> dict[str, dict[str, Any]]:
        names = set(BUILTIN_STRATEGY_OVERRIDES) | set(self.strategy_overrides)
        return {name: self.for_strategy(name).overridable_view() for name in sorted(names)}

    def public_view(self) -> dict[str, Any]:
        """Non-secret tunables safe to log and return from /health."""
        return {
            "delta_band_min": format(self.delta_band_min, "f"),
            "delta_band_max": format(self.delta_band_max, "f"),
            "itm_points": format(self.itm_points, "f"),
            "expiry_days": self.expiry_days,
            "open_limit_fallback": self.open_limit_fallback,
            "close_limit_fallback": self.close_limit_fallback,
            "max_qty": self.max_qty,
            "max_premium": format(self.max_premium, "f"),
            "max_orders_per_day": self.max_orders_per_day,
            "max_daily_opens": format(self.max_daily_opens, "f"),
            "tp_percent": format(self.tp_percent, "f"),
            "tp_fraction": format(self.tp_fraction, "f"),
            "tp_single": self.tp_single,
            "stop_loss_percent": None if self.stop_loss_percent is None else format(self.stop_loss_percent, "f"),
            "exits_count_toward_order_cap": self.exits_count_toward_order_cap,
            "max_exit_orders_per_day": self.max_exit_orders_per_day,
            "enforce_hours": self.enforce_hours,
            "alpaca_configured": bool(self.alpaca_key_id and self.alpaca_secret_key),
            "strategies": self.strategy_public_view(),
            "daily_caps": "shared",
        }

    def _validate_strategy_profiles(self) -> None:
        names = set(BUILTIN_STRATEGY_OVERRIDES) | set(self.strategy_overrides)
        for name in sorted(names):
            profile = self.for_strategy(name)
            if profile.delta_band_min < 0 or profile.delta_band_max > 1 or profile.delta_band_min > profile.delta_band_max:
                raise ValueError(
                    f"strategy {name} delta band must satisfy 0 <= min <= max <= 1 "
                    f"(got {profile.delta_band_min}..{profile.delta_band_max})")

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Settings":
        e = os.environ if env is None else env
        requested = e.get("MODE", "PREFLIGHT").strip().upper()
        live_ok = requested == "LIVE" and e.get("LIVE_CONFIRM", "") == "I_UNDERSTAND"
        mode = _effective_mode(requested, live_ok)
        # Alpaca credentials are read only for PAPER. Other modes leave them blank.
        if mode == "PAPER":
            alpaca_key_id = _env_text(e, "ALPACA_KEY_ID")
            alpaca_secret_key = _env_text(e, "ALPACA_SECRET_KEY")
            alpaca_base_url = _paper_base_url(e)
            if not alpaca_key_id or not alpaca_secret_key:
                raise ValueError("MODE=PAPER requires ALPACA_KEY_ID and ALPACA_SECRET_KEY")
        else:
            alpaca_key_id = ""
            alpaca_secret_key = ""
            alpaca_base_url = ""
        band_min = _env_decimal(e, "DELTA_BAND_MIN", "0.50")
        band_max = _env_decimal(e, "DELTA_BAND_MAX", "0.60")
        if band_min < 0 or band_max > 1 or band_min > band_max:
            raise ValueError("DELTA_BAND_MIN/MAX must satisfy 0 <= min <= max <= 1")
        itm_points = _env_decimal(e, "ITM_POINTS", "5")
        if itm_points < 0 or itm_points > 50:
            raise ValueError("ITM_POINTS must be between 0 and 50")
        tp_percent = _env_decimal(e, "TP_PERCENT", "20")
        if tp_percent < 0 or tp_percent > 500:
            raise ValueError("TP_PERCENT must be between 0 and 500")
        tp_fraction = _env_decimal(e, "TP_FRACTION", "0.50")
        if tp_fraction < 0 or tp_fraction > 1:
            raise ValueError("TP_FRACTION must be between 0 and 1")
        settings = cls(
            public_secret=e.get("PUBLIC_SECRET", ""),
            webhook_passphrase=e.get("WEBHOOK_PASSPHRASE", ""),
            requested_mode=requested,
            mode=mode,
            account_id=e.get("PUBLIC_ACCOUNT_ID", "").strip(),
            max_qty=_env_int(e, "MAX_QTY", 1, 1, 100),
            max_premium=_env_decimal(e, "MAX_PREMIUM", "5.00"),
            max_orders_per_day=_env_int(e, "MAX_ORDERS_PER_DAY", 3, 1, 1000),
            max_daily_opens=_env_decimal(e, "MAX_DAILY_OPENS", "1500"),
            allowlist=_allowlist(e),
            supabase_url=e.get("SUPABASE_URL", "").rstrip("/"),
            supabase_key=e.get("SUPABASE_SERVICE_KEY", ""),
            enforce_hours=_env_bool(e, "ENFORCE_MARKET_HOURS", True),
            delta_band_min=band_min,
            delta_band_max=band_max,
            itm_points=itm_points,
            expiry_days=_env_int(e, "EXPIRY_DAYS", 0, 0, 30),
            open_limit_fallback=_env_fallback(e, "OPEN_LIMIT_FALLBACK", "ask"),
            close_limit_fallback=_env_fallback(e, "CLOSE_LIMIT_FALLBACK", "bid"),
            tp_percent=tp_percent,
            tp_fraction=tp_fraction,
            tp_single=_env_bool(e, "TP_SINGLE", False),
            stop_loss_percent=_env_stop_percent(e, "STOP_LOSS_PERCENT"),
            exits_count_toward_order_cap=_env_bool(e, "EXITS_COUNT_TOWARD_ORDER_CAP", False),
            max_exit_orders_per_day=_env_int(e, "MAX_EXIT_ORDERS_PER_DAY", 6, 1, 1000),
            alpaca_key_id=alpaca_key_id,
            alpaca_secret_key=alpaca_secret_key,
            alpaca_base_url=alpaca_base_url,
            strategy_overrides=_parse_strategy_overrides(_env_text(e, "STRATEGY_OVERRIDES")),
        )
        settings._validate_strategy_profiles()
        return settings


# --------------------------------------------------------------------------- logging
def redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: ("***" if str(k).lower() in REDACT_KEYS else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


log = logging.getLogger("relay")
if not log.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)


def jlog(event: str, **kw: Any) -> None:
    rec = {"ts": datetime.now(ET).isoformat(), "event": event, **redact(kw)}
    log.info(json.dumps(rec, default=str))


# --------------------------------------------------------------------------- state
class StoreError(RuntimeError):
    pass


def _rpc_reason(data: Any, fn: str) -> Optional[str]:
    if data is None or data == "":
        return None
    if isinstance(data, str):
        return data
    raise StoreError(f"{fn} returned an unexpected result")


class Store:
    """Supabase Postgres via PostgREST. Atomic SQL functions (see supabase/migrations) make
    idempotency and daily caps safe across concurrent serverless instances."""

    def __init__(self, url: str, key: str, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.configured = bool(url and key)
        if not self.configured:
            jlog("store_unconfigured", reason="SUPABASE_URL/SUPABASE_SERVICE_KEY missing; webhook will 503")
        self.http = httpx.AsyncClient(base_url=f"{url or 'http://unconfigured.invalid'}/rest/v1", timeout=5.0, transport=transport,
                                      headers={"apikey": key, "Authorization": f"Bearer {key}",
                                               "Content-Type": "application/json"})

    async def _rpc(self, fn: str, args: dict) -> Any:
        if not self.configured:
            raise StoreError("store not configured")
        r = await self.http.post(f"/rpc/{fn}", json=args)
        if r.status_code >= 400:
            raise StoreError(f"supabase {fn} -> {r.status_code}")
        return r.json() if r.content else None

    async def reserve(self, alert_id: str, day: str) -> bool:
        return bool(await self._rpc("relay_reserve", {"p_alert_id": alert_id, "p_day": day}))

    async def try_consume(self, day: str, mode: str, notional: Decimal, is_open: bool, s: "Settings",
                          is_exit: bool = False, exit_counts: Optional[bool] = None) -> Optional[str]:
        # Entries increment orders and, when opening, open_notional. Exits increment exit_orders.
        # They also increment orders only when EXITS_COUNT_TOWARD_ORDER_CAP is on.
        # PAPER passes exit_counts=False so an exit is never blocked by the entry cap.
        counts = s.exits_count_toward_order_cap if exit_counts is None else exit_counts
        data = await self._rpc("relay_try_consume", {
            "p_day": day, "p_mode": mode, "p_notional": str(notional), "p_is_open": is_open,
            "p_max_orders": s.max_orders_per_day, "p_max_opens": str(s.max_daily_opens),
            "p_is_exit": is_exit, "p_exit_counts": counts,
            "p_max_exit_orders": s.max_exit_orders_per_day})
        return _rpc_reason(data, "relay_try_consume")

    async def release(self, day: str, mode: str, notional: Decimal, is_open: bool,
                      is_exit: bool = False, exit_counts: bool = False) -> None:
        await self._rpc("relay_release", {
            "p_day": day, "p_mode": mode, "p_notional": str(notional), "p_is_open": is_open,
            "p_is_exit": is_exit, "p_exit_counts": exit_counts})

    async def get_position(self, underlying: str, strategy: str, mode: str) -> Optional[dict]:
        data = await self._rpc("relay_get_position", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode})
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return data

    async def record_open(self, underlying: str, strategy: str, mode: str, symbol: str, entry_side: str,
                          right_side: str, qty: int, strike: str, alert_id: str, qty_open: Optional[int] = None,
                          tp_qty: int = 0, tp_limit: Optional[str] = None, tp_order_id: Optional[str] = None,
                          tp_status: str = "NONE", entry_limit: Optional[str] = None,
                          tp_filled: int = 0, stop_qty: int = 0, stop_filled: int = 0,
                          stop_limit: Optional[str] = None, stop_order_id: Optional[str] = None,
                          stop_status: str = "NONE", entry_order_id: Optional[str] = None,
                          entry_status: str = "NONE", entry_filled: int = 0,
                          exits_placed: bool = False) -> Optional[str]:
        data = await self._rpc("relay_record_open", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode, "p_symbol": symbol,
            "p_entry_side": entry_side, "p_right_side": right_side, "p_qty": qty, "p_strike": strike,
            "p_alert_id": alert_id, "p_qty_open": qty if qty_open is None else qty_open,
            "p_tp_qty": tp_qty, "p_tp_limit": tp_limit, "p_tp_order_id": tp_order_id,
            "p_tp_status": tp_status, "p_entry_limit": entry_limit, "p_tp_filled": tp_filled,
            "p_stop_qty": stop_qty, "p_stop_filled": stop_filled, "p_stop_limit": stop_limit,
            "p_stop_order_id": stop_order_id, "p_stop_status": stop_status,
            "p_entry_order_id": entry_order_id, "p_entry_status": entry_status,
            "p_entry_filled": entry_filled, "p_exits_placed": exits_placed})
        return _rpc_reason(data, "relay_record_open")

    async def list_open_positions(self, mode: str) -> list:
        data = await self._rpc("relay_list_open_positions", {"p_mode": mode})
        if isinstance(data, dict):
            return [data]
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []

    async def save_paper(self, row: dict) -> Optional[str]:
        data = await self._rpc("relay_save_paper", {
            "p_underlying": row["underlying"], "p_strategy": row["strategy"], "p_mode": row["mode"],
            "p_entry_order_id": row.get("entry_order_id"), "p_entry_status": row.get("entry_status"),
            "p_entry_filled": row.get("entry_filled") or 0,
            "p_tp_qty": row.get("tp_qty") or 0, "p_tp_filled": row.get("tp_filled") or 0,
            "p_tp_limit": row.get("tp_limit"), "p_tp_order_id": row.get("tp_order_id"),
            "p_tp_status": row.get("tp_status") or "NONE",
            "p_stop_qty": row.get("stop_qty") or 0, "p_stop_filled": row.get("stop_filled") or 0,
            "p_stop_limit": row.get("stop_limit"), "p_stop_order_id": row.get("stop_order_id"),
            "p_stop_status": row.get("stop_status") or "NONE",
            "p_exits_placed": bool(row.get("exits_placed")),
            "p_qty_open": row.get("qty_open") if row.get("qty_open") is not None else row.get("qty"),
        })
        return _rpc_reason(data, "relay_save_paper")

    async def record_close(self, underlying: str, strategy: str, mode: str, symbol: str,
                           alert_id: str, tp_status: Optional[str] = None, stop_status: Optional[str] = None,
                           tp_filled: Optional[int] = None, stop_filled: Optional[int] = None) -> Optional[str]:
        data = await self._rpc("relay_record_close", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode,
            "p_symbol": symbol, "p_alert_id": alert_id, "p_tp_status": tp_status,
            "p_stop_status": stop_status, "p_tp_filled": tp_filled, "p_stop_filled": stop_filled})
        return _rpc_reason(data, "relay_record_close")

    async def log_pick(self, alert_id: str, day: str, mode: str, selection: dict) -> None:
        if not self.configured:
            raise StoreError("store not configured")
        row = {
            "alert_id": alert_id, "day": day, "mode": mode,
            "action": selection.get("action"), "underlying": selection.get("underlying"),
            "strategy": selection.get("strategy"),
            "underlying_price": selection.get("underlying_price"),
            "underlying_price_source": selection.get("underlying_price_source"),
            "expiry": selection.get("expiry"), "candidates": selection.get("candidates") or [],
            "chosen_strike": selection.get("chosen_strike"), "chosen_symbol": selection.get("chosen_symbol"),
            "delta": selection.get("delta"), "bid": selection.get("bid"), "ask": selection.get("ask"),
            "mid": selection.get("mid"), "limit_price": selection.get("limit"),
            "limit_source": selection.get("limit_source"), "detail": selection,
        }
        try:
            response = await self.http.post("/contract_picks", json=row, headers={"Prefer": "return=minimal"})
        except httpx.HTTPError as exc:
            raise StoreError(type(exc).__name__) from exc
        if response.status_code >= 400:
            raise StoreError(f"supabase contract_picks -> {response.status_code}")

    async def ping(self) -> None:
        if not self.configured:
            raise StoreError("store not configured")
        r = await self.http.get("/daily_counters", params={"select": "day", "limit": "1"})
        if r.status_code >= 400:
            raise StoreError(f"supabase ping -> {r.status_code}")

    async def finish(self, alert_id: str, status: str, open_close: str = "", notional: Decimal = Decimal(0),
                     mode: str = "", detail: Any = None) -> None:
        try:
            r = await self.http.patch("/processed_alerts", params={"alert_id": f"eq.{alert_id}"},
                                      json={"status": status, "open_close": open_close or None,
                                            "notional": str(notional), "mode": mode,
                                            "detail": json.loads(json.dumps(redact(detail), default=str))})
            if r.status_code >= 400:
                jlog("store_finish_failed", alert_id=alert_id, status=r.status_code)
        except httpx.HTTPError as e:  # audit write only; never flip an already-decided outcome
            jlog("store_finish_failed", alert_id=alert_id, error=type(e).__name__)


# --------------------------------------------------------------------------- Public client
class PublicClient:
    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.s = settings
        self.http = httpx.AsyncClient(base_url=API_BASE, timeout=10.0, transport=transport)
        self._token: Optional[str] = None
        self._token_exp = 0.0
        self._account_id: Optional[str] = settings.account_id or None
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        async with self._lock:
            if self._token and time.time() < self._token_exp - TOKEN_REFRESH_MARGIN_S:
                return self._token
            if not self.s.public_secret:
                raise RuntimeError("PUBLIC_SECRET not set")
            r = await self.http.post(TOKEN_PATH, json={"validityInMinutes": TOKEN_VALIDITY_MIN,
                                                       "secret": self.s.public_secret})
            r.raise_for_status()
            self._token = r.json()["accessToken"]
            self._token_exp = time.time() + TOKEN_VALIDITY_MIN * 60
            jlog("token_refreshed")
            return self._token

    async def _req(self, method: str, path: str, body: Optional[dict] = None,
                   params: Optional[list[tuple[str, str]]] = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self.token()}"}
        r = await self.http.request(method, path, json=body, headers=headers, params=params)
        if r.status_code == 401:  # token revoked/expired early: refresh once and retry
            self._token = None
            headers = {"Authorization": f"Bearer {await self.token()}"}
            r = await self.http.request(method, path, json=body, headers=headers, params=params)
        return r

    async def account_id(self) -> str:
        if self._account_id:
            return self._account_id
        r = await self._req("GET", ACCOUNT_PATH)
        r.raise_for_status()
        accts = r.json().get("accounts", [])
        # Prefer an account that reports an options level; set PUBLIC_ACCOUNT_ID to be explicit.
        opt = [a for a in accts if a.get("optionsLevel") not in (None, "", "NONE")]
        chosen = (opt or accts or [None])[0]
        if not chosen:
            raise RuntimeError("no Public accounts returned")
        self._account_id = chosen["accountId"]
        jlog("account_selected", accountId=self._account_id, optionsLevel=chosen.get("optionsLevel"))
        return self._account_id

    async def preflight(self, body: dict) -> httpx.Response:
        acct = await self.account_id()
        return await self._req("POST", f"/userapigateway/trading/{acct}/preflight/single-leg", body)

    async def place(self, body: dict) -> httpx.Response:
        acct = await self.account_id()
        return await self._req("POST", f"/userapigateway/trading/{acct}/order", body)

    async def get_order(self, order_id: str) -> httpx.Response:
        acct = await self.account_id()
        return await self._req("GET", f"/userapigateway/trading/{acct}/order/{order_id}")

    async def _account(self, what: str) -> str:
        try:
            return await self.account_id()
        except (httpx.HTTPError, RuntimeError) as exc:
            jlog("public_call_failed", what=what, error=type(exc).__name__)
            raise Reject(f"{what} unreachable", 502) from exc

    async def option_chain(self, underlying: str, expiration: str) -> dict:
        account = await self._account("option chain")
        data = await public_json(self, "POST", f"/userapigateway/marketdata/{account}/option-chain", body={
            "instrument": {"symbol": underlying, "type": "EQUITY"},
            "expirationDate": expiration,
        }, what="option chain")
        if not isinstance(data, dict):
            raise Reject("option chain returned invalid JSON", 502)
        return data

    async def option_greeks(self, symbols: list[str]) -> Any:
        account = await self._account("option greeks")
        params = [("osiSymbols", symbol) for symbol in symbols]
        return await public_json(self, "GET", f"/userapigateway/option-details/{account}/greeks",
                                 params=params, what="option greeks")

    async def quotes(self, instruments: list[dict], what: str) -> Any:
        account = await self._account(what)
        return await public_json(self, "POST", f"/userapigateway/marketdata/{account}/quotes",
                                 body={"instruments": instruments}, what=what)


# --------------------------------------------------------------------------- validation
class Reject(Exception):
    def __init__(self, reason: str, status: int = 422):
        self.reason, self.status = reason, status


def validate(payload: dict, s: Settings) -> dict:
    sym = str(payload.get("symbol", "")).strip().upper().replace(" ", "")
    m = OCC_RE.match(sym)
    if not m:
        raise Reject(f"symbol must be an OCC option symbol like AAPL240216C00145000, got {sym!r}")
    underlying = m.group(1)
    if underlying not in s.allowlist:
        raise Reject(f"underlying {underlying} not in ALLOWLIST")
    side = str(payload.get("side", "")).upper()
    if side not in ("BUY", "SELL"):
        raise Reject("side must be BUY or SELL")
    oc = str(payload.get("open_close", "")).upper()
    if oc not in ("OPEN", "CLOSE"):
        raise Reject("open_close must be OPEN or CLOSE")
    if str(payload.get("order_type", "LIMIT")).upper() != "LIMIT":
        raise Reject("only LIMIT orders are allowed")
    try:
        price = Decimal(str(payload.get("limit_price")))
    except (InvalidOperation, TypeError):
        raise Reject("limit_price missing or not a number")
    if not price.is_finite() or price <= 0:
        raise Reject("limit_price must be > 0")
    price = price.quantize(Decimal("0.01"))
    # Opens only. A close or flatten is never blocked by this cap, for any strategy.
    if oc == "OPEN" and price > s.max_premium:
        raise Reject(f"limit_price {price} exceeds MAX_PREMIUM {s.max_premium}")
    try:
        qty_raw = Decimal(str(payload.get("qty")))
        if qty_raw != qty_raw.to_integral_value():
            raise ValueError
        qty = int(qty_raw)
    except (InvalidOperation, TypeError, ValueError):
        raise Reject("qty must be a whole number")
    if qty < 1 or qty > s.max_qty:
        raise Reject(f"qty {qty} outside 1..MAX_QTY({s.max_qty})")
    return {"symbol": sym, "underlying": underlying, "side": side, "open_close": oc,
            "limit_price": price, "qty": qty, "notional": price * qty * 100}


def in_hours(now: datetime) -> bool:
    # Weekdays 09:31-15:55 ET. Does NOT know exchange holidays/half days.
    return now.weekday() < 5 and MARKET_OPEN <= now.time() <= MARKET_CLOSE


def hours_error(now: datetime) -> str:
    return f"outside trading window 09:31-15:55 ET (now {now:%a %H:%M})"


def _never(value: Never) -> Never:
    raise RuntimeError(f"unhandled variant: {value!r}")


def as_et(now: datetime) -> datetime:
    if now.tzinfo is None:
        return now.replace(tzinfo=ET)
    return now.astimezone(ET)


def parse_decimal(value: Any) -> Optional[Decimal]:
    if value is None:
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


def positive_price(value: Any) -> Optional[Decimal]:
    number = parse_decimal(value)
    if number is None or number <= 0:
        return None
    return number


def cents(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def cents_or_none(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return cents(value)


def dec_text(value: Optional[Decimal], places: str = "0.0001") -> Optional[str]:
    if value is None:
        return None
    quantized = value.quantize(Decimal(places), rounding=ROUND_HALF_UP)
    text = format(quantized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def parse_occ(symbol: str) -> Optional[dict]:
    match = OCC_RE.match(symbol)
    if not match:
        return None
    yymmdd = match.group(2)
    try:
        expiry = date(2000 + int(yymmdd[:2]), int(yymmdd[2:4]), int(yymmdd[4:6]))
    except ValueError:
        return None
    return {
        "underlying": match.group(1),
        "expiry": expiry,
        "right": match.group(3),
        "strike": Decimal(match.group(4)) / Decimal(1000),
    }


def occ_symbol(underlying: str, expiry: date, right: str, strike: Decimal) -> str:
    millis = (strike * Decimal(1000)).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    if millis < 0 or millis > 99_999_999:
        raise Reject(f"strike {strike} cannot be encoded as OCC")
    return f"{underlying}{expiry:%y%m%d}{right}{int(millis):08d}"


def classify_symbol(raw: Any) -> tuple[Literal["occ", "underlying"], str]:
    symbol = str(raw or "").strip().upper().replace(" ", "")
    if OCC_RE.match(symbol):
        return "occ", symbol
    if UNDERLYING_RE.match(symbol):
        return "underlying", symbol
    raise Reject(f"symbol must be an OCC option symbol like AAPL240216C00145000, got {symbol!r}")


def strategy_name(payload: dict) -> str:
    raw = payload.get("strategy", "default")
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("id") or "default"
    name = str(raw).strip() if raw is not None else ""
    if not name:
        name = "default"
    if not STRATEGY_RE.match(name):
        raise Reject("strategy must be 1-64 characters (letters, digits, _ . : -)")
    return name


def normalize_position(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("", "none", "nan"):
        return None
    if text in ("flat", "0", "0.0"):
        return "flat"
    if text in ("long", "buy"):
        return "long"
    if text in ("short", "sell"):
        return "short"
    return text


def position_fields(payload: dict) -> tuple[Optional[str], Optional[str]]:
    prev = payload.get("prev_market_position")
    curr = payload.get("market_position")
    nested = payload.get("strategy")
    if isinstance(nested, dict):
        if prev is None:
            prev = nested.get("prev_market_position")
        if curr is None:
            curr = nested.get("market_position")
    return normalize_position(prev), normalize_position(curr)


def alert_side(payload: dict, required: bool) -> Optional[Literal["BUY", "SELL"]]:
    raw = payload.get("side")
    if raw is None or str(raw).strip() == "":
        if required:
            raise Reject("side must be BUY or SELL")
        return None
    side = str(raw).strip().upper()
    match side:
        case "BUY" | "SELL":
            return side
        case _:
            raise Reject("side must be BUY or SELL")


def decide_action(payload: dict, stored: Optional[dict]) -> tuple[Literal["open", "close"], str]:
    explicit = str(payload.get("open_close") or "").strip().upper()
    if explicit not in ("", "OPEN", "CLOSE"):
        raise Reject("open_close must be OPEN or CLOSE")
    prev, curr = position_fields(payload)
    closing = explicit == "CLOSE" or curr == "flat" or (
        prev in ("long", "short") and curr in ("long", "short") and prev != curr)
    side = alert_side(payload, required=not closing)
    if explicit == "CLOSE":
        return "close", "explicit_close"
    if curr == "flat":
        return "close", "flat"
    if prev in ("long", "short") and curr in ("long", "short") and prev != curr:
        return "close", "reversal"
    entry = stored.get("entry_side") if stored else None
    if side is not None and entry in ("BUY", "SELL") and entry != side:
        return "close", "opposite_side"
    if side is None:
        raise Reject("side must be BUY or SELL")
    if explicit == "OPEN":
        return "open", "explicit_open"
    if prev == "flat" and curr in ("long", "short"):
        return "open", "from_flat"
    return "open", "side"


def right_for_entry(side: Literal["BUY", "SELL"]) -> Literal["C", "P"]:
    match side:
        case "BUY":
            return "C"
        case "SELL":
            return "P"
        case _ as unreachable:
            _never(unreachable)


def itm_amount(strike: Decimal, spot: Decimal, right: Literal["C", "P"]) -> Decimal:
    match right:
        case "C":
            return spot - strike
        case "P":
            return strike - spot
        case _ as unreachable:
            _never(unreachable)


def in_window(strike: Decimal, spot: Decimal, right: Literal["C", "P"],
              itm_points: Decimal = ITM_POINTS) -> bool:
    amount = itm_amount(strike, spot, right)
    return Decimal(0) <= amount <= itm_points


def band_distance(absolute_delta: Decimal, lo: Decimal = DELTA_LO, hi: Decimal = DELTA_HI) -> Decimal:
    if absolute_delta < lo:
        return lo - absolute_delta
    if absolute_delta > hi:
        return absolute_delta - hi
    return Decimal(0)


@dataclass
class Leg:
    symbol: str
    strike: Decimal
    delta: Optional[Decimal]
    delta_source: Optional[str]
    bid: Optional[Decimal]
    ask: Optional[Decimal]


def quote_mid(bid: Optional[Decimal], ask: Optional[Decimal]) -> Optional[Decimal]:
    if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
        return None
    mid = ((bid + ask) / 2).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if mid <= 0:
        return None
    return mid


def limit_from_market(bid: Optional[Decimal], ask: Optional[Decimal],
                      fallback: LimitFallback) -> tuple[Decimal, str]:
    """Prefer the bid/ask mid. Otherwise use the configured side, or reject when that side is 'reject'."""
    mid = quote_mid(bid, ask)
    if mid is not None:
        return mid, "mid"
    match fallback:
        case "reject":
            raise Reject("no mid for the chosen contract")
        case "ask":
            raw, label = ask, "ask"
        case "bid":
            raw, label = bid, "bid"
        case _ as unreachable:
            _never(unreachable)
    if raw is None or raw <= 0:
        raise Reject(f"no mid or {label} for the chosen contract")
    price = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if price <= 0:
        raise Reject(f"no usable {label} for the chosen contract")
    return price, fallback


def choose_leg(legs: list[Leg], spot: Decimal, band_lo: Decimal = DELTA_LO,
               band_hi: Decimal = DELTA_HI) -> Leg:
    """|delta| closest to the band, then to the band midpoint, then to spot."""
    usable = [leg for leg in legs if leg.delta is not None]
    if not usable:
        raise Reject("no delta for strikes in the ITM window")
    midpoint = (band_lo + band_hi) / 2

    def rank(leg: Leg) -> tuple:
        if leg.delta is None:
            raise Reject("no delta for strikes in the ITM window")
        absolute = abs(leg.delta)
        return (band_distance(absolute, band_lo, band_hi), abs(absolute - midpoint),
                abs(leg.strike - spot), leg.strike, leg.symbol)

    return min(usable, key=rank)


def candidate_view(leg: Leg) -> dict:
    return {
        "symbol": leg.symbol,
        "strike": dec_text(leg.strike),
        "delta": dec_text(leg.delta, "0.000001"),
        "delta_source": leg.delta_source,
        "bid": cents_or_none(leg.bid),
        "ask": cents_or_none(leg.ask),
        "mid": cents_or_none(quote_mid(leg.bid, leg.ask)),
    }


def alert_underlying_price(payload: dict) -> Optional[Decimal]:
    if "underlying_price" not in payload or payload.get("underlying_price") in (None, ""):
        return None
    price = positive_price(payload.get("underlying_price"))
    if price is None:
        raise Reject("underlying_price must be > 0")
    return price


def whole_qty(value: Any, allow_zero: bool = False) -> int:
    try:
        number = Decimal(str(value))
        if number != number.to_integral_value():
            raise ValueError
        qty = int(number)
    except (InvalidOperation, TypeError, ValueError):
        raise Reject("stored position qty is invalid", 502)
    if qty < (0 if allow_zero else 1):
        raise Reject("stored position qty is invalid", 502)
    return qty


def take_profit_qty(qty: int, fraction: Decimal, single: bool) -> int:
    """Contracts to sell at the take-profit. Qty 1 stays a runner unless TP_SINGLE is set."""
    if qty < 1:
        return 0
    if qty == 1:
        return 1 if single else 0
    if fraction <= 0:
        return 0
    contracts = int((Decimal(qty) * fraction).to_integral_value(rounding=ROUND_DOWN))
    if contracts < 1:
        contracts = 1
    if contracts > qty:
        contracts = qty
    return contracts


def option_tick(price: Decimal, penny: bool = False) -> Decimal:
    """$0.01 under $3, and at every price in the penny program. Otherwise $0.05 at and above $3."""
    if penny or price < Decimal("3"):
        return Decimal("0.01")
    return Decimal("0.05")


def round_option_price(price: Decimal, penny: bool = False) -> Decimal:
    """Nearest valid option tick. 1.015 under $3 is 1.02. A penny-program contract stays on $0.01."""
    if not price.is_finite() or price <= 0:
        raise Reject("option price must be > 0")
    tick = option_tick(price, penny)
    units = (price / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    rounded = (units * tick).quantize(tick)
    if rounded <= 0:
        raise Reject("option price must be > 0")
    return rounded


def take_profit_limit(entry: Decimal, percent: Decimal, penny: bool = False) -> Decimal:
    """Limit = entry * (1 + TP_PERCENT/100), rounded to the option tick. Not capped by MAX_PREMIUM."""
    raw = entry * (Decimal(1) + percent / Decimal(100))
    return round_option_price(raw, penny)


def stop_price(entry: Decimal, percent: Decimal, penny: bool = False) -> Decimal:
    """Stop = entry * (1 - STOP_LOSS_PERCENT/100), rounded to the option tick.

    1.45 with 30 percent is 1.015, which rounds to 1.02 because the tick under $3 is $0.01.
    """
    raw = entry * (Decimal(1) - percent / Decimal(100))
    return round_option_price(raw, penny)


def price_text(value: Decimal) -> str:
    """At least two decimals, up to three, so 1.015 stays 1.015 and 1.020 is 1.02."""
    text = f"{value.quantize(Decimal('0.001'), rounding=ROUND_HALF_UP):.3f}"
    if text.endswith("0"):
        text = text[:-1]
    return text


def no_take_profit_reason(qty: int, fraction: Decimal) -> str:
    if qty < 2:
        return "single contract closes on the flatten alert; set TP_SINGLE to take profit that contract"
    if fraction <= 0:
        return "TP_FRACTION is 0"
    return "no take-profit quantity"


def _count_field(stored: dict, key: str) -> int:
    raw = stored.get(key)
    if raw in (None, ""):
        return 0
    return whole_qty(raw, allow_zero=True)


def clamp_fills(opened: int, tp_filled: int, stop_filled: int) -> tuple[int, int]:
    """Fills are of one position. Cap them so a later sell cannot close more than was opened."""
    tp_filled = min(max(tp_filled, 0), opened)
    stop_filled = min(max(stop_filled, 0), opened - tp_filled)
    return tp_filled, stop_filled


def stored_exit(stored: dict, prefix: str) -> tuple[str, int, int]:
    """Status, ordered quantity, and filled quantity for the take-profit or the stop."""
    status = str(stored.get(f"{prefix}_status") or "NONE").upper()
    ordered = _count_field(stored, f"{prefix}_qty")
    filled = _count_field(stored, f"{prefix}_filled")
    match status:
        case "NONE":
            return "NONE", 0, 0
        case "RESTING":
            return "RESTING", ordered, min(filled, ordered)
        case "PARTIAL":
            if ordered < 1 or filled <= 0 or filled >= ordered:
                raise Reject(f"{prefix} partial fill needs a filled quantity below the order size", 502)
            return "PARTIAL", ordered, filled
        case "FILLED":
            return "FILLED", ordered, ordered if filled == 0 else min(filled, ordered)
        case "CANCELLED":
            return "CANCELLED", ordered, min(filled, ordered)
        case _:
            raise Reject(f"unrecognized {prefix} status {status!r}", 502)


def broker_fill(payload: dict, ordered: int) -> tuple[str, int]:
    """Map a Public.com order payload to our status and a filled quantity that cannot exceed the order."""
    status = str(payload.get("status") or "").upper()
    raw = payload.get("filledQuantity")
    filled = 0 if raw in (None, "") else whole_qty(raw, allow_zero=True)
    filled = min(max(filled, 0), ordered)
    match status:
        case "FILLED":
            return "FILLED", ordered if filled == 0 else filled
        case "PARTIALLY_FILLED":
            if filled <= 0:
                raise Reject("partial fill is missing filledQuantity", 502)
            if filled >= ordered:
                return "FILLED", ordered
            return "PARTIAL", filled
        case "NEW" | "PENDING_REPLACE" | "PENDING_CANCEL":
            return "RESTING", filled
        case "CANCELLED" | "QUEUED_CANCELLED" | "EXPIRED" | "REJECTED" | "REPLACED":
            if ordered > 0 and filled >= ordered:
                return "FILLED", ordered
            if filled > 0:
                return "PARTIAL", filled
            return "CANCELLED", 0
        case _:
            raise Reject(f"unrecognized order status {status!r}", 502)


def reduced_stop_qty(opened: int, tp_filled: int, stop_ordered: int, stop_filled: int,
                     stop_status: str) -> Optional[int]:
    """After a take-profit fill, the stop may cover only the contracts still open."""
    if stop_status not in ("RESTING", "PARTIAL") or tp_filled <= 0:
        return None
    working = max(0, stop_ordered - stop_filled)
    still = max(0, opened - tp_filled - stop_filled)
    reduced = min(working, still)
    if reduced < working:
        return reduced
    return None


def plan_opened_qty(stored: dict) -> int:
    """Contracts a flatten can sell. A paper entry that has not filled contributes only its fills."""
    status = str(stored.get("entry_status") or "NONE").upper()
    if status == "NONE":
        return whole_qty(stored.get("qty"))
    filled = _count_field(stored, "entry_filled")
    if status == "FILLED" and filled < 1:
        return whole_qty(stored.get("qty"))
    return filled


def empty_exit_plan(stored: dict) -> dict:
    """Nothing has filled. Cancel a resting take-profit or stop so a later fill cannot oversell."""
    tp_status, tp_ordered, tp_filled = stored_exit(stored, "tp")
    stop_status, stop_ordered, stop_filled = stored_exit(stored, "stop")
    unfilled_tp = max(0, tp_ordered - tp_filled) if tp_status in ("RESTING", "PARTIAL") else 0
    unfilled_stop = max(0, stop_ordered - stop_filled) if stop_status in ("RESTING", "PARTIAL") else 0
    cancel_tp = None
    if unfilled_tp > 0 and str(stored.get("tp_order_id") or "").strip():
        cancel_tp = {
            "orderId": str(stored.get("tp_order_id")).strip(),
            "quantity": unfilled_tp,
            "reason": "unfilled",
            "note": "cancelled so it cannot sell contracts that are already closed",
        }
    cancel_stop = None
    if unfilled_stop > 0 and str(stored.get("stop_order_id") or "").strip():
        cancel_stop = {
            "orderId": str(stored.get("stop_order_id")).strip(),
            "quantity": unfilled_stop,
            "reason": "flatten",
            "note": "cancelled by flatten",
        }
    return {
        "opened_qty": 0,
        "still_open": 0,
        "runners": 0,
        "take_profit_contracts_closed": 0,
        "tp_filled": tp_filled,
        "stop_filled": stop_filled,
        "tp_status_after": "CANCELLED" if cancel_tp else tp_status,
        "stop_status_after": "CANCELLED" if cancel_stop else stop_status,
        "cancel_take_profit": cancel_tp,
        "cancel_stop": cancel_stop,
        "stop_reduced_to": None,
        "note": flatten_note(0, cancel_tp, cancel_stop, None),
    }


def exit_plan(stored: dict) -> dict:
    """What a flatten must cancel and how many contracts are still open.

    A resting take-profit has not closed anything. Those contracts are sold with the runners.
    A fill, including a partial fill, reduces the sell to what remains. Fills are clamped so
    the sell cannot exceed the opened quantity.
    """
    opened = plan_opened_qty(stored)
    if opened < 1:
        return empty_exit_plan(stored)
    tp_status, tp_ordered, tp_filled = stored_exit(stored, "tp")
    stop_status, stop_ordered, stop_filled = stored_exit(stored, "stop")
    tp_filled, stop_filled = clamp_fills(opened, tp_filled, stop_filled)
    still = opened - tp_filled - stop_filled
    unfilled_tp = max(0, tp_ordered - tp_filled) if tp_status in ("RESTING", "PARTIAL") else 0
    unfilled_stop = max(0, stop_ordered - stop_filled) if stop_status in ("RESTING", "PARTIAL") else 0
    included_tp = min(unfilled_tp, still)
    runners = still - included_tp
    reduced = reduced_stop_qty(opened, tp_filled, stop_ordered, stop_filled, stop_status)
    cancel_tp = None
    if unfilled_tp > 0 and str(stored.get("tp_order_id") or "").strip():
        cancel_tp = {
            "orderId": str(stored.get("tp_order_id")).strip(),
            "quantity": unfilled_tp,
            "reason": "partial" if tp_filled else "unfilled",
            "note": ("unfilled contracts are included in this sell-to-close" if included_tp
                     else "cancelled so it cannot sell contracts that are already closed"),
        }
    cancel_stop = None
    if unfilled_stop > 0 and str(stored.get("stop_order_id") or "").strip():
        cancel_stop = {
            "orderId": str(stored.get("stop_order_id")).strip(),
            "quantity": unfilled_stop,
            "reason": "flatten",
            "note": "cancelled by flatten",
        }
        if reduced is not None:
            cancel_stop["working_quantity"] = reduced
            cancel_stop["note"] = "take-profit fills reduced the stop to the runners; flatten cancels it"
    tp_after = "CANCELLED" if cancel_tp else tp_status
    stop_after = "CANCELLED" if cancel_stop else stop_status
    return {
        "opened_qty": opened,
        "still_open": still,
        "runners": runners,
        "take_profit_contracts_closed": included_tp,
        "tp_filled": tp_filled,
        "stop_filled": stop_filled,
        "tp_status_after": tp_after,
        "stop_status_after": stop_after,
        "cancel_take_profit": cancel_tp,
        "cancel_stop": cancel_stop,
        "stop_reduced_to": reduced,
        "note": flatten_note(still, cancel_tp, cancel_stop, reduced),
    }


def flatten_note(still: int, cancel_tp: Optional[dict], cancel_stop: Optional[dict],
                 reduced: Optional[int]) -> str:
    if still == 0:
        if cancel_tp:
            return ("Nothing remains open. The resting take-profit is cancelled so it cannot sell "
                    "contracts that are already closed.")
        if cancel_stop:
            return "Nothing remains open. The resting stop is cancelled."
        return "Nothing remains open, so no sell is sent."
    sentences = []
    if cancel_tp:
        sentences.append("Resting take-profit is cancelled. Its unfilled contracts are included in this sell-to-close.")
    if reduced is not None:
        sentences.append("Take-profit fills reduced the stop to the remaining runners.")
    if cancel_stop:
        sentences.append("Resting stop is cancelled.")
    return " ".join(sentences) if sentences else "Sell-to-close the contracts still open."


async def public_json(client: "PublicClient", method: str, path: str, body: Optional[dict] = None,
                      params: Optional[list[tuple[str, str]]] = None, what: str = "Public") -> Any:
    try:
        response = await client._req(method, path, body, params)
    except (httpx.HTTPError, RuntimeError) as exc:
        jlog("public_call_failed", what=what, error=type(exc).__name__)
        raise Reject(f"{what} unreachable", 502) from exc
    if response.status_code >= 400:
        raise Reject(f"{what} failed ({response.status_code})", 502)
    try:
        return response.json()
    except ValueError as exc:
        raise Reject(f"{what} returned invalid JSON", 502) from exc


def first_quote(data: Any, what: str) -> dict:
    if not isinstance(data, dict):
        raise Reject(f"{what} returned invalid JSON", 502)
    quotes = data.get("quotes")
    if not isinstance(quotes, list) or not quotes or not isinstance(quotes[0], dict):
        raise Reject(f"{what} unavailable", 502)
    quote = quotes[0]
    if str(quote.get("outcome") or "SUCCESS").upper() != "SUCCESS":
        raise Reject(f"{what} unavailable", 502)
    return quote


def price_from_quote(data: Any, what: str) -> Decimal:
    quote = first_quote(data, what)
    last = positive_price(quote.get("last"))
    if last is not None:
        return last
    bid = positive_price(quote.get("bid"))
    ask = positive_price(quote.get("ask"))
    mid = quote_mid(bid, ask)
    if mid is not None:
        return mid
    raise Reject(f"{what} has no price", 502)


def parse_greeks_payload(data: Any) -> dict[str, Decimal]:
    if not isinstance(data, dict):
        return {}
    rows = data.get("greeks")
    if not isinstance(rows, list):
        return {}
    found: dict[str, Decimal] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        symbol = str(row.get("symbol") or "").upper().replace(" ", "")
        nested = row.get("greeks") if isinstance(row.get("greeks"), dict) else row
        delta = parse_decimal(nested.get("delta") if isinstance(nested, dict) else None)
        if symbol and delta is not None:
            found[symbol] = delta
    return found


async def fetch_greeks_map(client: "PublicClient", symbols: list[str]) -> dict[str, Decimal]:
    if not symbols:
        return {}
    merged: dict[str, Decimal] = {}
    try:
        for offset in range(0, len(symbols), 250):
            payload = await client.option_greeks(symbols[offset:offset + 250])
            merged.update(parse_greeks_payload(payload))
    except Reject as exc:
        jlog("greeks_unavailable", error=exc.reason)
        return merged
    return merged


def leg_from_row(row: Any, underlying: str, expiry: date, right: Literal["C", "P"]) -> Optional[Leg]:
    if not isinstance(row, dict):
        return None
    if str(row.get("outcome") or "SUCCESS").upper() != "SUCCESS":
        return None
    instrument = row.get("instrument") if isinstance(row.get("instrument"), dict) else {}
    raw_symbol = str(instrument.get("symbol") or "").upper().replace(" ", "")
    parsed = parse_occ(raw_symbol)
    details = row.get("optionDetails") if isinstance(row.get("optionDetails"), dict) else {}
    if parsed:
        # A real OCC symbol for a different expiry or right is not this chain slice.
        if parsed["underlying"] != underlying or parsed["right"] != right or parsed["expiry"] != expiry:
            return None
        symbol = raw_symbol
        strike = parsed["strike"]
    else:
        strike = positive_price(details.get("strikePrice"))
        if strike is None:
            return None
        symbol = occ_symbol(underlying, expiry, right, strike)
    greeks = details.get("greeks") if isinstance(details.get("greeks"), dict) else {}
    delta = parse_decimal(greeks.get("delta"))
    return Leg(symbol, strike, delta, "chain" if delta is not None else None,
               positive_price(row.get("bid")), positive_price(row.get("ask")))


def legs_from_chain(chain: dict, underlying: str, expiry: date, right: Literal["C", "P"]) -> list[Leg]:
    base = str(chain.get("baseSymbol") or "").upper()
    if base and base != underlying:
        raise Reject(f"option chain baseSymbol {base} != {underlying}", 502)
    key = "calls" if right == "C" else "puts"
    rows = chain.get(key)
    if not isinstance(rows, list):
        raise Reject(f"option chain missing {key}", 502)
    legs = []
    for row in rows:
        leg = leg_from_row(row, underlying, expiry, right)
        if leg is not None:
            legs.append(leg)
    return legs


def with_greeks(leg: Leg, greeks: dict[str, Decimal]) -> Leg:
    if leg.symbol not in greeks:
        return leg
    return Leg(leg.symbol, leg.strike, greeks[leg.symbol], "greeks", leg.bid, leg.ask)


def selection_from_leg(action: str, underlying: str, strategy: str, entry_side: str, order_side: str,
                       open_close: str, spot: Optional[Decimal], spot_source: Optional[str], expiry: date,
                       right: str, legs: list[Leg], chosen: Leg, limit: Decimal, limit_source: str,
                       qty: Any, intent: str) -> dict:
    return {
        "action": action,
        "intent": intent,
        "underlying": underlying,
        "strategy": strategy,
        "entry_side": entry_side,
        "order_side": order_side,
        "open_close": open_close,
        "underlying_price": dec_text(spot),
        "underlying_price_source": spot_source,
        "expiry": expiry.isoformat(),
        "right": right,
        "candidates": [candidate_view(leg) for leg in legs],
        "chosen_symbol": chosen.symbol,
        "chosen_strike": dec_text(chosen.strike),
        "delta": dec_text(chosen.delta, "0.000001"),
        "delta_source": chosen.delta_source,
        "bid": cents_or_none(chosen.bid),
        "ask": cents_or_none(chosen.ask),
        "mid": cents_or_none(quote_mid(chosen.bid, chosen.ask)),
        "limit": cents(limit),
        "limit_source": limit_source,
        "qty": qty,
    }


async def equity_spot(client: "PublicClient", underlying: str) -> Decimal:
    data = await client.quotes([{"symbol": underlying, "type": "EQUITY"}], what="underlying quote")
    return price_from_quote(data, "underlying quote")


async def build_open(payload: dict, underlying: str, strategy: str, client: "PublicClient",
                     s: "Settings", now: datetime, intent: str) -> tuple[dict, dict]:
    side = alert_side(payload, required=True)
    if side is None:
        raise Reject("side must be BUY or SELL")
    right = right_for_entry(side)
    alert_px = alert_underlying_price(payload)
    if alert_px is not None:
        spot, spot_source = alert_px, "alert"
    else:
        spot, spot_source = await equity_spot(client, underlying), "quote"
    expiry = as_et(now).date() + timedelta(days=s.expiry_days)
    chain = await client.option_chain(underlying, expiry.isoformat())
    window = [leg for leg in legs_from_chain(chain, underlying, expiry, right)
              if in_window(leg.strike, spot, right, s.itm_points)]
    window.sort(key=lambda leg: (leg.strike, leg.symbol))
    if not window:
        noun = "call" if right == "C" else "put"
        points = format(s.itm_points, "f")
        raise Reject(
            f"no {underlying} {noun} strikes from ATM {dec_text(spot)} to {points} points ITM for {expiry.isoformat()}")
    greeks = await fetch_greeks_map(client, [leg.symbol for leg in window])
    window = [with_greeks(leg, greeks) for leg in window]
    chosen = choose_leg(window, spot, s.delta_band_min, s.delta_band_max)
    limit, limit_source = limit_from_market(chosen.bid, chosen.ask, s.open_limit_fallback)
    order_payload = {
        "symbol": chosen.symbol, "side": "BUY", "open_close": "OPEN",
        "limit_price": cents(limit), "qty": payload.get("qty"),
    }
    selection = selection_from_leg(
        "open", underlying, strategy, side, "BUY", "OPEN", spot, spot_source, expiry, right,
        window, chosen, limit, limit_source, payload.get("qty"), intent)
    return order_payload, selection


async def reconcile_exits(stored: dict, client: "PublicClient", s: "Settings") -> dict:
    """In LIVE, overlay Public.com fill status onto the stored take-profit and stop.

    PREFLIGHT never submitted those orders, so their ids are not queried. A 404 means the id
    was never accepted at the broker; stored fills stay as they are. Any other failure refuses
    the flatten rather than guessing a quantity.
    """
    snapshot = dict(stored)
    if s.mode != "LIVE":
        return snapshot
    for prefix in ("tp", "stop"):
        status = str(snapshot.get(f"{prefix}_status") or "NONE").upper()
        order_id = str(snapshot.get(f"{prefix}_order_id") or "").strip()
        if status not in ("RESTING", "PARTIAL", "FILLED") or not order_id:
            continue
        try:
            response = await client.get_order(order_id)
        except (httpx.HTTPError, RuntimeError) as exc:
            jlog("exit_status_unreachable", kind=prefix, error=type(exc).__name__)
            raise Reject("order status unreachable", 502) from exc
        if response.status_code == 404:
            jlog("exit_not_at_broker", kind=prefix, orderId=order_id)
            continue
        if response.status_code >= 400:
            raise Reject(f"order status failed ({response.status_code})", 502)
        try:
            payload = response.json()
        except ValueError as exc:
            raise Reject("order status returned invalid JSON", 502) from exc
        if not isinstance(payload, dict):
            raise Reject("order status returned invalid JSON", 502)
        ordered = _count_field(snapshot, f"{prefix}_qty")
        new_status, filled = broker_fill(payload, ordered)
        snapshot[f"{prefix}_status"] = new_status
        snapshot[f"{prefix}_filled"] = filled
        jlog("exit_reconciled", kind=prefix, orderId=order_id, status=new_status, filled=filled)
    return snapshot


def _close_selection(plan: dict, underlying: str, strategy: str, stored: dict, symbol: str,
                     parsed: dict, strike: Decimal, spot: Optional[Decimal], intent: str,
                     chosen: Optional[Leg], limit: Optional[Decimal], limit_source: Optional[str],
                     legs: list[Leg]) -> dict:
    if chosen is None or limit is None:
        selection = {
            "action": "close", "intent": intent, "underlying": underlying, "strategy": strategy,
            "entry_side": str(stored.get("entry_side") or ""), "order_side": "SELL", "open_close": "CLOSE",
            "underlying_price": dec_text(spot), "underlying_price_source": "alert" if spot is not None else None,
            "expiry": parsed["expiry"].isoformat(), "right": parsed["right"], "candidates": [],
            "chosen_symbol": symbol, "chosen_strike": dec_text(strike), "delta": None, "delta_source": None,
            "bid": None, "ask": None, "mid": None, "limit": None, "limit_source": None,
            "qty": 0,
        }
    else:
        selection = selection_from_leg(
            "close", underlying, strategy, str(stored.get("entry_side") or ""), "SELL", "CLOSE",
            spot, "alert" if spot is not None else None, parsed["expiry"], parsed["right"],
            legs, chosen, limit, limit_source or "", plan["still_open"], intent)
    selection.update({
        "opened_qty": plan["opened_qty"],
        "take_profit_qty": _count_field(stored, "tp_qty"),
        "take_profit_filled": plan["tp_filled"],
        "stop_qty": _count_field(stored, "stop_qty"),
        "stop_filled": plan["stop_filled"],
        "runners_closed": plan["runners"],
        "contracts_closed": plan["still_open"],
        "take_profit_contracts_closed": plan["take_profit_contracts_closed"],
        "tp_status_after": plan["tp_status_after"],
        "stop_status_after": plan["stop_status_after"],
        "tp_filled": plan["tp_filled"],
        "note": plan["note"],
    })
    if plan["still_open"] < 1:
        selection["skip_order"] = True
        selection["qty"] = 0
    if plan["cancel_take_profit"] is not None:
        selection["cancel_take_profit"] = plan["cancel_take_profit"]
    if plan["cancel_stop"] is not None:
        selection["cancel_stop"] = plan["cancel_stop"]
    if plan["stop_reduced_to"] is not None:
        selection["stop_reduced_to"] = plan["stop_reduced_to"]
    return selection


async def build_close(payload: dict, underlying: str, strategy: str, client: "PublicClient",
                      s: "Settings", stored: dict, intent: str) -> tuple[dict, dict]:
    symbol = str(stored.get("symbol") or "").upper().replace(" ", "")
    parsed = parse_occ(symbol)
    if not parsed or parsed["underlying"] != underlying:
        raise Reject(f"stored contract {symbol!r} does not match {underlying}", 502)
    snapshot = await reconcile_exits(stored, client, s)
    plan = exit_plan(snapshot)
    strike = parse_decimal(snapshot.get("strike")) or parsed["strike"]
    spot = alert_underlying_price(payload)
    if plan["still_open"] < 1:
        selection = _close_selection(plan, underlying, strategy, snapshot, symbol, parsed, strike, spot,
                                     intent, None, None, None, [])
        jlog("flatten", alert_id=payload.get("alert_id"), contracts_closed=0, note=plan["note"],
             cancel_take_profit=plan["cancel_take_profit"], cancel_stop=plan["cancel_stop"])
        return {"symbol": symbol, "side": "SELL", "open_close": "CLOSE", "qty": 0}, selection
    quoted = await client.quotes([{"symbol": symbol, "type": "OPTION"}], what="option quote")
    quote = first_quote(quoted, "option quote")
    bid = positive_price(quote.get("bid"))
    ask = positive_price(quote.get("ask"))
    greeks = await fetch_greeks_map(client, [symbol])
    delta = greeks.get(symbol)
    delta_source = "greeks" if symbol in greeks else None
    if delta is None:
        details = quote.get("optionDetails") if isinstance(quote.get("optionDetails"), dict) else {}
        nested = details.get("greeks") if isinstance(details.get("greeks"), dict) else {}
        delta = parse_decimal(nested.get("delta"))
        if delta is not None:
            delta_source = "quote"
    limit, limit_source = limit_from_market(bid, ask, s.close_limit_fallback)
    chosen = Leg(symbol, strike, delta, delta_source, bid, ask)
    order_payload = {
        "symbol": symbol, "side": "SELL", "open_close": "CLOSE",
        "limit_price": cents(limit), "qty": plan["still_open"],
    }
    selection = _close_selection(plan, underlying, strategy, snapshot, symbol, parsed, strike, spot,
                                 intent, chosen, limit, limit_source, [chosen])
    jlog("flatten", alert_id=payload.get("alert_id"), contracts_closed=plan["still_open"],
         runners_closed=plan["runners"], take_profit_contracts_closed=plan["take_profit_contracts_closed"],
         cancel_take_profit=plan["cancel_take_profit"], cancel_stop=plan["cancel_stop"], note=plan["note"])
    return order_payload, selection


async def prepare_underlying(payload: dict, underlying: str, s: "Settings", client: "PublicClient",
                             store: "Store", now: datetime) -> tuple[dict, dict]:
    if underlying not in s.allowlist:
        raise Reject(f"underlying {underlying} not in ALLOWLIST")
    if s.enforce_hours and not in_hours(now):
        raise Reject(hours_error(now))
    strategy = strategy_name(payload)
    stored = await store.get_position(underlying, strategy, s.mode)
    action, intent = decide_action(payload, stored)
    match action:
        case "open":
            if stored:
                raise Reject(f"position already open for {underlying}/{strategy} ({stored.get('symbol')})")
            return await build_open(payload, underlying, strategy, client, s, now, intent)
        case "close":
            if not stored:
                raise Reject(f"no open position for {underlying}/{strategy}")
            return await build_close(payload, underlying, strategy, client, s, stored, intent)
        case _ as unreachable:
            _never(unreachable)


async def prepare_order(payload: dict, s: "Settings", client: "PublicClient", store: "Store",
                        now: datetime) -> tuple[dict, Optional[dict]]:
    kind, symbol = classify_symbol(payload.get("symbol", ""))
    match kind:
        case "occ":
            return payload, None
        case "underlying":
            return await prepare_underlying(payload, symbol, s, client, store, now)
        case _ as unreachable:
            _never(unreachable)


async def book_position(store: "Store", selection: dict, order: dict, mode: str, alert_id: str,
                        take_profit: Optional[dict] = None, stop: Optional[dict] = None) -> Optional[str]:
    action = selection.get("action")
    if action == "open":
        tp = take_profit or {}
        held = stop or {}
        attached = bool(tp.get("attached"))
        stop_on = bool(held.get("attached"))
        qty = int(order["qty"])
        entry_limit = cents(order["limit_price"]) if isinstance(order["limit_price"], Decimal) else str(order["limit_price"])
        return await store.record_open(
            str(selection["underlying"]), str(selection["strategy"]), mode, order["symbol"],
            str(selection["entry_side"]), str(selection["right"]), qty,
            str(selection["chosen_strike"]), alert_id, qty_open=qty,
            tp_qty=int(tp.get("quantity") or 0) if attached else 0,
            tp_limit=tp.get("limit_price") if attached else None,
            tp_order_id=tp.get("orderId") if attached else None,
            tp_status="RESTING" if attached else "NONE",
            entry_limit=entry_limit, tp_filled=0,
            stop_qty=int(held.get("quantity") or 0) if stop_on else 0,
            stop_filled=0, stop_limit=held.get("stop_price") if stop_on else None,
            stop_order_id=held.get("orderId") if stop_on else None,
            stop_status="RESTING" if stop_on else "NONE")
    if action == "close":
        return await store.record_close(
            str(selection["underlying"]), str(selection["strategy"]), mode,
            str(selection.get("chosen_symbol") or order["symbol"]), alert_id,
            tp_status=selection.get("tp_status_after"), stop_status=selection.get("stop_status_after"),
            tp_filled=selection.get("tp_filled"), stop_filled=selection.get("stop_filled"))
    return f"unhandled selection action {action!r}"


def cap_slot(day: str, mode: str, notional: Decimal, is_open: bool, is_exit: bool,
             exit_counts: bool) -> tuple:
    return (day, mode, notional, is_open, is_exit, exit_counts and is_exit)


async def plan_take_profit(order: dict, s: "Settings", client: "PublicClient", store: "Store",
                           day: str) -> tuple[dict, Optional[tuple]]:
    """After an open is accepted, report the partial sell-to-close. Returns (report, cap slot).

    The slot is set only when a resting take-profit is holding an exit reservation. Cap or
    preflight failures release that reservation here and leave the entry without a take-profit.
    This never calls place(), including when MODE is LIVE. MAX_PREMIUM does not apply.
    """
    qty = int(order["qty"])
    entry = order["limit_price"] if isinstance(order["limit_price"], Decimal) else Decimal(str(order["limit_price"]))
    tp_qty = take_profit_qty(qty, s.tp_fraction, s.tp_single)
    if tp_qty < 1:
        reason = no_take_profit_reason(qty, s.tp_fraction)
        report = {"attached": False, "quantity": 0, "qty_open": qty, "runners": qty,
                  "status": "NONE", "reason": reason}
        jlog("take_profit", **report)
        return report, None
    limit = take_profit_limit(entry, s.tp_percent)
    tp_order = {"symbol": order["symbol"], "side": "SELL", "open_close": "CLOSE",
                "limit_price": limit, "qty": tp_qty}
    body = order_body(tp_order)
    try:
        reason = await store.try_consume(day, s.mode, Decimal(0), False, s, is_exit=True)
    except (StoreError, httpx.HTTPError) as exc:
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "runners": qty - tp_qty,
                  "status": "NONE", "limit_price": cents(limit), "order": body,
                  "reason": f"state store unavailable: {type(exc).__name__}"}
        jlog("take_profit", **report)
        return report, None
    if reason:
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "runners": qty - tp_qty,
                  "status": "NONE", "limit_price": cents(limit), "order": body, "reason": reason}
        jlog("take_profit", **report)
        return report, None
    slot = cap_slot(day, s.mode, Decimal(0), False, True, s.exits_count_toward_order_cap)
    try:
        pf = await client.preflight(body)
        pf_body = _json(pf)
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        await _release_quiet(store, slot)
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "runners": qty - tp_qty,
                  "status": "NONE", "limit_price": cents(limit), "order": body,
                  "reason": f"upstream error: {type(exc).__name__}"}
        jlog("take_profit", **report)
        return report, None
    jlog("take_profit", status=pf.status_code, request=body, response=pf_body, quantity=tp_qty)
    if pf.status_code >= 400:
        await _release_quiet(store, slot)
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "runners": qty - tp_qty,
                  "status": "NONE", "limit_price": cents(limit), "order": body, "reason": "preflight failed",
                  "preflight_status": pf.status_code, "preflight": pf_body}
        return report, None
    report = {
        "attached": True, "status": "RESTING", "quantity": tp_qty, "qty_open": qty,
        "runners": qty - tp_qty, "limit_price": cents(limit), "orderId": str(uuid.uuid4()),
        "order": body, "preflight": pf_body, "note": "reported only; not sent to /order",
    }
    jlog("take_profit", attached=True, quantity=tp_qty, qty_open=qty, runners=report["runners"],
         limit_price=report["limit_price"], orderId=report["orderId"], order=body)
    return report, slot


async def _release_quiet(store: "Store", slot: tuple) -> None:
    try:
        await store.release(*slot)
    except (StoreError, httpx.HTTPError):
        jlog("release_failed", slot="exit")


def stop_order_body(symbol: str, qty: int, price: Decimal) -> dict:
    """STOP_LIMIT sell-to-close. Limit equals the stop so a wide 0DTE spread cannot sweep through."""
    text = price_text(price)
    return {
        "instrument": {"symbol": symbol, "type": "OPTION"},
        "orderSide": "SELL",
        "orderType": "STOP_LIMIT",
        "expiration": {"timeInForce": "DAY"},
        "quantity": str(qty),
        "stopPrice": text,
        "limitPrice": text,
        "openCloseIndicator": "CLOSE",
    }


async def plan_stop(order: dict, s: "Settings", client: "PublicClient", store: "Store",
                    day: str) -> tuple[dict, Optional[tuple]]:
    """Report a stop for every contract still open. Never calls place(). Does not use MAX_PREMIUM.

    The stop covers the whole position, so it overlaps the take-profit until a fill reduces it
    to the runners. Public.com's single-leg schema accepts STOP and STOP_LIMIT; STOP_LIMIT is
    used so the protective sell has a price. If price gaps through the limit, the stop can sit
    unfilled and the flatten alert is what closes the contracts.
    """
    qty = int(order["qty"])
    if s.stop_loss_percent is None:
        report = {"attached": False, "quantity": 0, "status": "NONE",
                  "reason": "STOP_LOSS_PERCENT is not set"}
        jlog("stop", **report)
        return report, None
    entry = order["limit_price"] if isinstance(order["limit_price"], Decimal) else Decimal(str(order["limit_price"]))
    price = stop_price(entry, s.stop_loss_percent)
    body = stop_order_body(order["symbol"], qty, price)
    try:
        reason = await store.try_consume(day, s.mode, Decimal(0), False, s, is_exit=True)
    except (StoreError, httpx.HTTPError) as exc:
        report = {"attached": False, "quantity": qty, "status": "NONE", "stop_price": price_text(price),
                  "order": body, "reason": f"state store unavailable: {type(exc).__name__}"}
        jlog("stop", **report)
        return report, None
    if reason:
        report = {"attached": False, "quantity": qty, "status": "NONE", "stop_price": price_text(price),
                  "order": body, "reason": reason}
        jlog("stop", **report)
        return report, None
    slot = cap_slot(day, s.mode, Decimal(0), False, True, s.exits_count_toward_order_cap)
    try:
        pf = await client.preflight(body)
        pf_body = _json(pf)
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        await _release_quiet(store, slot)
        report = {"attached": False, "quantity": qty, "status": "NONE", "stop_price": price_text(price),
                  "order": body, "reason": f"upstream error: {type(exc).__name__}"}
        jlog("stop", **report)
        return report, None
    jlog("stop", status=pf.status_code, request=body, response=pf_body, quantity=qty)
    if pf.status_code >= 400:
        await _release_quiet(store, slot)
        report = {"attached": False, "quantity": qty, "status": "NONE", "stop_price": price_text(price),
                  "order": body, "reason": "preflight failed", "preflight_status": pf.status_code,
                  "preflight": pf_body}
        return report, None
    report = {
        "attached": True, "status": "RESTING", "quantity": qty, "stop_price": price_text(price),
        "limit_price": price_text(price), "orderId": str(uuid.uuid4()), "order": body,
        "order_type": "STOP_LIMIT", "preflight": pf_body, "note": "reported only; not sent to /order",
    }
    jlog("stop", attached=True, quantity=qty, stop_price=report["stop_price"], orderId=report["orderId"], order=body)
    return report, slot


def audit_detail(selection: Optional[dict], **extra: Any) -> dict:
    detail = dict(extra)
    if selection is not None:
        detail["selection"] = selection
    return detail


def error_body(alert_id: str, error: str, selection: Optional[dict] = None, **extra: Any) -> dict:
    body: dict[str, Any] = {"ok": False, "alert_id": alert_id, "error": error, **extra}
    if selection is not None:
        body["selection"] = selection
    return body


def order_body(o: dict) -> dict:
    return {
        "instrument": {"symbol": o["symbol"], "type": "OPTION"},
        "orderSide": o["side"],
        "orderType": "LIMIT",
        "expiration": {"timeInForce": "DAY"},
        "quantity": str(o["qty"]),
        "limitPrice": f"{o['limit_price']:.2f}",
        "openCloseIndicator": o["open_close"],
    }


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text[:2000]


# --------------------------------------------------------------------------- Alpaca paper
def paper_entry_cutoff(underlying: str, expiry: date, now: datetime) -> Optional[str]:
    """Alpaca rejects new 0DTE entries at 15:30 ET for SPY and QQQ, and at 15:15 ET for the rest.

    Alpaca auto-liquidates 0DTE positions at 15:45 ET. This relay does not send that liquidation.
    """
    current = as_et(now)
    if expiry != current.date():
        return None
    cutoff = PAPER_SPY_QQQ_CUTOFF if underlying in {"SPY", "QQQ"} else PAPER_OTHER_CUTOFF
    if current.time() < cutoff:
        return None
    return (
        f"Alpaca rejects new 0DTE {underlying} entries at or after {cutoff:%H:%M} ET. "
        "Alpaca auto-liquidates 0DTE options at 15:45 ET."
    )


def configured_expiry(now: datetime, expiry_days: int) -> date:
    return as_et(now).date() + timedelta(days=expiry_days)


def alpaca_option_symbol(symbol: str) -> str:
    """Alpaca uses OCC symbols with no space padding. Rebuild so the strike is 8 digits."""
    compact = "".join(str(symbol).upper().split())
    parsed = parse_occ(compact)
    if not parsed:
        raise Reject(f"cannot translate {symbol!r} into an Alpaca OCC symbol")
    return occ_symbol(parsed["underlying"], parsed["expiry"], parsed["right"], parsed["strike"])


def alpaca_order_state(payload: dict, ordered: int) -> tuple[str, int]:
    """Map an Alpaca order to FILLED, PARTIAL, RESTING, or CANCELLED, and a filled quantity."""
    status = str(payload.get("status") or "").lower()
    raw = payload.get("filled_qty")
    filled = 0 if raw in (None, "") else whole_qty(raw, allow_zero=True)
    filled = min(max(filled, 0), ordered) if ordered > 0 else max(filled, 0)
    resting = {"new", "accepted", "pending_new", "accepted_for_bidding", "pending_replace",
               "pending_cancel", "stopped", "calculated", "held"}
    # A dead order is CANCELLED even with a partial fill, so a later flatten does not cancel it again.
    # The filled count is kept and reduces what a sell-to-close may send.
    terminal = {"canceled", "expired", "rejected", "suspended", "replaced", "done_for_day"}
    if status == "filled":
        return "FILLED", ordered if filled == 0 else min(filled, ordered)
    if status == "partially_filled":
        if filled <= 0:
            raise Reject("partial Alpaca fill is missing filled_qty", 502)
        if ordered > 0 and filled >= ordered:
            return "FILLED", ordered
        return "PARTIAL", filled
    if status in resting:
        return "RESTING", filled
    if status in terminal:
        if ordered > 0 and filled >= ordered:
            return "FILLED", ordered
        return "CANCELLED", filled
    raise Reject(f"unrecognized Alpaca order status {status!r}", 502)


def entry_status_name(state: str) -> str:
    if state == "RESTING":
        return "PENDING"
    return state


def contract_is_penny(contract: dict) -> bool:
    return contract.get("ppind") is True


def fill_price(order: dict, fallback: Decimal) -> Decimal:
    average = positive_price(order.get("filled_avg_price"))
    if average is not None:
        return average
    return fallback


def paper_client_id(seed: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"public-relay:{seed}"))


def alpaca_limit_order(symbol: str, qty: int, side: str, intent: str, price: Decimal, client_order_id: str) -> dict:
    return {
        "symbol": symbol,
        "qty": str(qty),
        "side": side,
        "type": "limit",
        "time_in_force": "day",
        "limit_price": price_text(price),
        "position_intent": intent,
        "client_order_id": client_order_id,
    }


def alpaca_stop_order(symbol: str, qty: int, price: Decimal, client_order_id: str) -> dict:
    text = price_text(price)
    return {
        "symbol": symbol,
        "qty": str(qty),
        "side": "sell",
        "type": "stop_limit",
        "time_in_force": "day",
        "stop_price": text,
        "limit_price": text,
        "position_intent": "sell_to_close",
        "client_order_id": client_order_id,
    }


def _contract_row(payload: Any, symbol: str) -> dict:
    if isinstance(payload, dict) and payload.get("symbol"):
        return payload
    rows = payload.get("option_contracts") if isinstance(payload, dict) else None
    if isinstance(rows, list):
        for row in rows:
            if isinstance(row, dict) and row.get("symbol") == symbol:
                return row
    raise Reject(f"Alpaca has no option contract {symbol}", 422)


def _order_payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise Reject("Alpaca returned invalid JSON", 502) from exc
    if not isinstance(payload, dict) or not payload.get("id"):
        raise Reject("Alpaca returned invalid JSON", 502)
    return payload


def _alpaca_failure(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return f"Alpaca failed ({response.status_code})"
    if isinstance(payload, dict):
        message = payload.get("message") or payload.get("error")
        if isinstance(message, str) and message.strip():
            return f"Alpaca rejected the order ({response.status_code}): {message.strip()[:240]}"
    return f"Alpaca failed ({response.status_code})"


class AlpacaClient:
    """Alpaca paper trading. The client is constructed only when MODE=PAPER."""

    def __init__(self, settings: Settings, transport: Optional[httpx.AsyncBaseTransport] = None):
        self.http = httpx.AsyncClient(base_url=settings.alpaca_base_url or PAPER_BASE_URL, timeout=10.0,
                                      transport=transport)
        self._key = settings.alpaca_key_id
        self._secret = settings.alpaca_secret_key

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self._key,
            "APCA-API-SECRET-KEY": self._secret,
            "Accept": "application/json",
        }

    async def _req(self, method: str, path: str, body: Optional[dict] = None,
                   params: Optional[dict] = None) -> httpx.Response:
        try:
            return await self.http.request(method, path, headers=self._headers(), json=body, params=params)
        except httpx.HTTPError as exc:
            jlog("alpaca_unreachable", error=type(exc).__name__)
            raise Reject("Alpaca unreachable", 502) from exc

    async def account_view(self) -> dict:
        """Status, and equity when Alpaca sent it. Cash and buying power stay out of the response."""
        response = await self._req("GET", "/v2/account")
        if response.status_code >= 400:
            return {"ok": False, "error": f"account check failed ({response.status_code})"}
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "error": "account check returned invalid JSON"}
        if not isinstance(payload, dict):
            return {"ok": False, "error": "account check returned invalid JSON"}
        view: dict[str, Any] = {"ok": True, "status": payload.get("status")}
        if payload.get("equity") not in (None, ""):
            view["equity"] = str(payload.get("equity"))
        return view

    async def get_contract(self, symbol: str) -> dict:
        response = await self._req("GET", f"/v2/options/contracts/{symbol}")
        if response.status_code == 404:
            raise Reject(f"Alpaca has no option contract {symbol}", 422)
        if response.status_code >= 400:
            raise Reject(f"Alpaca contract lookup failed ({response.status_code})", 502)
        try:
            payload = response.json()
        except ValueError as exc:
            raise Reject("Alpaca contract lookup returned invalid JSON", 502) from exc
        contract = _contract_row(payload, symbol)
        if contract.get("tradable") is False or str(contract.get("status") or "active").lower() != "active":
            raise Reject(f"Alpaca contract {symbol} is not tradable", 422)
        return contract

    async def place(self, body: dict) -> dict:
        response = await self._req("POST", "/v2/orders", body)
        if response.status_code == 422:
            existing = await self.order_by_client(str(body.get("client_order_id") or ""))
            if existing is not None:
                return existing
        if response.status_code >= 400:
            raise Reject(_alpaca_failure(response), 502)
        return _order_payload(response)

    async def get_order(self, order_id: str) -> dict:
        response = await self._req("GET", f"/v2/orders/{order_id}")
        if response.status_code == 404:
            raise Reject(f"Alpaca order {order_id} was not found", 502)
        if response.status_code >= 400:
            raise Reject(f"Alpaca order status failed ({response.status_code})", 502)
        return _order_payload(response)

    async def order_by_client(self, client_order_id: str) -> Optional[dict]:
        if not client_order_id:
            return None
        response = await self._req("GET", "/v2/orders:by_client_order_id",
                                   params={"client_order_id": client_order_id})
        if response.status_code >= 400:
            return None
        try:
            return _order_payload(response)
        except Reject:
            return None

    async def cancel(self, order_id: str) -> None:
        response = await self._req("DELETE", f"/v2/orders/{order_id}")
        if response.status_code in (204, 404, 422):
            return
        if response.status_code >= 400:
            raise Reject(f"Alpaca cancel failed ({response.status_code})", 502)


async def poll_alpaca_order(alpaca: AlpacaClient, order_id: str, ordered: int, sleep: Callable[[float], Any]) -> dict:
    """A few short reads inside this request. Anything still working is reconciled later."""
    last = None
    for attempt in range(PAPER_FILL_POLLS):
        last = await alpaca.get_order(order_id)
        state, _filled = alpaca_order_state(last, ordered)
        if state in ("FILLED", "CANCELLED"):
            return last
        if attempt + 1 < PAPER_FILL_POLLS:
            await sleep(PAPER_FILL_WAIT_S)
    if last is None:
        raise Reject("Alpaca order status unreachable", 502)
    return last


async def refresh_paper_row(alpaca: AlpacaClient, stored: dict) -> dict:
    row = dict(stored)
    if row.get("entry_order_id"):
        payload = await alpaca.get_order(str(row["entry_order_id"]))
        state, filled = alpaca_order_state(payload, whole_qty(row.get("qty")))
        row["entry_status"] = entry_status_name(state)
        row["entry_filled"] = filled
        row["_entry_order"] = payload
    for prefix in ("tp", "stop"):
        order_id = str(row.get(f"{prefix}_order_id") or "").strip()
        status = str(row.get(f"{prefix}_status") or "NONE").upper()
        if not order_id or status not in ("RESTING", "PARTIAL", "FILLED"):
            continue
        payload = await alpaca.get_order(order_id)
        state, filled = alpaca_order_state(payload, _count_field(row, f"{prefix}_qty"))
        row[f"{prefix}_status"] = state
        row[f"{prefix}_filled"] = filled
    return row


async def cancel_if_working(alpaca: AlpacaClient, order_id: Any, status: Any) -> bool:
    if not str(order_id or "").strip():
        return False
    if str(status or "").upper() not in ("RESTING", "PARTIAL", "PENDING"):
        return False
    await alpaca.cancel(str(order_id).strip())
    return True


async def cancel_paper_working(alpaca: AlpacaClient, row: dict) -> list[str]:
    cancelled = []
    pairs = (
        (row.get("entry_order_id"), row.get("entry_status")),
        (row.get("tp_order_id"), row.get("tp_status")),
        (row.get("stop_order_id"), row.get("stop_status")),
    )
    for order_id, status in pairs:
        if await cancel_if_working(alpaca, order_id, status):
            cancelled.append(str(order_id).strip())
    return cancelled


async def _paper_release(store: Store, slot: tuple) -> None:
    try:
        await store.release(*slot)
    except (StoreError, httpx.HTTPError):
        jlog("release_failed", slot="paper")


def _exit_report(attached: bool, quantity: int, **extra: Any) -> dict:
    report = {"attached": attached, "quantity": quantity, "status": extra.pop("status", "NONE")}
    report.update(extra)
    return report


async def submit_paper_exits(alpaca: AlpacaClient, store: Store, s: Settings, day: str, row: dict,
                             entry: Decimal, penny: bool) -> tuple[dict, dict]:
    """Place the take-profit and stop after the entry has filled. Exit slots ignore the entry cap."""
    qty = _count_field(row, "entry_filled") or whole_qty(row.get("qty"))
    symbol = alpaca_option_symbol(str(row["symbol"]))
    entry_id = str(row.get("entry_order_id") or "")
    tp_qty = take_profit_qty(qty, s.tp_fraction, s.tp_single)
    if row.get("tp_order_id"):
        take_profit = _exit_report(True, _count_field(row, "tp_qty"), status=str(row.get("tp_status") or "RESTING"),
                                   limit_price=row.get("tp_limit"), orderId=row.get("tp_order_id"))
    elif tp_qty < 1:
        take_profit = _exit_report(False, 0, status="NONE", reason=no_take_profit_reason(qty, s.tp_fraction))
    else:
        limit = take_profit_limit(entry, s.tp_percent, penny)
        body = alpaca_limit_order(symbol, tp_qty, "sell", "sell_to_close", limit, paper_client_id(f"{entry_id}:tp"))
        take_profit = await _place_exit(alpaca, store, s, day, body, tp_qty, limit_price=price_text(limit))
    if row.get("stop_order_id"):
        stop = _exit_report(True, _count_field(row, "stop_qty"), status=str(row.get("stop_status") or "RESTING"),
                            stop_price=row.get("stop_limit"), limit_price=row.get("stop_limit"),
                            orderId=row.get("stop_order_id"), order_type="STOP_LIMIT")
    elif s.stop_loss_percent is None:
        stop = _exit_report(False, 0, status="NONE", reason="STOP_LOSS_PERCENT is not set")
    else:
        price = stop_price(entry, s.stop_loss_percent, penny)
        body = alpaca_stop_order(symbol, qty, price, paper_client_id(f"{entry_id}:stop"))
        stop = await _place_exit(alpaca, store, s, day, body, qty, stop_price=price_text(price),
                                 limit_price=price_text(price), order_type="STOP_LIMIT")
    return take_profit, stop


async def _place_exit(alpaca: AlpacaClient, store: Store, s: Settings, day: str, body: dict, qty: int,
                      **prices: str) -> dict:
    reason = await store.try_consume(day, s.mode, Decimal(0), False, s, is_exit=True, exit_counts=False)
    if reason:
        return _exit_report(False, qty, status="NONE", reason=reason, order=body, **prices)
    slot = cap_slot(day, s.mode, Decimal(0), False, True, False)
    try:
        placed = await alpaca.place(body)
    except Reject as exc:
        await _paper_release(store, slot)
        return _exit_report(False, qty, status="NONE", reason=exc.reason, order=body, **prices)
    jlog("paper_exit", order_id=placed.get("id"), symbol=body.get("symbol"), type=body.get("type"), qty=qty)
    return _exit_report(True, qty, status="RESTING", orderId=placed.get("id"), order=body,
                        placed=True, **prices)


def apply_exit_reports(row: dict, take_profit: dict, stop: dict) -> None:
    if take_profit.get("orderId"):
        row["tp_qty"] = take_profit.get("quantity") or 0
        row["tp_limit"] = take_profit.get("limit_price")
        row["tp_order_id"] = take_profit.get("orderId")
        row["tp_status"] = "RESTING"
        row["tp_filled"] = row.get("tp_filled") or 0
    elif not row.get("tp_order_id"):
        row["tp_status"] = "NONE"
        row["tp_qty"] = 0
    if stop.get("orderId"):
        row["stop_qty"] = stop.get("quantity") or 0
        row["stop_limit"] = stop.get("stop_price") or stop.get("limit_price")
        row["stop_order_id"] = stop.get("orderId")
        row["stop_status"] = "RESTING"
        row["stop_filled"] = row.get("stop_filled") or 0
    elif not row.get("stop_order_id"):
        row["stop_status"] = "NONE"
        row["stop_qty"] = 0
    row["exits_placed"] = bool(
        (row.get("tp_order_id") or take_profit.get("quantity", 0) < 1 or take_profit.get("reason") in {
            "single contract closes on the flatten alert; set TP_SINGLE to take profit that contract",
            "TP_FRACTION is 0", "no take-profit quantity",
        }) and (row.get("stop_order_id") or stop.get("reason") == "STOP_LOSS_PERCENT is not set")
    )
    # A cap or place failure leaves the leg without an id and must be retried.
    if take_profit.get("quantity", 0) >= 1 and not row.get("tp_order_id") and take_profit.get("reason") not in {
        "single contract closes on the flatten alert; set TP_SINGLE to take profit that contract",
        "TP_FRACTION is 0", "no take-profit quantity",
    }:
        row["exits_placed"] = False
    if stop.get("reason") not in (None, "STOP_LOSS_PERCENT is not set") and not row.get("stop_order_id"):
        row["exits_placed"] = False


def _without_private(row: dict) -> dict:
    return {key: value for key, value in row.items() if not str(key).startswith("_")}


async def save_refreshed_paper(store: "Store", row: dict) -> dict:
    clean = _without_private(row)
    clean["qty_open"] = max(0, _count_field(clean, "entry_filled") - _count_field(clean, "tp_filled")
                            - _count_field(clean, "stop_filled"))
    reason = await store.save_paper(clean)
    if reason:
        raise Reject(reason, 409)
    row["qty_open"] = clean["qty_open"]
    return row


async def refresh_saved_position(store: "Store", alpaca: AlpacaClient, underlying: str, strategy: str,
                                 mode: str) -> Optional[dict]:
    stored = await store.get_position(underlying, strategy, mode)
    if not stored:
        return None
    return await save_refreshed_paper(store, await refresh_paper_row(alpaca, stored))


async def reconcile_paper_positions(store: "Store", alpaca: AlpacaClient, s: "Settings", day: str) -> list:
    """Attach take-profit and stop orders once a resting paper entry has filled."""
    rows = await store.list_open_positions(s.mode)
    results = []
    for stored in rows:
        underlying = str(stored.get("underlying") or "")
        strategy = str(stored.get("strategy") or "")
        try:
            row = await refresh_paper_row(alpaca, stored)
            status = str(row.get("entry_status") or "NONE").upper()
            filled = _count_field(row, "entry_filled")
            if status == "CANCELLED" and filled < 1:
                await store.record_close(
                    underlying, strategy, s.mode, str(row.get("symbol") or ""), "keepalive",
                    tp_status=row.get("tp_status"), stop_status=row.get("stop_status"),
                    tp_filled=_count_field(row, "tp_filled"), stop_filled=_count_field(row, "stop_filled"))
                results.append({"underlying": underlying, "strategy": strategy, "entry_status": status, "closed": True})
                continue
            if status == "FILLED" and not row.get("exits_placed"):
                contract = await alpaca.get_contract(alpaca_option_symbol(str(row.get("symbol") or "")))
                price = fill_price(row.get("_entry_order") or {}, Decimal(str(row.get("entry_limit") or "0")))
                profile = s.for_strategy(strategy)
                take_profit, stop = await submit_paper_exits(
                    alpaca, store, profile, day, row, price, contract_is_penny(contract))
                apply_exit_reports(row, take_profit, stop)
            await save_refreshed_paper(store, row)
            results.append({
                "underlying": underlying, "strategy": strategy, "entry_status": status,
                "exits_placed": bool(row.get("exits_placed")),
            })
        except Reject as exc:
            jlog("paper_reconcile_failed", underlying=underlying, strategy=strategy, error=exc.reason)
            results.append({"underlying": underlying, "strategy": strategy, "error": exc.reason})
    return results


async def run_paper_alert(payload: dict, alert_id: str, day: str, now: datetime, s: "Settings",
                          store: "Store", public: "PublicClient", alpaca: AlpacaClient,
                          sleep: Callable[[float], Any], respond: Callable[..., JSONResponse]) -> JSONResponse:
    """Place the Public.com-selected contract on Alpaca paper, then the exits once it fills."""
    selection: Optional[dict] = None
    entry_slot = None
    exit_slot = None
    broker_sent = False
    try:
        s = s.for_strategy(strategy_name(payload))
        kind, symbol = classify_symbol(payload.get("symbol", ""))
        if kind == "underlying":
            strategy = strategy_name(payload)
            stored = await store.get_position(symbol, strategy, s.mode)
            if stored:
                await refresh_saved_position(store, alpaca, symbol, strategy, s.mode)
            else:
                action, _intent = decide_action(payload, None)
                if action == "open":
                    blocked = paper_entry_cutoff(symbol, configured_expiry(now, s.expiry_days), now)
                    if blocked:
                        raise Reject(blocked)
        order_payload, selection = await prepare_order(payload, s, public, store, now)
        if selection is None and str(order_payload.get("open_close", "")).upper() == "OPEN":
            parsed = parse_occ(str(order_payload.get("symbol") or ""))
            if parsed:
                blocked = paper_entry_cutoff(parsed["underlying"], parsed["expiry"], now)
                if blocked:
                    raise Reject(blocked)
        if selection is not None:
            await store.log_pick(alert_id, day, s.mode, selection)
            jlog("contract_pick", alert_id=alert_id, selection=selection)
        if selection is not None and selection.get("skip_order"):
            return await _paper_finish_flat(payload, alert_id, selection, s, store, alpaca, respond, sold=None)
        o = validate(order_payload, s)
        if selection is None and s.enforce_hours and not in_hours(now):
            raise Reject(hours_error(now))
        is_open = o["open_close"] == "OPEN"
        if is_open:
            broker_symbol = alpaca_option_symbol(o["symbol"])
            contract = await alpaca.get_contract(broker_symbol)
            penny = contract_is_penny(contract)
            limit = round_option_price(o["limit_price"], penny)
            # Closes are not checked again. A fill above the cap still has to be sellable.
            if limit > s.max_premium:
                raise Reject(f"limit_price {limit} exceeds MAX_PREMIUM {s.max_premium}")
            notional = limit * o["qty"] * 100
            reason = await store.try_consume(day, s.mode, notional, True, s, is_exit=False, exit_counts=False)
            if reason:
                raise Reject(reason)
            slot = cap_slot(day, s.mode, notional, True, False, False)
            entry_slot = slot
            body = alpaca_limit_order(broker_symbol, o["qty"], "buy", "buy_to_open", limit,
                                      str(uuid.uuid4()))
            placed = await alpaca.place(body)
            broker_sent = True
            entry_slot = None
            try:
                polled = await poll_alpaca_order(alpaca, str(placed["id"]), o["qty"], sleep)
            except Reject as exc:
                # The order is already at Alpaca. Keep it as working and let keepalive reconcile.
                jlog("paper_poll_failed", alert_id=alert_id, error=exc.reason)
                polled = placed
            state, filled = alpaca_order_state(polled, o["qty"])
            entry_status = entry_status_name(state)
            jlog("paper_entry", alert_id=alert_id, order_id=placed.get("id"), status=entry_status, filled=filled)
            if entry_status == "CANCELLED" and filled < 1:
                await _paper_release(store, slot)
                raise Reject("Alpaca entry was not filled", 502)
            take_profit: dict
            stop: dict
            if entry_status == "FILLED":
                price = fill_price(polled, limit)
                provisional = {
                    "symbol": broker_symbol, "qty": o["qty"], "entry_order_id": placed.get("id"),
                    "entry_filled": filled, "tp_order_id": None, "stop_order_id": None,
                }
                take_profit, stop = await submit_paper_exits(
                    alpaca, store, s, day, provisional, price, penny)
            else:
                take_profit = _exit_report(False, 0, status="NONE", reason="waiting for the entry to fill")
                if s.stop_loss_percent is None:
                    stop = _exit_report(False, 0, status="NONE", reason="STOP_LOSS_PERCENT is not set")
                else:
                    stop = _exit_report(False, o["qty"], status="NONE", reason="waiting for the entry to fill")
            if selection is not None and selection.get("action") == "open":
                row = {
                    "underlying": selection["underlying"], "strategy": selection["strategy"], "mode": s.mode,
                    "symbol": broker_symbol, "qty": o["qty"], "entry_order_id": placed.get("id"),
                    "entry_status": entry_status, "entry_filled": filled, "entry_limit": price_text(limit),
                    "tp_filled": 0, "stop_filled": 0, "tp_order_id": None, "stop_order_id": None,
                }
                apply_exit_reports(row, take_profit, stop)
                if entry_status != "FILLED":
                    row["exits_placed"] = False
                reason = await store.record_open(
                    str(selection["underlying"]), str(selection["strategy"]), s.mode, broker_symbol,
                    str(selection["entry_side"]), str(selection["right"]), o["qty"],
                    str(selection["chosen_strike"]), alert_id,
                    qty_open=filled, tp_qty=int(row.get("tp_qty") or 0), tp_limit=row.get("tp_limit"),
                    tp_order_id=row.get("tp_order_id"), tp_status=str(row.get("tp_status") or "NONE"),
                    entry_limit=price_text(limit), tp_filled=0, stop_qty=int(row.get("stop_qty") or 0),
                    stop_filled=0, stop_limit=row.get("stop_limit"), stop_order_id=row.get("stop_order_id"),
                    stop_status=str(row.get("stop_status") or "NONE"), entry_order_id=str(placed.get("id")),
                    entry_status=entry_status, entry_filled=filled, exits_placed=bool(row.get("exits_placed")))
                if reason:
                    jlog("position_persist_failed", alert_id=alert_id, reason=reason)
            await store.finish(alert_id, "ACCEPTED", "OPEN", notional, s.mode, {
                "selection": selection, "take_profit": take_profit, "stop": stop,
                "alpaca_order_id": placed.get("id"), "entry_status": entry_status,
            })
            return respond(200, {
                "ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": True, "broker": "alpaca",
                "selection": selection, "order": body, "alpaca_order_id": placed.get("id"),
                "entry_status": entry_status, "filled_qty": filled, "take_profit": take_profit, "stop": stop,
            })
        reason = await store.try_consume(
            day, s.mode, o["notional"], False, s, is_exit=True, exit_counts=False)
        if reason:
            raise Reject(reason)
        slot = cap_slot(day, s.mode, o["notional"], False, True, False)
        exit_slot = None
        return await _paper_sell_close(payload, alert_id, selection, o, s, store, alpaca, respond, slot)
    except Reject as exc:
        if entry_slot is not None and not broker_sent:
            await _paper_release(store, entry_slot)
        if exit_slot is not None and not broker_sent:
            await _paper_release(store, exit_slot)
        await store.finish(alert_id, "REJECTED", mode=s.mode, detail=audit_detail(selection, reason=exc.reason))
        return respond(exc.status, error_body(alert_id, exc.reason, selection))
    except (StoreError, httpx.HTTPError) as exc:
        await store.finish(alert_id, "ERROR", mode=s.mode, detail=audit_detail(selection, error=type(exc).__name__))
        return respond(503, error_body(alert_id, f"state store unavailable: {type(exc).__name__}", selection))


async def _paper_finish_flat(payload: dict, alert_id: str, selection: dict, s: "Settings", store: "Store",
                             alpaca: AlpacaClient, respond: Callable[..., JSONResponse],
                             sold: Optional[dict]) -> JSONResponse:
    stored = await store.get_position(str(selection["underlying"]), str(selection["strategy"]), s.mode)
    cancelled: list[str] = []
    if stored:
        cancelled = await cancel_paper_working(alpaca, stored)
        refreshed = await refresh_paper_row(alpaca, stored)
        await save_refreshed_paper(store, refreshed)
        selection = dict(selection)
        selection["tp_status_after"] = "CANCELLED" if str(refreshed.get("tp_status") or "") in ("RESTING", "PARTIAL", "PENDING") else refreshed.get("tp_status")
        selection["stop_status_after"] = "CANCELLED" if str(refreshed.get("stop_status") or "") in ("RESTING", "PARTIAL", "PENDING") else refreshed.get("stop_status")
        selection["tp_filled"] = _count_field(refreshed, "tp_filled")
        selection["stop_filled"] = _count_field(refreshed, "stop_filled")
    reason = await store.record_close(
        str(selection["underlying"]), str(selection["strategy"]), s.mode, str(selection["chosen_symbol"]), alert_id,
        tp_status=selection.get("tp_status_after"), stop_status=selection.get("stop_status_after"),
        tp_filled=selection.get("tp_filled"), stop_filled=selection.get("stop_filled"))
    if reason:
        await store.finish(alert_id, "REJECTED", mode=s.mode, detail=audit_detail(selection, reason=reason))
        return respond(409, error_body(alert_id, reason, selection))
    note = str(selection.get("note") or "Nothing remains open, so no sell is sent.")
    await store.finish(alert_id, "ACCEPTED", "CLOSE", Decimal(0), s.mode,
                       audit_detail(selection, contracts_closed=0, note=note, cancelled_order_ids=cancelled))
    body = {"ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": False, "broker": "alpaca",
            "selection": selection, "contracts_closed": 0, "runners_closed": 0, "note": note,
            "cancelled_order_ids": cancelled}
    if selection.get("cancel_take_profit"):
        body["cancel_take_profit"] = selection["cancel_take_profit"]
    if selection.get("cancel_stop"):
        body["cancel_stop"] = selection["cancel_stop"]
    return respond(200, body)


async def _paper_sell_close(payload: dict, alert_id: str, selection: Optional[dict], order: dict, s: "Settings",
                            store: "Store", alpaca: AlpacaClient, respond: Callable[..., JSONResponse],
                            slot: tuple) -> JSONResponse:
    cancelled: list[str] = []
    if selection is not None:
        stored = await store.get_position(str(selection["underlying"]), str(selection["strategy"]), s.mode)
        if stored:
            live = await refresh_paper_row(alpaca, stored)
            planned = exit_plan(live)
            cancelled = await cancel_paper_working(alpaca, live)
            live = await refresh_paper_row(alpaca, live)
            await save_refreshed_paper(store, live)
            confirmed = exit_plan(live)
            # Cancelling a resting order does not change the fill. Keep the pre-cancel split so
            # an unfilled take-profit is still part of this sell. A fill that landed first wins.
            plan = planned
            if confirmed["tp_filled"] != planned["tp_filled"] or confirmed["stop_filled"] != planned["stop_filled"]:
                plan = confirmed
                if planned.get("cancel_take_profit") and not plan.get("cancel_take_profit"):
                    plan["cancel_take_profit"] = planned["cancel_take_profit"]
                if planned.get("cancel_stop") and not plan.get("cancel_stop"):
                    plan["cancel_stop"] = planned["cancel_stop"]
            if plan["still_open"] < 1:
                await _paper_release(store, slot)
                selection = dict(selection)
                selection.update({
                    "skip_order": True, "contracts_closed": 0, "runners_closed": 0,
                    "note": plan["note"], "tp_status_after": plan["tp_status_after"],
                    "stop_status_after": plan["stop_status_after"], "tp_filled": plan["tp_filled"],
                    "stop_filled": plan["stop_filled"], "cancel_take_profit": plan["cancel_take_profit"],
                    "cancel_stop": plan["cancel_stop"],
                })
                return await _paper_finish_flat(payload, alert_id, selection, s, store, alpaca, respond, sold=None)
            order = {**order, "qty": plan["still_open"], "notional": order["limit_price"] * plan["still_open"] * 100}
            selection = dict(selection)
            selection.update({
                "contracts_closed": plan["still_open"], "runners_closed": plan["runners"],
                "take_profit_contracts_closed": plan["take_profit_contracts_closed"],
                "tp_status_after": plan["tp_status_after"], "stop_status_after": plan["stop_status_after"],
                "tp_filled": plan["tp_filled"], "stop_filled": plan["stop_filled"],
                "cancel_take_profit": plan["cancel_take_profit"], "cancel_stop": plan["cancel_stop"],
                "note": plan["note"],
            })
    broker_symbol = alpaca_option_symbol(order["symbol"])
    penny = False
    sent = False
    try:
        try:
            penny = contract_is_penny(await alpaca.get_contract(broker_symbol))
        except Reject:
            penny = False
        limit = round_option_price(order["limit_price"], penny)
        body = alpaca_limit_order(broker_symbol, int(order["qty"]), "sell", "sell_to_close", limit, str(uuid.uuid4()))
        placed = await alpaca.place(body)
        sent = True
    except Reject:
        if not sent:
            await _paper_release(store, slot)
        raise
    jlog("paper_flatten", alert_id=alert_id, order_id=placed.get("id"), qty=order["qty"], cancelled=cancelled)
    if selection is not None:
        reason = await store.record_close(
            str(selection["underlying"]), str(selection["strategy"]), s.mode, str(selection.get("chosen_symbol") or broker_symbol),
            alert_id, tp_status=selection.get("tp_status_after"), stop_status=selection.get("stop_status_after"),
            tp_filled=selection.get("tp_filled"), stop_filled=selection.get("stop_filled"))
        if reason:
            jlog("position_persist_failed", alert_id=alert_id, reason=reason)
    await store.finish(alert_id, "ACCEPTED", "CLOSE", order["notional"], s.mode, {
        "selection": selection, "alpaca_order_id": placed.get("id"), "cancelled_order_ids": cancelled,
    })
    response = {
        "ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": True, "broker": "alpaca",
        "order": body, "alpaca_order_id": placed.get("id"), "cancelled_order_ids": cancelled,
    }
    if selection is not None:
        response["selection"] = selection
        response["contracts_closed"] = selection.get("contracts_closed")
        response["runners_closed"] = selection.get("runners_closed")
        response["take_profit_contracts_closed"] = selection.get("take_profit_contracts_closed")
        response["note"] = selection.get("note")
        if selection.get("cancel_take_profit"):
            response["cancel_take_profit"] = selection["cancel_take_profit"]
        if selection.get("cancel_stop"):
            response["cancel_stop"] = selection["cancel_stop"]
    return respond(200, response)


async def run_readonly_preflight(payload: dict, s: "Settings", client: "PublicClient", store: "Store",
                                 now: datetime) -> dict:
    """Pick the contract and ask Public.com to preflight it.

    Market hours are not enforced. This does not place an order, write a position or order row,
    or consume a daily cap. A read of the open position, if any, is only used to tell an open
    from a flatten.
    """
    profile = replace(s.for_strategy(strategy_name(payload)), enforce_hours=False)
    order_payload, selection = await prepare_order(payload, profile, client, store, now)
    if selection is not None and selection.get("skip_order"):
        return {
            "ok": True, "passed": False, "order_placed": False,
            "contract": selection.get("chosen_symbol"), "premium": None, "delta": selection.get("delta"),
            "reason": selection.get("note"), "selection": selection,
        }
    order = validate(order_payload, profile)
    body = order_body(order)
    try:
        response = await client.preflight(body)
        preview = _json(response)
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        raise Reject(f"upstream error: {type(exc).__name__}", 502) from exc
    contract = order["symbol"]
    premium = f"{order['limit_price']:.2f}"
    delta = None
    if selection is not None:
        contract = selection.get("chosen_symbol") or contract
        premium = selection.get("limit") or premium
        delta = selection.get("delta")
    jlog("readonly_preflight", passed=response.status_code < 400, contract=contract,
         premium=premium, delta=delta, status=response.status_code)
    report = {
        "ok": True, "passed": response.status_code < 400, "preflight_status": response.status_code,
        "order_placed": False, "contract": contract, "premium": premium, "delta": delta,
        "preflight": preview,
    }
    if selection is not None:
        report["selection"] = selection
    return report


# --------------------------------------------------------------------------- app
def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               store_transport: Optional[httpx.AsyncBaseTransport] = None,
               clock: Callable[[], datetime] = lambda: datetime.now(ET),
               alpaca_transport: Optional[httpx.AsyncBaseTransport] = None,
               sleeper: Optional[Callable[[float], Any]] = None) -> FastAPI:
    s = settings or Settings.from_env()
    store = Store(s.supabase_url, s.supabase_key, store_transport)
    client = PublicClient(s, transport)
    alpaca = AlpacaClient(s, alpaca_transport) if s.mode == "PAPER" else None
    sleep = sleeper or asyncio.sleep
    app = FastAPI(title="public-relay")

    if s.requested_mode == "LIVE" and s.mode != "LIVE":
        jlog("live_refused", reason="MODE=LIVE but LIVE_CONFIRM != I_UNDERSTAND; running PREFLIGHT")
    jlog("startup", mode=s.mode, allowlist=sorted(s.allowlist), max_qty=s.max_qty,
         max_premium=s.max_premium, max_orders_per_day=s.max_orders_per_day,
         max_daily_opens=s.max_daily_opens, settings=s.public_view())

    def respond(status: int, body: dict, req: Any = None) -> JSONResponse:
        jlog("response", status=status, body=body, request=req)
        return JSONResponse(body, status_code=status)

    @app.get("/health")
    async def health():
        body = {"ok": True, "mode": s.mode, "store_configured": store.configured,
                "public_configured": bool(s.public_secret), "webhook_configured": bool(s.webhook_passphrase),
                "alpaca_configured": bool(s.alpaca_key_id and s.alpaca_secret_key),
                "settings": s.public_view()}
        if s.mode == "PAPER" and alpaca is not None:
            try:
                body["alpaca"] = await alpaca.account_view()
            except Reject as exc:
                body["alpaca"] = {"ok": False, "error": exc.reason}
        return body

    @app.get("/keepalive")
    async def keepalive(request: Request):
        # Vercel Cron sends "Authorization: Bearer $CRON_SECRET". Touches Supabase only, never Public.
        secret = os.environ.get("CRON_SECRET", "")
        auth = request.headers.get("authorization", "")
        if not secret or not hmac.compare_digest(auth.encode(), f"Bearer {secret}".encode()):
            return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
        try:
            await store.ping()
        except (StoreError, httpx.HTTPError) as e:
            jlog("keepalive_failed", error=type(e).__name__)
            return JSONResponse({"ok": False, "error": "store unavailable"}, status_code=503)
        reconciled = None
        if s.mode == "PAPER" and alpaca is not None:
            try:
                reconciled = await reconcile_paper_positions(store, alpaca, s, clock().date().isoformat())
            except (StoreError, httpx.HTTPError, Reject) as exc:
                jlog("paper_reconcile_failed", error=type(exc).__name__)
                return JSONResponse({"ok": False, "error": "paper reconcile failed"}, status_code=503)
        jlog("keepalive_ok")
        body = {"ok": True}
        if reconciled is not None:
            body["reconciled"] = reconciled
        return body

    @app.post("/preflight")
    async def preflight(request: Request):
        # Same passphrase as /webhook. Selects and preflights only. Never places or persists.
        try:
            payload = json.loads(await request.body())
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return respond(400, {"ok": False, "error": "body must be a JSON object", "order_placed": False})
        jlog("request", body=payload)
        pw = str(payload.get("passphrase", ""))
        if not s.webhook_passphrase or not hmac.compare_digest(pw.encode(), s.webhook_passphrase.encode()):
            return respond(401, {"ok": False, "error": "unauthorized", "order_placed": False})
        try:
            report = await run_readonly_preflight(payload, s, client, store, clock())
        except Reject as exc:
            return respond(exc.status, {"ok": False, "error": exc.reason, "order_placed": False})
        except (StoreError, httpx.HTTPError) as exc:
            return respond(503, {"ok": False, "order_placed": False,
                                 "error": f"state store unavailable: {type(exc).__name__}"})
        return respond(200, report)

    @app.post("/webhook")
    async def webhook(request: Request):
        try:
            payload = json.loads(await request.body())
            if not isinstance(payload, dict):
                raise ValueError
        except ValueError:
            return respond(400, {"ok": False, "error": "body must be a JSON object"})
        jlog("request", body=payload)

        pw = str(payload.get("passphrase", ""))
        if not s.webhook_passphrase or not hmac.compare_digest(pw.encode(), s.webhook_passphrase.encode()):
            return respond(401, {"ok": False, "error": "unauthorized"})

        alert_id = str(payload.get("alert_id", "")).strip()
        if not alert_id:
            return respond(422, {"ok": False, "error": "alert_id required"})

        now = clock()
        day = now.date().isoformat()
        try:
            if not await store.reserve(alert_id, day):
                return respond(200, {"ok": True, "duplicate": True, "alert_id": alert_id})
        except (StoreError, httpx.HTTPError) as e:
            return respond(503, {"ok": False, "alert_id": alert_id, "error": f"state store unavailable: {type(e).__name__}"})
        if s.mode == "PAPER":
            if alpaca is None:
                return respond(503, {"ok": False, "alert_id": alert_id, "error": "Alpaca is not configured"})
            return await run_paper_alert(payload, alert_id, day, now, s, store, client, alpaca, sleep, respond)
        releases: list[tuple] = []
        selection: Optional[dict] = None
        try:
            profile = s.for_strategy(strategy_name(payload))
            order_payload, selection = await prepare_order(payload, profile, client, store, now)
            if selection is not None and selection.get("skip_order"):
                await store.log_pick(alert_id, day, s.mode, selection)
                jlog("contract_pick", alert_id=alert_id, selection=selection)
                jlog("flatten_close", alert_id=alert_id, contracts_closed=0,
                     cancel_take_profit=selection.get("cancel_take_profit"),
                     cancel_stop=selection.get("cancel_stop"), note=selection.get("note"))
                reason = await store.record_close(
                    str(selection["underlying"]), str(selection["strategy"]), s.mode,
                    str(selection["chosen_symbol"]), alert_id,
                    tp_status=selection.get("tp_status_after"), stop_status=selection.get("stop_status_after"),
                    tp_filled=selection.get("tp_filled"), stop_filled=selection.get("stop_filled"))
                if reason:
                    await store.finish(alert_id, "REJECTED", mode=s.mode,
                                       detail=audit_detail(selection, reason=reason))
                    return respond(409, error_body(alert_id, reason, selection))
                note = str(selection.get("note") or "Nothing remains open, so no sell is sent.")
                await store.finish(alert_id, "ACCEPTED", "CLOSE", Decimal(0), s.mode,
                                   audit_detail(selection, contracts_closed=0, note=note))
                body = {"ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": False,
                        "selection": selection, "contracts_closed": 0, "runners_closed": 0, "note": note}
                if selection.get("cancel_take_profit"):
                    body["cancel_take_profit"] = selection["cancel_take_profit"]
                if selection.get("cancel_stop"):
                    body["cancel_stop"] = selection["cancel_stop"]
                return respond(200, body)
            if selection is not None:
                await store.log_pick(alert_id, day, profile.mode, selection)
                jlog("contract_pick", alert_id=alert_id, selection=selection)
            o = validate(order_payload, profile)
            # Underlying alerts already failed closed on the window before any Public call.
            if selection is None and profile.enforce_hours and not in_hours(now):
                raise Reject(hours_error(now))
            is_open = o["open_close"] == "OPEN"
            is_exit = not is_open
            reason = await store.try_consume(day, profile.mode, o["notional"], is_open, profile, is_exit=is_exit)
            if reason:
                raise Reject(reason)
            releases.append(cap_slot(
                day, profile.mode, o["notional"], is_open, is_exit, profile.exits_count_toward_order_cap))
        except Reject as e:
            await store.finish(alert_id, "REJECTED", mode=s.mode, detail=audit_detail(selection, reason=e.reason))
            return respond(e.status, error_body(alert_id, e.reason, selection))
        except (StoreError, httpx.HTTPError) as e:  # fail closed
            await store.finish(alert_id, "ERROR", mode=s.mode, detail=audit_detail(selection, error=type(e).__name__))
            return respond(503, error_body(alert_id, f"state store unavailable: {type(e).__name__}", selection))

        async def fail(status_txt: str, detail: Any) -> None:
            await store.finish(alert_id, status_txt, mode=s.mode, detail=detail)
            for slot in releases:
                try:
                    await store.release(*slot)
                except (StoreError, httpx.HTTPError):
                    jlog("release_failed", alert_id=alert_id)  # fails safe: cap stays consumed

        async def persist(take_profit: Optional[dict], stop: Optional[dict] = None) -> Optional[JSONResponse]:
            if selection is None:
                return None
            try:
                reason = await book_position(store, selection, o, s.mode, alert_id, take_profit, stop)
            except (StoreError, httpx.HTTPError) as exc:
                jlog("position_persist_failed", alert_id=alert_id, error=type(exc).__name__)
                if s.mode == "LIVE":
                    return None
                await fail("ERROR", audit_detail(selection, error=type(exc).__name__))
                return respond(503, error_body(alert_id, f"state store unavailable: {type(exc).__name__}", selection))
            if not reason:
                return None
            jlog("position_persist_failed", alert_id=alert_id, reason=reason)
            if s.mode == "LIVE":
                return None
            await fail("REJECTED", audit_detail(selection, reason=reason))
            return respond(409, error_body(alert_id, reason, selection))

        def accepted_body(preflight: Any, placed: bool, take_profit: Optional[dict] = None,
                          stop: Optional[dict] = None, **extra: Any) -> dict:
            body = {"ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": placed, **extra}
            if preflight is not None:
                body["preflight"] = preflight
            if selection is not None:
                body["selection"] = selection
                if not selection.get("skip_order"):
                    body["order"] = order_body(o)
            if selection is not None and selection.get("action") == "close":
                body["contracts_closed"] = selection.get("contracts_closed")
                body["runners_closed"] = selection.get("runners_closed")
                body["take_profit_contracts_closed"] = selection.get("take_profit_contracts_closed")
                if selection.get("cancel_take_profit"):
                    body["cancel_take_profit"] = selection["cancel_take_profit"]
                if selection.get("cancel_stop"):
                    body["cancel_stop"] = selection["cancel_stop"]
                if selection.get("note"):
                    body["note"] = selection["note"]
            if take_profit is not None:
                body["take_profit"] = take_profit
            if stop is not None:
                body["stop"] = stop
            return body

        async def accept(preflight: Any, placed: bool, **extra: Any) -> JSONResponse:
            take_profit = None
            stop = None
            if o["open_close"] == "OPEN":
                take_profit, slot = await plan_take_profit(o, profile, client, store, day)
                if slot is not None:
                    releases.append(slot)
                stop, stop_slot = await plan_stop(o, profile, client, store, day)
                if stop_slot is not None:
                    releases.append(stop_slot)
            blocked = await persist(take_profit, stop)
            if blocked is not None:
                return blocked
            detail: dict[str, Any] = {}
            if preflight is not None:
                detail["preflight"] = preflight
            if selection is not None:
                detail["selection"] = selection
            if take_profit is not None:
                detail["take_profit"] = take_profit
            if stop is not None:
                detail["stop"] = stop
            for key, value in extra.items():
                if value is not None:
                    detail[key] = value
            await store.finish(alert_id, "ACCEPTED", o["open_close"], o["notional"], s.mode, detail or None)
            shown = None if placed else preflight
            return respond(200, accepted_body(shown, placed, take_profit, stop, **extra))

        body = order_body(o)
        try:
            pf = await client.preflight(body)
            pf_body = _json(pf)
            jlog("preflight", status=pf.status_code, request=body, response=pf_body)
            if pf.status_code >= 400:
                await fail("PREFLIGHT_FAILED", pf_body if selection is None else {"preflight": pf_body, "selection": selection})
                failed = {"ok": False, "alert_id": alert_id, "stage": "preflight",
                          "status": pf.status_code, "response": pf_body}
                if selection is not None:
                    failed["selection"] = selection
                return respond(502, failed)
            if s.mode != "LIVE":
                return await accept(pf_body, False)
            place_body = {**body, "orderId": str(uuid.uuid4())}
            r = await client.place(place_body)
            r_body = _json(r)
            jlog("place_order", status=r.status_code, request=place_body, response=r_body)
            if r.status_code >= 400:
                await fail("ORDER_FAILED", r_body if selection is None else {"response": r_body, "selection": selection})
                failed = {"ok": False, "alert_id": alert_id, "stage": "order",
                          "status": r.status_code, "response": r_body}
                if selection is not None:
                    failed["selection"] = selection
                return respond(502, failed)
            return await accept(None, True, orderId=place_body["orderId"], response=r_body)
        except (httpx.HTTPError, RuntimeError, KeyError) as e:
            # Don't release on transport errors during LIVE place: order may have gone through.
            await store.finish(alert_id, "ERROR", mode=s.mode, detail=audit_detail(selection, error=type(e).__name__))
            return respond(502, error_body(alert_id, f"upstream error: {type(e).__name__}", selection))

    return app


if os.environ.get("RELAY_NO_AUTOAPP") != "1":
    app = create_app()
