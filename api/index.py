"""TradingView webhook -> Public.com single-leg options relay.

Vercel + Supabase port. Review-only. Defaults to MODE=PREFLIGHT, which never places an order.
LIVE requires BOTH MODE=LIVE and LIVE_CONFIRM=I_UNDERSTAND.
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
from datetime import datetime, time as dtime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional
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
MARKET_OPEN = dtime(9, 31)
MARKET_CLOSE = dtime(15, 55)
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

    async def _req(self, method: str, path: str, body: Optional[dict] = None) -> httpx.Response:
        headers = {"Authorization": f"Bearer {await self.token()}"}
        r = await self.http.request(method, path, json=body, headers=headers)
        if r.status_code == 401:  # token revoked/expired early: refresh once and retry
            self._token = None
            headers = {"Authorization": f"Bearer {await self.token()}"}
            r = await self.http.request(method, path, json=body, headers=headers)
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

    # TODO-verify: option quote endpoint path/shape not confirmed; not used by the relay.
    # async def option_expirations(self, underlying): POST /userapigateway/marketdata/{acct}/option-expirations


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
        try:
            o = validate(payload, s)
            if s.enforce_hours and not in_hours(now):
                raise Reject(f"outside trading window 09:31-15:55 ET (now {now:%a %H:%M})")
            is_open = o["open_close"] == "OPEN"
            reason = await store.try_consume(day, s.mode, o["notional"], is_open, s)  # atomic caps
            if reason:
                raise Reject(reason)
            consumed = (day, s.mode, o["notional"], is_open)
        except Reject as e:
            await store.finish(alert_id, "REJECTED", mode=s.mode, detail={"reason": e.reason})
            return respond(e.status, {"ok": False, "alert_id": alert_id, "error": e.reason})
        except (StoreError, httpx.HTTPError) as e:  # fail closed
            await store.finish(alert_id, "ERROR", mode=s.mode, detail={"error": type(e).__name__})
            return respond(503, {"ok": False, "alert_id": alert_id, "error": f"state store unavailable: {type(e).__name__}"})

        async def fail(status_txt: str, detail: Any) -> None:
            await store.finish(alert_id, status_txt, mode=s.mode, detail=detail)
            try:
                await store.release(*consumed)
            except (StoreError, httpx.HTTPError):
                jlog("release_failed", alert_id=alert_id)  # fails safe: cap stays consumed

        body = order_body(o)
        try:
            pf = await client.preflight(body)
            pf_body = _json(pf)
            jlog("preflight", status=pf.status_code, request=body, response=pf_body)
            if pf.status_code >= 400:
                await fail("PREFLIGHT_FAILED", pf_body)
                return respond(502, {"ok": False, "alert_id": alert_id, "stage": "preflight",
                                     "status": pf.status_code, "response": pf_body})
            if s.mode != "LIVE":
                await store.finish(alert_id, "ACCEPTED", o["open_close"], o["notional"], s.mode, pf_body)
                return respond(200, {"ok": True, "mode": s.mode, "alert_id": alert_id,
                                     "preflight": pf_body, "order_placed": False})
            place_body = {**body, "orderId": str(uuid.uuid4())}
            r = await client.place(place_body)
            r_body = _json(r)
            jlog("place_order", status=r.status_code, request=place_body, response=r_body)
            if r.status_code >= 400:
                await fail("ORDER_FAILED", r_body)
                return respond(502, {"ok": False, "alert_id": alert_id, "stage": "order",
                                     "status": r.status_code, "response": r_body})
            await store.finish(alert_id, "ACCEPTED", o["open_close"], o["notional"], s.mode,
                               {"orderId": place_body["orderId"], "response": r_body})
            return respond(200, {"ok": True, "mode": s.mode, "alert_id": alert_id,
                                 "order_placed": True, "orderId": place_body["orderId"],
                                 "response": r_body})
        except (httpx.HTTPError, RuntimeError, KeyError) as e:
            # Don't release on transport errors during LIVE place: order may have gone through.
            await store.finish(alert_id, "ERROR", mode=s.mode, detail={"error": type(e).__name__})
            return respond(502, {"ok": False, "alert_id": alert_id, "error": f"upstream error: {type(e).__name__}"})

    return app


if os.environ.get("RELAY_NO_AUTOAPP") != "1":
    app = create_app()
