"""TradingView webhook -> Public.com single-leg options relay.

Vercel + Supabase port. Review-only. Defaults to MODE=PREFLIGHT, which never places an order.
LIVE requires BOTH MODE=LIVE and LIVE_CONFIRM=I_UNDERSTAND.

A full OCC symbol is forwarded as sent. An underlying such as SPY selects a call (BUY) or put
(SELL) for the configured expiry, or sells the stored runners when the alert flattens or flips.
Selection, size, and take-profit knobs are environment variables read at startup. MODE and
LIVE_CONFIRM are unchanged: LIVE still requires both MODE=LIVE and LIVE_CONFIRM=I_UNDERSTAND.
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
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from typing import Any, Callable, Literal, Never, Optional
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
# Defaults. Each one is overridable from the environment; see Settings.from_env.
ITM_POINTS = Decimal("5")
DELTA_LO = Decimal("0.50")
DELTA_HI = Decimal("0.60")
LimitFallback = Literal["ask", "bid", "reject"]
REDACT_KEYS = {"passphrase", "secret", "authorization", "accesstoken", "access_token",
               "public_secret", "webhook_passphrase", "apikey", "supabase_service_key"}


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


@dataclass
class Settings:
    public_secret: str = ""
    webhook_passphrase: str = ""
    mode: str = "PREFLIGHT"           # effective mode, after LIVE_CONFIRM check
    requested_mode: str = "PREFLIGHT"
    account_id: str = ""              # optional; auto-discovered if blank
    max_qty: int = 1
    max_premium: Decimal = Decimal("2.00")
    max_orders_per_day: int = 3
    max_daily_opens: Decimal = Decimal("300")  # $ spent on OPEN orders per ET day
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
    exits_count_toward_order_cap: bool = False
    max_exit_orders_per_day: int = 6

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
            "exits_count_toward_order_cap": self.exits_count_toward_order_cap,
            "max_exit_orders_per_day": self.max_exit_orders_per_day,
            "enforce_hours": self.enforce_hours,
        }

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Settings":
        e = os.environ if env is None else env
        requested = e.get("MODE", "PREFLIGHT").strip().upper()
        live_ok = requested == "LIVE" and e.get("LIVE_CONFIRM", "") == "I_UNDERSTAND"
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
        return cls(
            public_secret=e.get("PUBLIC_SECRET", ""),
            webhook_passphrase=e.get("WEBHOOK_PASSPHRASE", ""),
            requested_mode=requested,
            mode="LIVE" if live_ok else "PREFLIGHT",
            account_id=e.get("PUBLIC_ACCOUNT_ID", "").strip(),
            max_qty=_env_int(e, "MAX_QTY", 1, 1, 100),
            max_premium=_env_decimal(e, "MAX_PREMIUM", "2.00"),
            max_orders_per_day=_env_int(e, "MAX_ORDERS_PER_DAY", 3, 1, 1000),
            max_daily_opens=_env_decimal(e, "MAX_DAILY_OPENS", "300"),
            allowlist={s.strip().upper() for s in e.get("ALLOWLIST", "").split(",") if s.strip()},
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
            exits_count_toward_order_cap=_env_bool(e, "EXITS_COUNT_TOWARD_ORDER_CAP", False),
            max_exit_orders_per_day=_env_int(e, "MAX_EXIT_ORDERS_PER_DAY", 6, 1, 1000),
        )


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
                          is_exit: bool = False) -> Optional[str]:
        # Entries increment orders and, when opening, open_notional. Exits increment exit_orders.
        # They also increment orders only when EXITS_COUNT_TOWARD_ORDER_CAP is on.
        data = await self._rpc("relay_try_consume", {
            "p_day": day, "p_mode": mode, "p_notional": str(notional), "p_is_open": is_open,
            "p_max_orders": s.max_orders_per_day, "p_max_opens": str(s.max_daily_opens),
            "p_is_exit": is_exit, "p_exit_counts": s.exits_count_toward_order_cap,
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
                          tp_status: str = "NONE", entry_limit: Optional[str] = None) -> Optional[str]:
        data = await self._rpc("relay_record_open", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode, "p_symbol": symbol,
            "p_entry_side": entry_side, "p_right_side": right_side, "p_qty": qty, "p_strike": strike,
            "p_alert_id": alert_id, "p_qty_open": qty if qty_open is None else qty_open,
            "p_tp_qty": tp_qty, "p_tp_limit": tp_limit, "p_tp_order_id": tp_order_id,
            "p_tp_status": tp_status, "p_entry_limit": entry_limit})
        return _rpc_reason(data, "relay_record_open")

    async def record_close(self, underlying: str, strategy: str, mode: str, symbol: str,
                           alert_id: str) -> Optional[str]:
        data = await self._rpc("relay_record_close", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode,
            "p_symbol": symbol, "p_alert_id": alert_id})
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
    if price > s.max_premium:
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


def take_profit_limit(entry: Decimal, percent: Decimal) -> Decimal:
    """Limit = entry * (1 + TP_PERCENT/100), rounded to the cent. Not capped by MAX_PREMIUM."""
    return (entry * (Decimal(1) + percent / Decimal(100))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def no_take_profit_reason(qty: int, fraction: Decimal) -> str:
    if qty < 2:
        return "single contract closes on the flatten alert; set TP_SINGLE to take profit that contract"
    if fraction <= 0:
        return "TP_FRACTION is 0"
    return "no take-profit quantity"


def runner_qty(stored: dict) -> int:
    if "qty_open" in stored and stored.get("qty_open") not in (None, ""):
        return whole_qty(stored.get("qty_open"), allow_zero=True)
    return whole_qty(stored.get("qty"))


def stored_tp_qty(stored: dict) -> int:
    if stored.get("tp_qty") in (None, ""):
        return 0
    return whole_qty(stored.get("tp_qty"), allow_zero=True)


def cancel_take_profit_view(stored: dict) -> Optional[dict]:
    """A resting take-profit is cancelled by a flatten. Those contracts are not sold."""
    if str(stored.get("tp_status") or "").upper() != "RESTING":
        return None
    order_id = str(stored.get("tp_order_id") or "").strip()
    if not order_id:
        return None
    return {
        "orderId": order_id,
        "quantity": stored_tp_qty(stored),
        "reason": "unfilled",
        "note": "cancelled take-profit contracts are not sold",
    }


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


async def build_close(payload: dict, underlying: str, strategy: str, client: "PublicClient",
                      s: "Settings", stored: dict, intent: str) -> tuple[dict, dict]:
    symbol = str(stored.get("symbol") or "").upper().replace(" ", "")
    parsed = parse_occ(symbol)
    if not parsed or parsed["underlying"] != underlying:
        raise Reject(f"stored contract {symbol!r} does not match {underlying}", 502)
    runners = runner_qty(stored)
    opened = whole_qty(stored.get("qty"))
    tp_qty = stored_tp_qty(stored)
    strike = parse_decimal(stored.get("strike")) or parsed["strike"]
    spot = alert_underlying_price(payload)
    cancel = cancel_take_profit_view(stored)
    if runners < 1:
        # Nothing left to sell. Cancel the resting take-profit and do not quote or preflight.
        selection = {
            "action": "close", "intent": intent, "underlying": underlying, "strategy": strategy,
            "entry_side": str(stored.get("entry_side") or ""), "order_side": "SELL", "open_close": "CLOSE",
            "underlying_price": dec_text(spot), "underlying_price_source": "alert" if spot is not None else None,
            "expiry": parsed["expiry"].isoformat(), "right": parsed["right"], "candidates": [],
            "chosen_symbol": symbol, "chosen_strike": dec_text(strike), "delta": None, "delta_source": None,
            "bid": None, "ask": None, "mid": None, "limit": None, "limit_source": None,
            "qty": 0, "opened_qty": opened, "take_profit_qty": tp_qty, "runners_closed": 0,
            "skip_order": True,
        }
        if cancel is not None:
            selection["cancel_take_profit"] = cancel
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
        "limit_price": cents(limit), "qty": runners,
    }
    selection = selection_from_leg(
        "close", underlying, strategy, str(stored.get("entry_side") or ""), "SELL", "CLOSE",
        spot, "alert" if spot is not None else None, parsed["expiry"], parsed["right"],
        [chosen], chosen, limit, limit_source, runners, intent)
    selection["opened_qty"] = opened
    selection["take_profit_qty"] = tp_qty
    selection["runners_closed"] = runners
    if cancel is not None:
        selection["cancel_take_profit"] = cancel
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
                        take_profit: Optional[dict] = None) -> Optional[str]:
    action = selection.get("action")
    if action == "open":
        tp = take_profit or {}
        attached = bool(tp.get("attached"))
        qty = int(order["qty"])
        qty_open = int(tp["qty_open"]) if "qty_open" in tp else qty
        return await store.record_open(
            str(selection["underlying"]), str(selection["strategy"]), mode, order["symbol"],
            str(selection["entry_side"]), str(selection["right"]), qty,
            str(selection["chosen_strike"]), alert_id, qty_open=qty_open,
            tp_qty=int(tp.get("quantity") or 0) if attached else 0,
            tp_limit=tp.get("limit_price") if attached else None,
            tp_order_id=tp.get("orderId") if attached else None,
            tp_status="RESTING" if attached else "NONE",
            entry_limit=cents(order["limit_price"]) if isinstance(order["limit_price"], Decimal)
            else str(order["limit_price"]))
    if action == "close":
        return await store.record_close(
            str(selection["underlying"]), str(selection["strategy"]), mode,
            str(selection.get("chosen_symbol") or order["symbol"]), alert_id)
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
        report = {"attached": False, "quantity": 0, "qty_open": qty, "status": "NONE", "reason": reason}
        jlog("take_profit", **report)
        return report, None
    limit = take_profit_limit(entry, s.tp_percent)
    tp_order = {"symbol": order["symbol"], "side": "SELL", "open_close": "CLOSE",
                "limit_price": limit, "qty": tp_qty}
    body = order_body(tp_order)
    try:
        reason = await store.try_consume(day, s.mode, Decimal(0), False, s, is_exit=True)
    except (StoreError, httpx.HTTPError) as exc:
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "status": "NONE",
                  "limit_price": cents(limit), "order": body,
                  "reason": f"state store unavailable: {type(exc).__name__}"}
        jlog("take_profit", **report)
        return report, None
    if reason:
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "status": "NONE",
                  "limit_price": cents(limit), "order": body, "reason": reason}
        jlog("take_profit", **report)
        return report, None
    slot = cap_slot(day, s.mode, Decimal(0), False, True, s.exits_count_toward_order_cap)
    try:
        pf = await client.preflight(body)
        pf_body = _json(pf)
    except (httpx.HTTPError, RuntimeError, KeyError) as exc:
        await _release_quiet(store, slot)
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "status": "NONE",
                  "limit_price": cents(limit), "order": body,
                  "reason": f"upstream error: {type(exc).__name__}"}
        jlog("take_profit", **report)
        return report, None
    jlog("take_profit", status=pf.status_code, request=body, response=pf_body, quantity=tp_qty)
    if pf.status_code >= 400:
        await _release_quiet(store, slot)
        report = {"attached": False, "quantity": tp_qty, "qty_open": qty, "status": "NONE",
                  "limit_price": cents(limit), "order": body, "reason": "preflight failed",
                  "preflight_status": pf.status_code, "preflight": pf_body}
        return report, None
    report = {
        "attached": True, "status": "RESTING", "quantity": tp_qty, "qty_open": qty - tp_qty,
        "limit_price": cents(limit), "orderId": str(uuid.uuid4()), "order": body, "preflight": pf_body,
        "note": "reported only; not sent to /order",
    }
    jlog("take_profit", attached=True, quantity=tp_qty, qty_open=report["qty_open"],
         limit_price=report["limit_price"], orderId=report["orderId"], order=body)
    return report, slot


async def _release_quiet(store: "Store", slot: tuple) -> None:
    try:
        await store.release(*slot)
    except (StoreError, httpx.HTTPError):
        jlog("release_failed", slot="take_profit")


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


# --------------------------------------------------------------------------- app
def create_app(settings: Optional[Settings] = None,
               transport: Optional[httpx.AsyncBaseTransport] = None,
               store_transport: Optional[httpx.AsyncBaseTransport] = None,
               clock: Callable[[], datetime] = lambda: datetime.now(ET)) -> FastAPI:
    s = settings or Settings.from_env()
    store = Store(s.supabase_url, s.supabase_key, store_transport)
    client = PublicClient(s, transport)
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
        return {"ok": True, "mode": s.mode, "store_configured": store.configured,
                "public_configured": bool(s.public_secret), "webhook_configured": bool(s.webhook_passphrase),
                "settings": s.public_view()}

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
        jlog("keepalive_ok")
        return {"ok": True}

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
        releases: list[tuple] = []
        selection: Optional[dict] = None
        try:
            order_payload, selection = await prepare_order(payload, s, client, store, now)
            if selection is not None and selection.get("skip_order"):
                await store.log_pick(alert_id, day, s.mode, selection)
                jlog("contract_pick", alert_id=alert_id, selection=selection)
                jlog("flatten", alert_id=alert_id, runners_closed=0,
                     cancel_take_profit=selection.get("cancel_take_profit"))
                reason = await store.record_close(
                    str(selection["underlying"]), str(selection["strategy"]), s.mode,
                    str(selection["chosen_symbol"]), alert_id)
                if reason:
                    await store.finish(alert_id, "REJECTED", mode=s.mode,
                                       detail=audit_detail(selection, reason=reason))
                    return respond(409, error_body(alert_id, reason, selection))
                note = ("No runner contracts remain. The resting take-profit is cancelled and its "
                        "contracts are not sold." if selection.get("cancel_take_profit")
                        else "No runner contracts remain, so no sell is sent.")
                await store.finish(alert_id, "ACCEPTED", "CLOSE", Decimal(0), s.mode,
                                   audit_detail(selection, runners_closed=0, note=note))
                body = {"ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": False,
                        "selection": selection, "runners_closed": 0, "note": note}
                if selection.get("cancel_take_profit"):
                    body["cancel_take_profit"] = selection["cancel_take_profit"]
                return respond(200, body)
            if selection is not None:
                await store.log_pick(alert_id, day, s.mode, selection)
                jlog("contract_pick", alert_id=alert_id, selection=selection)
            o = validate(order_payload, s)
            # Underlying alerts already failed closed on the window before any Public call.
            if selection is None and s.enforce_hours and not in_hours(now):
                raise Reject(hours_error(now))
            is_open = o["open_close"] == "OPEN"
            is_exit = not is_open
            reason = await store.try_consume(day, s.mode, o["notional"], is_open, s, is_exit=is_exit)
            if reason:
                raise Reject(reason)
            releases.append(cap_slot(day, s.mode, o["notional"], is_open, is_exit, s.exits_count_toward_order_cap))
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

        async def persist(take_profit: Optional[dict]) -> Optional[JSONResponse]:
            if selection is None:
                return None
            try:
                reason = await book_position(store, selection, o, s.mode, alert_id, take_profit)
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

        def accepted_body(preflight: Any, placed: bool, take_profit: Optional[dict] = None, **extra: Any) -> dict:
            body = {"ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": placed, **extra}
            if preflight is not None:
                body["preflight"] = preflight
            if selection is not None:
                body["selection"] = selection
                body["order"] = order_body(o)
            if selection is not None and selection.get("action") == "close":
                body["runners_closed"] = selection.get("runners_closed")
                if selection.get("cancel_take_profit"):
                    body["cancel_take_profit"] = selection["cancel_take_profit"]
                    body["note"] = ("Resting take-profit is cancelled. Its contracts are not sold; "
                                    "only the remaining runners are closed.")
            if take_profit is not None:
                body["take_profit"] = take_profit
            return body

        async def accept(preflight: Any, placed: bool, **extra: Any) -> JSONResponse:
            take_profit = None
            if o["open_close"] == "OPEN":
                take_profit, slot = await plan_take_profit(o, s, client, store, day)
                if slot is not None:
                    releases.append(slot)
            blocked = await persist(take_profit)
            if blocked is not None:
                return blocked
            detail: dict[str, Any] = {}
            if preflight is not None:
                detail["preflight"] = preflight
            if selection is not None:
                detail["selection"] = selection
            if take_profit is not None:
                detail["take_profit"] = take_profit
            for key, value in extra.items():
                if value is not None:
                    detail[key] = value
            await store.finish(alert_id, "ACCEPTED", o["open_close"], o["notional"], s.mode, detail or None)
            shown = None if placed else preflight
            return respond(200, accepted_body(shown, placed, take_profit, **extra))

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
