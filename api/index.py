"""TradingView webhook -> Public.com single-leg options relay.

Vercel + Supabase port. Review-only. Defaults to MODE=PREFLIGHT, which never places an order.
LIVE requires BOTH MODE=LIVE and LIVE_CONFIRM=I_UNDERSTAND.

A full OCC symbol is forwarded as sent. An underlying such as SPY selects the 0DTE call (BUY)
or put (SELL), or sells the stored contract when the alert flattens or flips.
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
from datetime import date, datetime, time as dtime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
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
# 0DTE window: ATM through 5 points in the money. |delta| nearest the 0.50–0.60 band,
# then nearest 0.55, then nearest the underlying price.
ITM_POINTS = Decimal("5")
DELTA_LO = Decimal("0.50")
DELTA_HI = Decimal("0.60")
DELTA_MID = Decimal("0.55")
REDACT_KEYS = {"passphrase", "secret", "authorization", "accesstoken", "access_token",
               "public_secret", "webhook_passphrase", "apikey", "supabase_service_key"}


# --------------------------------------------------------------------------- config
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

    @classmethod
    def from_env(cls, env: Optional[dict] = None) -> "Settings":
        e = os.environ if env is None else env
        requested = e.get("MODE", "PREFLIGHT").strip().upper()
        live_ok = requested == "LIVE" and e.get("LIVE_CONFIRM", "") == "I_UNDERSTAND"
        return cls(
            public_secret=e.get("PUBLIC_SECRET", ""),
            webhook_passphrase=e.get("WEBHOOK_PASSPHRASE", ""),
            requested_mode=requested,
            mode="LIVE" if live_ok else "PREFLIGHT",
            account_id=e.get("PUBLIC_ACCOUNT_ID", "").strip(),
            max_qty=int(e.get("MAX_QTY", "1")),
            max_premium=Decimal(e.get("MAX_PREMIUM", "2.00")),
            max_orders_per_day=int(e.get("MAX_ORDERS_PER_DAY", "3")),
            max_daily_opens=Decimal(e.get("MAX_DAILY_OPENS", "300")),
            allowlist={s.strip().upper() for s in e.get("ALLOWLIST", "").split(",") if s.strip()},
            supabase_url=e.get("SUPABASE_URL", "").rstrip("/"),
            supabase_key=e.get("SUPABASE_SERVICE_KEY", ""),
            enforce_hours=e.get("ENFORCE_MARKET_HOURS", "true").lower() != "false",
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

    async def try_consume(self, day: str, mode: str, notional: Decimal, is_open: bool, s: "Settings") -> Optional[str]:
        return await self._rpc("relay_try_consume", {
            "p_day": day, "p_mode": mode, "p_notional": str(notional), "p_is_open": is_open,
            "p_max_orders": s.max_orders_per_day, "p_max_opens": str(s.max_daily_opens)})

    async def release(self, day: str, mode: str, notional: Decimal, is_open: bool) -> None:
        await self._rpc("relay_release", {"p_day": day, "p_mode": mode, "p_notional": str(notional),
                                          "p_is_open": is_open})

    async def get_position(self, underlying: str, strategy: str, mode: str) -> Optional[dict]:
        data = await self._rpc("relay_get_position", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode})
        if isinstance(data, list):
            data = data[0] if data else None
        if not isinstance(data, dict):
            return None
        return data

    async def record_open(self, underlying: str, strategy: str, mode: str, symbol: str, entry_side: str,
                          right_side: str, qty: int, strike: str, alert_id: str) -> Optional[str]:
        data = await self._rpc("relay_record_open", {
            "p_underlying": underlying, "p_strategy": strategy, "p_mode": mode, "p_symbol": symbol,
            "p_entry_side": entry_side, "p_right_side": right_side, "p_qty": qty, "p_strike": strike,
            "p_alert_id": alert_id})
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


def in_window(strike: Decimal, spot: Decimal, right: Literal["C", "P"]) -> bool:
    amount = itm_amount(strike, spot, right)
    return Decimal(0) <= amount <= ITM_POINTS


def band_distance(absolute_delta: Decimal) -> Decimal:
    if absolute_delta < DELTA_LO:
        return DELTA_LO - absolute_delta
    if absolute_delta > DELTA_HI:
        return absolute_delta - DELTA_HI
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
                      fallback: Literal["ask", "bid"]) -> tuple[Decimal, str]:
    mid = quote_mid(bid, ask)
    if mid is not None:
        return mid, "mid"
    raw = ask if fallback == "ask" else bid
    label = "ask" if fallback == "ask" else "bid"
    if raw is None or raw <= 0:
        raise Reject(f"no mid or {label} for the chosen contract")
    price = raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if price <= 0:
        raise Reject(f"no usable {label} for the chosen contract")
    return price, fallback


def choose_leg(legs: list[Leg], spot: Decimal) -> Leg:
    """Pick among already window-filtered legs. |delta| closest to 0.50–0.60, then to 0.55, then to spot."""
    usable = [leg for leg in legs if leg.delta is not None]
    if not usable:
        raise Reject("no delta for strikes from ATM to 5 points ITM")

    def rank(leg: Leg) -> tuple:
        if leg.delta is None:
            raise Reject("no delta for strikes from ATM to 5 points ITM")
        absolute = abs(leg.delta)
        return (band_distance(absolute), abs(absolute - DELTA_MID), abs(leg.strike - spot), leg.strike, leg.symbol)

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


def whole_qty(value: Any) -> int:
    try:
        number = Decimal(str(value))
        if number != number.to_integral_value():
            raise ValueError
        qty = int(number)
    except (InvalidOperation, TypeError, ValueError):
        raise Reject("stored position qty is invalid", 502)
    if qty < 1:
        raise Reject("stored position qty is invalid", 502)
    return qty


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
                     now: datetime, intent: str) -> tuple[dict, dict]:
    side = alert_side(payload, required=True)
    if side is None:
        raise Reject("side must be BUY or SELL")
    right = right_for_entry(side)
    alert_px = alert_underlying_price(payload)
    if alert_px is not None:
        spot, spot_source = alert_px, "alert"
    else:
        spot, spot_source = await equity_spot(client, underlying), "quote"
    expiry = as_et(now).date()
    chain = await client.option_chain(underlying, expiry.isoformat())
    window = [leg for leg in legs_from_chain(chain, underlying, expiry, right) if in_window(leg.strike, spot, right)]
    window.sort(key=lambda leg: (leg.strike, leg.symbol))
    if not window:
        noun = "call" if right == "C" else "put"
        raise Reject(
            f"no {underlying} {noun} strikes from ATM {dec_text(spot)} to 5 points ITM for {expiry.isoformat()}")
    greeks = await fetch_greeks_map(client, [leg.symbol for leg in window])
    window = [with_greeks(leg, greeks) for leg in window]
    chosen = choose_leg(window, spot)
    limit, limit_source = limit_from_market(chosen.bid, chosen.ask, "ask")
    order_payload = {
        "symbol": chosen.symbol, "side": "BUY", "open_close": "OPEN",
        "limit_price": cents(limit), "qty": payload.get("qty"),
    }
    selection = selection_from_leg(
        "open", underlying, strategy, side, "BUY", "OPEN", spot, spot_source, expiry, right,
        window, chosen, limit, limit_source, payload.get("qty"), intent)
    return order_payload, selection


async def build_close(payload: dict, underlying: str, strategy: str, client: "PublicClient",
                      stored: dict, intent: str) -> tuple[dict, dict]:
    symbol = str(stored.get("symbol") or "").upper().replace(" ", "")
    parsed = parse_occ(symbol)
    if not parsed or parsed["underlying"] != underlying:
        raise Reject(f"stored contract {symbol!r} does not match {underlying}", 502)
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
    limit, limit_source = limit_from_market(bid, ask, "bid")
    qty = whole_qty(stored.get("qty"))
    strike = parse_decimal(stored.get("strike")) or parsed["strike"]
    spot = alert_underlying_price(payload)
    chosen = Leg(symbol, strike, delta, delta_source, bid, ask)
    order_payload = {
        "symbol": symbol, "side": "SELL", "open_close": "CLOSE",
        "limit_price": cents(limit), "qty": qty,
    }
    selection = selection_from_leg(
        "close", underlying, strategy, str(stored.get("entry_side") or ""), "SELL", "CLOSE",
        spot, "alert" if spot is not None else None, parsed["expiry"], parsed["right"],
        [chosen], chosen, limit, limit_source, qty, intent)
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
            return await build_open(payload, underlying, strategy, client, now, intent)
        case "close":
            if not stored:
                raise Reject(f"no open position for {underlying}/{strategy}")
            return await build_close(payload, underlying, strategy, client, stored, intent)
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


async def book_position(store: "Store", selection: dict, order: dict, mode: str, alert_id: str) -> Optional[str]:
    action = selection.get("action")
    if action == "open":
        return await store.record_open(
            str(selection["underlying"]), str(selection["strategy"]), mode, order["symbol"],
            str(selection["entry_side"]), str(selection["right"]), int(order["qty"]),
            str(selection["chosen_strike"]), alert_id)
    if action == "close":
        return await store.record_close(
            str(selection["underlying"]), str(selection["strategy"]), mode, order["symbol"], alert_id)
    return f"unhandled selection action {action!r}"


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
         max_daily_opens=s.max_daily_opens)

    def respond(status: int, body: dict, req: Any = None) -> JSONResponse:
        jlog("response", status=status, body=body, request=req)
        return JSONResponse(body, status_code=status)

    @app.get("/health")
    async def health():
        return {"ok": True, "mode": s.mode, "store_configured": store.configured,
                "public_configured": bool(s.public_secret), "webhook_configured": bool(s.webhook_passphrase)}

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
        consumed = None
        selection: Optional[dict] = None
        try:
            order_payload, selection = await prepare_order(payload, s, client, store, now)
            if selection is not None:
                await store.log_pick(alert_id, day, s.mode, selection)
                jlog("contract_pick", alert_id=alert_id, selection=selection)
            o = validate(order_payload, s)
            # Underlying alerts already failed closed on the window before any Public call.
            if selection is None and s.enforce_hours and not in_hours(now):
                raise Reject(hours_error(now))
            is_open = o["open_close"] == "OPEN"
            reason = await store.try_consume(day, s.mode, o["notional"], is_open, s)  # atomic caps
            if reason:
                raise Reject(reason)
            consumed = (day, s.mode, o["notional"], is_open)
        except Reject as e:
            await store.finish(alert_id, "REJECTED", mode=s.mode, detail=audit_detail(selection, reason=e.reason))
            return respond(e.status, error_body(alert_id, e.reason, selection))
        except (StoreError, httpx.HTTPError) as e:  # fail closed
            await store.finish(alert_id, "ERROR", mode=s.mode, detail=audit_detail(selection, error=type(e).__name__))
            return respond(503, error_body(alert_id, f"state store unavailable: {type(e).__name__}", selection))

        async def fail(status_txt: str, detail: Any) -> None:
            await store.finish(alert_id, status_txt, mode=s.mode, detail=detail)
            try:
                await store.release(*consumed)
            except (StoreError, httpx.HTTPError):
                jlog("release_failed", alert_id=alert_id)  # fails safe: cap stays consumed

        async def persist() -> Optional[JSONResponse]:
            if selection is None:
                return None
            try:
                reason = await book_position(store, selection, o, s.mode, alert_id)
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

        def accepted_body(preflight: Any, placed: bool, **extra: Any) -> dict:
            body = {"ok": True, "mode": s.mode, "alert_id": alert_id, "order_placed": placed, **extra}
            if preflight is not None:
                body["preflight"] = preflight
            if selection is not None:
                body["selection"] = selection
                body["order"] = order_body(o)
            return body

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
                blocked = await persist()
                if blocked is not None:
                    return blocked
                detail = pf_body if selection is None else {"preflight": pf_body, "selection": selection}
                await store.finish(alert_id, "ACCEPTED", o["open_close"], o["notional"], s.mode, detail)
                return respond(200, accepted_body(pf_body, False))
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
            await persist()
            await store.finish(alert_id, "ACCEPTED", o["open_close"], o["notional"], s.mode,
                               {"orderId": place_body["orderId"], "response": r_body, **({"selection": selection} if selection else {})})
            return respond(200, accepted_body(None, True, orderId=place_body["orderId"], response=r_body))
        except (httpx.HTTPError, RuntimeError, KeyError) as e:
            # Don't release on transport errors during LIVE place: order may have gone through.
            await store.finish(alert_id, "ERROR", mode=s.mode, detail=audit_detail(selection, error=type(e).__name__))
            return respond(502, error_body(alert_id, f"upstream error: {type(e).__name__}", selection))

    return app


if os.environ.get("RELAY_NO_AUTOAPP") != "1":
    app = create_app()
