import json
import logging
import os
from datetime import date, datetime
from decimal import Decimal

os.environ["RELAY_NO_AUTOAPP"] = "1"

import httpx
import pytest
from fastapi.testclient import TestClient

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
import index as relay  # noqa: E402

ET = relay.ET
OPEN_TIME = datetime(2026, 10, 19, 10, 0, tzinfo=ET)  # Monday 10:00 ET
PASS = "test-pass-123"
SECRET = "test-secret-xyz"


class FakePublic:
    """httpx MockTransport standing in for api.public.com; records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        p = req.url.path
        if p == relay.TOKEN_PATH:
            assert json.loads(req.content)["secret"] == SECRET
            return httpx.Response(200, json={"accessToken": "tok-1"})
        assert req.headers["Authorization"] == "Bearer tok-1"
        if p == relay.ACCOUNT_PATH:
            return httpx.Response(200, json={"accounts": [{"accountId": "ACC1", "optionsLevel": "LEVEL_2"}]})
        if p.endswith("/preflight/single-leg"):
            return httpx.Response(200, json={"estimatedCost": "150.00"})
        if p.endswith("/order"):
            return httpx.Response(200, json={"orderId": json.loads(req.content)["orderId"]})
        return httpx.Response(404)

    def paths(self):
        return [c.url.path for c in self.calls]


class FakeSupabase:
    """In-memory stand-in for PostgREST + the SQL functions in supabase/migrations."""

    def __init__(self, fail=False, missing_position_rpc=False):
        self.alerts, self.counters, self.fail, self.calls = {}, {}, fail, []
        self.positions, self.picks, self.exit_orders = {}, [], {}
        self.missing_position_rpc = missing_position_rpc

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.headers["apikey"] == "svc-key" and req.headers["Authorization"] == "Bearer svc-key"
        if self.fail:
            return httpx.Response(503)
        p = req.url.path
        b = json.loads(req.content) if req.content else {}
        if p == "/rest/v1/rpc/relay_reserve":
            if b["p_alert_id"] in self.alerts:
                return httpx.Response(200, json=False)
            self.alerts[b["p_alert_id"]] = {"status": "RECEIVED"}
            return httpx.Response(200, json=True)
        if p == "/rest/v1/rpc/relay_try_consume":
            key = (b["p_day"], b["p_mode"])
            c = self.counters.setdefault(key, [0, Decimal(0)])
            n = Decimal(b["p_notional"])
            if b.get("p_is_exit"):
                used = self.exit_orders.get(key, 0)
                if used >= int(b.get("p_max_exit_orders", 6)):
                    return httpx.Response(200, json=f"MAX_EXIT_ORDERS_PER_DAY ({b.get('p_max_exit_orders', 6)}) reached")
                if b.get("p_exit_counts") and c[0] >= b["p_max_orders"]:
                    return httpx.Response(200, json=f"MAX_ORDERS_PER_DAY ({b['p_max_orders']}) reached")
                self.exit_orders[key] = used + 1
                if b.get("p_exit_counts"):
                    c[0] += 1
                return httpx.Response(200, json=None)
            if c[0] >= b["p_max_orders"]:
                return httpx.Response(200, json=f"MAX_ORDERS_PER_DAY ({b['p_max_orders']}) reached")
            if b["p_is_open"] and c[1] + n > Decimal(b["p_max_opens"]):
                return httpx.Response(200, json=f"daily OPEN spend cap: spent ${c[1]} + ${n} > ${b['p_max_opens']}")
            c[0] += 1
            c[1] += n if b["p_is_open"] else 0
            return httpx.Response(200, json=None)
        if p == "/rest/v1/rpc/relay_release":
            key = (b["p_day"], b["p_mode"])
            if b.get("p_is_exit"):
                self.exit_orders[key] = self.exit_orders.get(key, 0) - 1
                if b.get("p_exit_counts"):
                    self.counters[key][0] -= 1
                return httpx.Response(204)
            c = self.counters[key]
            c[0] -= 1
            c[1] -= Decimal(b["p_notional"]) if b["p_is_open"] else 0
            return httpx.Response(204)
        if p == "/rest/v1/daily_counters" and req.method == "GET":
            return httpx.Response(200, json=[])
        if p == "/rest/v1/processed_alerts" and req.method == "PATCH":
            aid = req.url.params["alert_id"].removeprefix("eq.")
            self.alerts[aid].update(b)
            return httpx.Response(204)
        if self.missing_position_rpc and (
            p in ("/rest/v1/rpc/relay_get_position", "/rest/v1/rpc/relay_record_open",
                  "/rest/v1/rpc/relay_record_close", "/rest/v1/rpc/relay_list_open_positions",
                  "/rest/v1/rpc/relay_save_paper", "/rest/v1/contract_picks")
        ):
            return httpx.Response(404, json={"message": "missing"})
        if p == "/rest/v1/rpc/relay_get_position":
            row = self.positions.get((b["p_underlying"], b["p_strategy"], b["p_mode"]))
            if not row or row["status"] != "OPEN":
                return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
            return httpx.Response(200, json=row)
        if p == "/rest/v1/rpc/relay_record_open":
            key = (b["p_underlying"], b["p_strategy"], b["p_mode"])
            existing = self.positions.get(key)
            if existing and existing["status"] == "OPEN":
                return httpx.Response(200, json=f"position already open for {key[0]}/{key[1]}")
            self.positions[key] = {
                "underlying": key[0], "strategy": key[1], "mode": key[2], "symbol": b["p_symbol"],
                "entry_side": b["p_entry_side"], "right_side": b["p_right_side"], "qty": b["p_qty"],
                "qty_open": b.get("p_qty_open", b["p_qty"]), "strike": b["p_strike"],
                "opened_alert_id": b["p_alert_id"], "status": "OPEN",
                "tp_qty": b.get("p_tp_qty", 0), "tp_filled": b.get("p_tp_filled", 0),
                "tp_limit": b.get("p_tp_limit"), "tp_order_id": b.get("p_tp_order_id"),
                "tp_status": b.get("p_tp_status", "NONE"), "entry_limit": b.get("p_entry_limit"),
                "stop_qty": b.get("p_stop_qty", 0), "stop_filled": b.get("p_stop_filled", 0),
                "stop_limit": b.get("p_stop_limit"), "stop_order_id": b.get("p_stop_order_id"),
                "stop_status": b.get("p_stop_status", "NONE"),
                "entry_order_id": b.get("p_entry_order_id"),
                "entry_status": b.get("p_entry_status", "NONE"),
                "entry_filled": b.get("p_entry_filled", 0),
                "exits_placed": b.get("p_exits_placed") is True,
            }
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        if p == "/rest/v1/rpc/relay_record_close":
            key = (b["p_underlying"], b["p_strategy"], b["p_mode"])
            row = self.positions.get(key)
            if not row or row["status"] != "OPEN" or row["symbol"] != b["p_symbol"]:
                return httpx.Response(200, json=f"no open position for {key[0]}/{key[1]}")
            row["status"] = "CLOSED"
            row["closed_alert_id"] = b["p_alert_id"]
            row["qty_open"] = 0
            if b.get("p_tp_status"):
                row["tp_status"] = b["p_tp_status"]
            elif row.get("tp_status") in ("RESTING", "PARTIAL"):
                row["tp_status"] = "CANCELLED"
            if b.get("p_stop_status"):
                row["stop_status"] = b["p_stop_status"]
            elif row.get("stop_status") in ("RESTING", "PARTIAL"):
                row["stop_status"] = "CANCELLED"
            if b.get("p_tp_filled") is not None:
                row["tp_filled"] = b["p_tp_filled"]
            if b.get("p_stop_filled") is not None:
                row["stop_filled"] = b["p_stop_filled"]
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        if p == "/rest/v1/rpc/relay_list_open_positions":
            rows = [row for row in self.positions.values()
                    if row["status"] == "OPEN" and row["mode"] == b["p_mode"]]
            return httpx.Response(200, json=rows)
        if p == "/rest/v1/rpc/relay_save_paper":
            key = (b["p_underlying"], b["p_strategy"], b["p_mode"])
            row = self.positions.get(key)
            if not row or row["status"] != "OPEN":
                return httpx.Response(200, json=f"no open position for {key[0]}/{key[1]}")
            row.update({
                "entry_order_id": b.get("p_entry_order_id"),
                "entry_status": b.get("p_entry_status") or row.get("entry_status") or "NONE",
                "entry_filled": 0 if b.get("p_entry_filled") in (None, "") else b.get("p_entry_filled"),
                "tp_qty": b.get("p_tp_qty") or 0,
                "tp_filled": 0 if b.get("p_tp_filled") in (None, "") else b.get("p_tp_filled"),
                "tp_limit": b.get("p_tp_limit"),
                "tp_order_id": b.get("p_tp_order_id"),
                "tp_status": b.get("p_tp_status") or "NONE",
                "stop_qty": b.get("p_stop_qty") or 0,
                "stop_filled": 0 if b.get("p_stop_filled") in (None, "") else b.get("p_stop_filled"),
                "stop_limit": b.get("p_stop_limit"),
                "stop_order_id": b.get("p_stop_order_id"),
                "stop_status": b.get("p_stop_status") or "NONE",
                "exits_placed": b.get("p_exits_placed") is True,
                "qty_open": b.get("p_qty_open"),
            })
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        if p == "/rest/v1/contract_picks" and req.method == "POST":
            self.picks.append(b)
            return httpx.Response(201, content=b"", headers={"content-type": "application/json"})
        return httpx.Response(404)


async def _no_sleep(_seconds: float) -> None:
    return None


class FakeAlpaca:
    """httpx stand-in for https://paper-api.alpaca.markets. Buy-to-open fills unless entry_status says otherwise."""

    def __init__(self, missing=None, ppind=False, entry_status="filled"):
        self.orders = {}
        self.calls = []
        self.deletes = []
        self.missing = set(missing or [])
        self.ppind = ppind
        self.entry_status = entry_status
        self.account = {"status": "ACTIVE", "equity": "100000", "cash": "50", "buying_power": "999"}
        self._seq = 0

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        assert req.headers["APCA-API-KEY-ID"] == "alpaca-key"
        assert req.headers["APCA-API-SECRET-KEY"] == "alpaca-secret"
        path = req.url.path
        if path == "/v2/account" and req.method == "GET":
            return httpx.Response(200, json=self.account)
        if path.startswith("/v2/options/contracts/") and req.method == "GET":
            symbol = path.rsplit("/", 1)[-1]
            if symbol in self.missing:
                return httpx.Response(404, json={"message": "contract not found"})
            return httpx.Response(200, json={
                "symbol": symbol, "status": "active", "tradable": True, "ppind": self.ppind,
            })
        if path == "/v2/orders" and req.method == "POST":
            body = json.loads(req.content)
            self._seq += 1
            oid = f"alp-{self._seq}"
            intent = body.get("position_intent")
            status = self.entry_status if intent == "buy_to_open" else "new"
            filled = body["qty"] if status == "filled" else "0"
            order = {
                "id": oid, "status": status, "filled_qty": filled,
                "filled_avg_price": body.get("limit_price") if status == "filled" else None,
                "symbol": body.get("symbol"), "qty": body.get("qty"), "type": body.get("type"),
                "side": body.get("side"), "limit_price": body.get("limit_price"),
                "stop_price": body.get("stop_price"), "position_intent": intent,
                "client_order_id": body.get("client_order_id"), "time_in_force": body.get("time_in_force"),
            }
            self.orders[oid] = order
            return httpx.Response(200, json=order)
        if req.method == "GET" and path.startswith("/v2/orders/"):
            order = self.orders.get(path.rsplit("/", 1)[-1])
            if order is None:
                return httpx.Response(404, json={"message": "order not found"})
            return httpx.Response(200, json=order)
        if req.method == "DELETE" and path.startswith("/v2/orders/"):
            oid = path.rsplit("/", 1)[-1]
            self.deletes.append(oid)
            order = self.orders.get(oid)
            if order is None:
                return httpx.Response(404, json={"message": "order not found"})
            if order["status"] == "filled":
                return httpx.Response(422, json={"message": "order is already filled"})
            order["status"] = "canceled"
            return httpx.Response(204)
        return httpx.Response(404, json={"message": path})

    def posts(self):
        return [json.loads(call.content) for call in self.calls
                if call.method == "POST" and call.url.path == "/v2/orders"]


def make(tmp_path, mode="PREFLIGHT", confirm="", clock=lambda: OPEN_TIME, sb=None, public=None,
         alpaca=None, **over):
    env = {"PUBLIC_SECRET": SECRET, "WEBHOOK_PASSPHRASE": PASS, "MODE": mode, "LIVE_CONFIRM": confirm,
           "ALLOWLIST": "AAPL,SPY", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_KEY": "svc-key", **over}
    fake = public or FakePublic()
    sb = sb or FakeSupabase()
    alpaca_transport = None
    if mode == "PAPER":
        env.setdefault("ALPACA_KEY_ID", "alpaca-key")
        env.setdefault("ALPACA_SECRET_KEY", "alpaca-secret")
        alpaca = alpaca if alpaca is not None else FakeAlpaca()
        alpaca_transport = httpx.MockTransport(alpaca)
    a = relay.create_app(relay.Settings.from_env(env), transport=httpx.MockTransport(fake),
                         store_transport=httpx.MockTransport(sb), clock=clock,
                         alpaca_transport=alpaca_transport, sleeper=_no_sleep)
    c = TestClient(a)
    c.sb = sb
    c.alpaca = alpaca
    return c, fake


def alert(**kw):
    base = {"passphrase": PASS, "symbol": "AAPL261120C00250000", "side": "BUY", "open_close": "OPEN",
            "limit_price": "1.50", "qty": 1, "alert_id": "a1"}
    base.update(kw)
    return base


def test_auth_reject(tmp_path):
    c, fake = make(tmp_path)
    r = c.post("/webhook", json=alert(passphrase="wrong"))
    assert r.status_code == 401
    assert fake.calls == []


def test_duplicate_ignored(tmp_path):
    c, fake = make(tmp_path)
    assert c.post("/webhook", json=alert()).status_code == 200
    n = len(fake.calls)
    r = c.post("/webhook", json=alert())
    assert r.status_code == 200 and r.json()["duplicate"] is True
    assert len(fake.calls) == n


def test_qty_cap(tmp_path):
    c, fake = make(tmp_path)
    r = c.post("/webhook", json=alert(qty=2))
    assert r.status_code == 422 and "MAX_QTY" in r.json()["error"]
    assert fake.calls == []


def test_premium_cap(tmp_path):
    c, fake = make(tmp_path)
    r = c.post("/webhook", json=alert(limit_price="5.01"))
    assert r.status_code == 422 and "MAX_PREMIUM" in r.json()["error"]
    assert fake.calls == []
    assert c.post("/webhook", json=alert(limit_price="5.00", alert_id="at-cap")).status_code == 200


def test_premium_cap_skips_closes(tmp_path):
    c, fake = make(tmp_path, MAX_PREMIUM="2.00")
    opened = c.post("/webhook", json=alert(limit_price="2.01", alert_id="open"))
    assert opened.status_code == 422 and "MAX_PREMIUM" in opened.json()["error"]
    closed = c.post("/webhook", json=alert(side="SELL", open_close="CLOSE", limit_price="7.50", alert_id="close"))
    assert closed.status_code == 200, closed.text
    assert fake.calls


def test_validate_caps_opens_only():
    s = relay.Settings.from_env({"MAX_PREMIUM": "2.00", "ALLOWLIST": "AAPL"})
    base = {"symbol": "AAPL261120C00250000", "limit_price": "9.00", "qty": 1}
    with pytest.raises(relay.Reject, match="MAX_PREMIUM"):
        relay.validate(base | {"side": "BUY", "open_close": "OPEN"}, s)
    assert relay.validate(base | {"side": "SELL", "open_close": "CLOSE"}, s)["limit_price"] == Decimal("9.00")


def test_preflight_mode_never_calls_order(tmp_path):
    c, fake = make(tmp_path)
    r = c.post("/webhook", json=alert())
    assert r.status_code == 200, r.text
    assert r.json()["order_placed"] is False
    assert "selection" not in r.json()
    assert r.json()["take_profit"]["attached"] is False and r.json()["take_profit"]["quantity"] == 0
    assert r.json()["stop"]["attached"] is False
    assert r.json()["stop"]["reason"] == "STOP_LOSS_PERCENT is not set"
    assert len(ending(fake, "/preflight/single-leg")) == 1
    assert not any(p.endswith("/order") for p in fake.paths())


def test_live_without_confirm_stays_preflight(tmp_path):
    c, fake = make(tmp_path, mode="LIVE", confirm="yes")
    assert c.post("/webhook", json=alert()).json()["mode"] == "PREFLIGHT"
    assert not any(p.endswith("/order") for p in fake.paths())


def test_live_mode_calls_order_once(tmp_path):
    c, fake = make(tmp_path, mode="LIVE", confirm="I_UNDERSTAND")
    r = c.post("/webhook", json=alert())
    assert r.status_code == 200, r.text
    assert r.json()["order_placed"] is True
    orders = [x for x in fake.calls if x.url.path.endswith("/order")]
    assert len(orders) == 1
    body = json.loads(orders[0].content)
    assert orders[0].url.path == "/userapigateway/trading/ACC1/order"
    assert body["instrument"] == {"symbol": "AAPL261120C00250000", "type": "OPTION"}
    assert body["orderType"] == "LIMIT" and body["limitPrice"] == "1.50" and body["quantity"] == "1"
    assert body["openCloseIndicator"] == "OPEN" and body["orderId"]
    # token fetched once and cached
    assert fake.paths().count(relay.TOKEN_PATH) == 1


def test_allowlist_and_hours(tmp_path):
    c, _ = make(tmp_path)
    assert c.post("/webhook", json=alert(symbol="TSLA261120C00250000")).status_code == 422
    late = tmp_path / "late"
    late.mkdir()
    c2, fake2 = make(late, clock=lambda: datetime(2026, 10, 19, 15, 56, tzinfo=ET))
    r = c2.post("/webhook", json=alert(alert_id="late"))
    assert r.status_code == 422 and "window" in r.json()["error"]
    assert fake2.calls == []


def test_daily_caps(tmp_path):
    c, _ = make(tmp_path, MAX_PREMIUM="2.00", MAX_DAILY_OPENS="300")
    assert c.post("/webhook", json=alert(alert_id="1", limit_price="1.50")).status_code == 200  # $150
    assert c.post("/webhook", json=alert(alert_id="2", limit_price="1.50")).status_code == 200  # $300
    r = c.post("/webhook", json=alert(alert_id="3", limit_price="0.10"))
    assert r.status_code == 422 and "spend cap" in r.json()["error"]
    # Closes are exits. They stay inside the spend rule and do not consume the entry order cap.
    assert c.post("/webhook", json=alert(alert_id="4", side="SELL", open_close="CLOSE")).status_code == 200
    assert c.post("/webhook", json=alert(alert_id="5", side="SELL", open_close="CLOSE")).status_code == 200
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 2
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 2


def test_secrets_not_logged(tmp_path, capsys):
    c, _ = make(tmp_path, mode="LIVE", confirm="I_UNDERSTAND")
    c.post("/webhook", json=alert())
    c.post("/webhook", json=alert(alert_id="b", passphrase="wrong"))
    out = capsys.readouterr().out
    assert PASS not in out and SECRET not in out and "tok-1" not in out and "svc-key" not in out
    for line in out.strip().splitlines():
        json.loads(line)  # every log line is JSON


def test_accepted_recorded_in_supabase(tmp_path):
    c, _ = make(tmp_path)
    assert c.post("/webhook", json=alert()).status_code == 200
    row = c.sb.alerts["a1"]
    assert row["status"] == "ACCEPTED" and row["notional"] == "150.00" and row["mode"] == "PREFLIGHT"


def test_store_down_fails_closed(tmp_path):
    c, fake = make(tmp_path, sb=FakeSupabase(fail=True), mode="LIVE", confirm="I_UNDERSTAND")
    r = c.post("/webhook", json=alert())
    assert r.status_code == 503
    assert fake.calls == []  # nothing sent to Public


def test_preflight_failure_releases_cap(tmp_path):
    class BadPF(FakePublic):
        def __call__(self, req):
            if req.url.path.endswith("/preflight/single-leg"):
                self.calls.append(req)
                return httpx.Response(400, json={"error": "nope"})
            return super().__call__(req)
    c, _ = make(tmp_path, public=BadPF())
    assert c.post("/webhook", json=alert()).status_code == 502
    assert c.sb.alerts["a1"]["status"] == "PREFLIGHT_FAILED"
    assert list(c.sb.counters.values()) == [[0, Decimal(0)]]


def test_token_cached_across_requests(tmp_path):
    c, fake = make(tmp_path)
    c.post("/webhook", json=alert(alert_id="x1"))
    c.post("/webhook", json=alert(alert_id="x2"))
    assert fake.paths().count(relay.TOKEN_PATH) == 1


def test_missing_supabase_env_fails_closed():
    c = TestClient(relay.create_app(relay.Settings.from_env({"WEBHOOK_PASSPHRASE": PASS, "ALLOWLIST": "AAPL"})))
    assert c.get("/health").json()["store_configured"] is False
    assert c.post("/webhook", json=alert()).status_code == 503


def test_keepalive(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    c, fake = make(tmp_path)
    assert c.get("/keepalive").status_code == 401
    assert c.get("/keepalive", headers={"Authorization": "Bearer nope"}).status_code == 401
    r = c.get("/keepalive", headers={"Authorization": "Bearer cron-s"})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert "reconciled" not in r.json()
    assert any(x.url.path == "/rest/v1/daily_counters" for x in c.sb.calls)
    assert not any(x.url.path.endswith("relay_list_open_positions") for x in c.sb.calls)
    assert fake.calls == []  # never touches Public


CALLS = [
    ("574", "0.90", "6.40", "6.60"),  # 6 points ITM, outside the window
    ("575", "0.80", "5.20", "5.40"),
    ("576", "0.72", "4.10", "4.30"),
    ("577", "0.66", "3.20", "3.40"),
    ("578", "0.61", "2.30", "2.50"),
    ("579", "0.55", "1.40", "1.50"),  # |delta| on the 0.55 center of the band
    ("580", "0.49", "0.80", "0.95"),
    ("581", "0.42", "0.30", "0.45"),  # OTM
]
PUTS = [
    ("579", "-0.42", "0.70", "0.85"),  # OTM put
    ("580", "-0.49", "1.00", "1.15"),
    ("581", "-0.55", "1.20", "1.40"),
    ("582", "-0.63", "2.10", "2.30"),
    ("585", "-0.80", "4.50", "4.70"),  # exactly 5 points ITM
    ("586", "-0.88", "5.40", "5.60"),
]
CHOSEN_CALL = "SPY261019C00579000"
CHOSEN_PUT = "SPY261019P00581000"


def occ(strike, right, underlying="SPY", yymmdd="261019"):
    millis = int(Decimal(strike) * 1000)
    return f"{underlying}{yymmdd}{right}{millis:08d}"


def chain_row(symbol, strike, delta, bid, ask):
    return {
        "instrument": {"symbol": symbol, "type": "OPTION"},
        "outcome": "SUCCESS",
        "bid": bid,
        "ask": ask,
        "optionDetails": {"strikePrice": strike, "greeks": {"delta": delta} if delta is not None else {}},
    }


def spy_chain(call_delta=None, put_delta=None, calls=CALLS, puts=PUTS, yymmdd="261019"):
    return {
        "baseSymbol": "SPY",
        "calls": [chain_row(occ(s, "C", yymmdd=yymmdd), s, call_delta if call_delta is not None else d, b, a)
                  for s, d, b, a in calls],
        "puts": [chain_row(occ(s, "P", yymmdd=yymmdd), s, put_delta if put_delta is not None else d, b, a)
                 for s, d, b, a in puts],
    }


def greek_map(rows, right, yymmdd="261019"):
    return {occ(strike, right, yymmdd=yymmdd): delta for strike, delta, _bid, _ask in rows}


class ChainPublic(FakePublic):
    """Public.com stand-in that also serves the option chain, greeks, and quotes."""

    def __init__(self, chain=None, greeks=None, quotes=None, greeks_status=200):
        super().__init__()
        self.chain = spy_chain() if chain is None else chain
        self.greeks = greek_map(CALLS, "C") | greek_map(PUTS, "P") if greeks is None else greeks
        self.quotes = quotes or {}
        self.greeks_status = greeks_status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/option-chain"):
            self.calls.append(req)
            assert req.headers["Authorization"] == "Bearer tok-1"
            return httpx.Response(200, json=self.chain)
        if path.endswith("/greeks"):
            self.calls.append(req)
            assert req.headers["Authorization"] == "Bearer tok-1"
            if self.greeks_status >= 400:
                return httpx.Response(self.greeks_status, json={"error": "greeks down"})
            rows = [{"symbol": sym, "greeks": {"delta": self.greeks[sym]}}
                    for sym in req.url.params.get_list("osiSymbols") if sym in self.greeks]
            return httpx.Response(200, json={"greeks": rows})
        if path.endswith("/quotes"):
            self.calls.append(req)
            assert req.headers["Authorization"] == "Bearer tok-1"
            symbol = json.loads(req.content)["instruments"][0]["symbol"]
            quote = self.quotes.get(symbol)
            if quote is None:
                return httpx.Response(404, json={"error": "no quote"})
            return httpx.Response(200, json={"quotes": [quote]})
        return super().__call__(req)


class StatusPublic(ChainPublic):
    """GET /order/{id} returns a canned broker status. Other calls follow the chain stand-in."""

    def __init__(self, statuses=None, order_status=200, **kw):
        super().__init__(**kw)
        self.statuses = {} if statuses is None else statuses
        self.order_status = order_status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "GET" and "/order/" in path:
            self.calls.append(req)
            assert req.headers["Authorization"] == "Bearer tok-1"
            if self.order_status >= 400 and self.order_status != 404:
                return httpx.Response(self.order_status, json={"error": "status down"})
            payload = self.statuses.get(path.rsplit("/", 1)[-1])
            if payload is None:
                return httpx.Response(404, json={"message": "not found"})
            return httpx.Response(200, json=payload)
        return super().__call__(req)


def underlying(**kw):
    base = {
        "passphrase": PASS, "symbol": "SPY", "side": "buy", "qty": 1, "underlying_price": "580",
        "strategy": "FlowVWAP", "alert_id": "spy-1", "market_position": "long", "prev_market_position": "flat",
    }
    base.update(kw)
    return base


def ending(fake, suffix):
    return [call for call in fake.calls if call.url.path.endswith(suffix)]


def flatten_alert(alert_id):
    return underlying(qty=99, side="sell", market_position="flat", prev_market_position="long", alert_id=alert_id)


def saved_position(client, mode="PREFLIGHT", strategy="FlowVWAP"):
    return client.sb.positions[("SPY", strategy, mode)]


def option_quote(symbol, bid, ask, delta="0.57"):
    return {
        "instrument": {"symbol": symbol, "type": "OPTION"},
        "outcome": "SUCCESS",
        "bid": bid,
        "ask": ask,
        "optionDetails": {"strikePrice": "579", "greeks": {"delta": delta}},
    }


def test_occ_symbol_window_and_delta_band():
    assert relay.occ_symbol("SPY", date(2026, 10, 19), "C", Decimal("579")) == CHOSEN_CALL
    assert relay.occ_symbol("SPY", date(2026, 10, 19), "P", Decimal("581.5")) == "SPY261019P00581500"
    spot = Decimal("580")
    assert relay.in_window(Decimal("575"), spot, "C")
    assert not relay.in_window(Decimal("574.99"), spot, "C")
    assert relay.in_window(Decimal("580"), spot, "C")
    assert not relay.in_window(Decimal("580.01"), spot, "C")
    assert relay.in_window(Decimal("585"), spot, "P")
    assert not relay.in_window(Decimal("579.99"), spot, "P")

    def leg(strike, delta):
        return relay.Leg(f"S{strike}", Decimal(strike), Decimal(delta), "greeks", Decimal("1"), Decimal("1.10"))

    # Both inside the band and equally far from 0.55: the strike nearer ATM wins.
    assert relay.choose_leg([leg("579", "0.59"), leg("580", "0.51")], spot).symbol == "S580"
    # 0.55 beats another in-band delta that is farther from the center.
    assert relay.choose_leg([leg("580", "0.51"), leg("579", "0.55")], spot).symbol == "S579"
    # Outside the band, the smaller distance to the band wins over a deeper strike.
    assert relay.choose_leg([leg("578", "0.62"), leg("580", "0.48"), leg("577", "0.70")], spot).symbol == "S580"
    limit, source = relay.limit_from_market(None, Decimal("1.37"), "ask")
    assert (limit, source) == (Decimal("1.37"), "ask")
    limit, source = relay.limit_from_market(Decimal("0.80"), None, "bid")
    assert (limit, source) == (Decimal("0.80"), "bid")
    limit, source = relay.limit_from_market(Decimal("1.40"), Decimal("1.50"), "ask")
    assert (limit, source) == (Decimal("1.45"), "mid")


def test_underlying_buy_selects_call_and_stays_preflight(tmp_path):
    public = ChainPublic()
    c, fake = make(tmp_path, public=public)
    r = c.post("/webhook", json=underlying())
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "PREFLIGHT" and body["order_placed"] is False
    assert not ending(fake, "/order")
    sel = body["selection"]
    assert sel["chosen_symbol"] == CHOSEN_CALL
    assert sel["chosen_strike"] == "579" and Decimal(sel["delta"]) == Decimal("0.55")
    assert sel["underlying_price"] == "580" and sel["underlying_price_source"] == "alert"
    assert sel["limit"] == "1.45" and sel["limit_source"] == "mid"
    assert sel["bid"] == "1.40" and sel["ask"] == "1.50" and sel["mid"] == "1.45"
    assert sel["intent"] == "from_flat" and sel["right"] == "C" and sel["order_side"] == "BUY"
    strikes = {row["strike"] for row in sel["candidates"]}
    assert strikes == {"575", "576", "577", "578", "579", "580"}
    assert body["order"]["instrument"]["symbol"] == CHOSEN_CALL
    assert body["order"]["orderSide"] == "BUY" and body["order"]["openCloseIndicator"] == "OPEN"
    assert body["order"]["limitPrice"] == "1.45" and body["order"]["orderType"] == "LIMIT"
    chain = json.loads(ending(fake, "/option-chain")[0].content)
    assert chain == {"instrument": {"symbol": "SPY", "type": "EQUITY"}, "expirationDate": "2026-10-19"}
    requested = ending(fake, "/greeks")[0].url.params.get_list("osiSymbols")
    assert occ("579", "C") in requested and occ("575", "C") in requested
    assert occ("574", "C") not in requested and occ("581", "C") not in requested
    pick = c.sb.picks[0]
    assert pick["chosen_symbol"] == CHOSEN_CALL and pick["limit_price"] == "1.45"
    assert pick["underlying_price"] == "580" and pick["candidates"]
    assert c.sb.alerts["spy-1"]["status"] == "ACCEPTED"
    assert c.sb.alerts["spy-1"]["detail"]["selection"]["chosen_symbol"] == CHOSEN_CALL
    pos = c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert pos["status"] == "OPEN" and pos["symbol"] == CHOSEN_CALL
    assert pos["entry_side"] == "BUY" and pos["right_side"] == "C" and pos["qty"] == 1
    assert PASS not in r.text and "passphrase" not in json.dumps(pick)


def test_greeks_override_chain_and_fallback(tmp_path):
    # Chain deltas are a flat 0.90, which would pick the 580 call. Greeks put 0.55 on the 579.
    public = ChainPublic(chain=spy_chain(call_delta="0.90"), greeks=greek_map(CALLS, "C"))
    c, fake = make(tmp_path, public=public)
    body = c.post("/webhook", json=underlying(alert_id="greeks")).json()
    assert body["selection"]["chosen_symbol"] == CHOSEN_CALL
    assert body["selection"]["delta_source"] == "greeks"
    assert not ending(fake, "/order")

    fallback = ChainPublic(greeks_status=500)
    c2, fake2 = make(tmp_path / "fb", public=fallback)
    body = c2.post("/webhook", json=underlying(alert_id="fallback")).json()
    assert body["selection"]["chosen_symbol"] == CHOSEN_CALL
    assert body["selection"]["delta_source"] == "chain"
    assert ending(fake2, "/greeks")  # the endpoint was called, then the chain delta was used


def test_sell_selects_put(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic())
    r = c.post("/webhook", json=underlying(
        side="sell", market_position="short", prev_market_position="flat", alert_id="spy-put"))
    assert r.status_code == 200, r.text
    sel = r.json()["selection"]
    assert sel["chosen_symbol"] == CHOSEN_PUT and sel["right"] == "P"
    assert Decimal(sel["delta"]) == Decimal("-0.55")
    assert sel["limit"] == "1.30" and sel["limit_source"] == "mid"
    assert r.json()["order"]["orderSide"] == "BUY" and r.json()["order"]["openCloseIndicator"] == "OPEN"
    pos = c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert pos["entry_side"] == "SELL" and pos["right_side"] == "P" and pos["symbol"] == CHOSEN_PUT
    assert not ending(fake, "/order")


def test_open_falls_back_to_ask_when_mid_missing(tmp_path):
    calls = [("579", "0.55", None, "1.37"), ("580", "0.40", "0.20", "0.30"), ("575", "0.80", "5", "5.2")]
    public = ChainPublic(chain=spy_chain(calls=calls, puts=[]), greeks=greek_map(calls, "C"))
    c, fake = make(tmp_path, public=public)
    body = c.post("/webhook", json=underlying(alert_id="ask")).json()
    assert body["selection"]["limit"] == "1.37" and body["selection"]["limit_source"] == "ask"
    assert body["selection"]["mid"] is None and body["selection"]["bid"] is None
    assert body["order"]["limitPrice"] == "1.37"
    assert not ending(fake, "/order")


def test_quote_fallback_for_underlying_price(tmp_path):
    quotes = {"SPY": {"instrument": {"symbol": "SPY", "type": "EQUITY"}, "outcome": "SUCCESS", "last": "580"}}
    c, fake = make(tmp_path, public=ChainPublic(quotes=quotes))
    payload = underlying(alert_id="quoted")
    del payload["underlying_price"]
    body = c.post("/webhook", json=payload).json()
    assert body["selection"]["chosen_symbol"] == CHOSEN_CALL
    assert body["selection"]["underlying_price_source"] == "quote"
    assert Decimal(body["selection"]["underlying_price"]) == Decimal("580")
    quote = json.loads(ending(fake, "/quotes")[0].content)
    assert quote["instruments"] == [{"symbol": "SPY", "type": "EQUITY"}]


def test_flatten_and_opposite_side_close_stored_contract(tmp_path):
    quote = {CHOSEN_CALL: option_quote(CHOSEN_CALL, "1.10", "1.30", "0.10")}
    greeks = greek_map(CALLS, "C") | {CHOSEN_CALL: "0.57"}
    public = ChainPublic(quotes=quote, greeks=greeks)
    c, fake = make(tmp_path, public=public, MAX_ORDERS_PER_DAY="10", MAX_DAILY_OPENS="1000")
    assert c.post("/webhook", json=underlying()).status_code == 200
    flat = underlying(side="sell", market_position="flat", prev_market_position="long",
                      alert_id="spy-flat", underlying_price="580.10", qty=99)
    r = c.post("/webhook", json=flat)
    assert r.status_code == 200, r.text
    sel = r.json()["selection"]
    assert sel["action"] == "close" and sel["intent"] == "flat"
    assert sel["chosen_symbol"] == CHOSEN_CALL and sel["order_side"] == "SELL"
    assert sel["limit"] == "1.20" and sel["limit_source"] == "mid"
    assert Decimal(sel["delta"]) == Decimal("0.57") and sel["delta_source"] == "greeks"
    assert Decimal(sel["underlying_price"]) == Decimal("580.10")
    assert r.json()["order"]["openCloseIndicator"] == "CLOSE"
    assert r.json()["order"]["quantity"] == "1"  # stored qty, not the alert's 99
    assert r.json()["order"]["limitPrice"] == "1.20"
    assert c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "CLOSED"
    assert len(ending(fake, "/option-chain")) == 1  # the close quotes the stored contract
    assert not ending(fake, "/order")
    # A second flatten has nothing left to close.
    again = c.post("/webhook", json=underlying(alert_id="spy-flat-2", side="sell", market_position="flat",
                                               prev_market_position="long"))
    assert again.status_code == 422 and "no open position" in again.json()["error"]

    # Re-open, then an opposite-side alert closes instead of buying a put. A reversal does too.
    assert c.post("/webhook", json=underlying(alert_id="spy-re")).status_code == 200
    opp = underlying(side="sell", alert_id="spy-opp", market_position=None, prev_market_position=None)
    r = c.post("/webhook", json=opp)
    assert r.status_code == 200, r.text
    assert r.json()["selection"]["intent"] == "opposite_side"
    assert r.json()["order"]["instrument"]["symbol"] == CHOSEN_CALL
    assert r.json()["order"]["orderSide"] == "SELL"
    assert "P00581000" not in r.text

    assert c.post("/webhook", json=underlying(alert_id="spy-re2")).status_code == 200
    rev = underlying(side="sell", alert_id="spy-rev", prev_market_position="long", market_position="short")
    r = c.post("/webhook", json=rev)
    assert r.json()["selection"]["intent"] == "reversal"
    assert r.json()["order"]["instrument"]["symbol"] == CHOSEN_CALL
    assert c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "CLOSED"


def test_close_falls_back_to_bid(tmp_path):
    quote = {CHOSEN_CALL: option_quote(CHOSEN_CALL, "0.80", None)}
    c, _ = make(tmp_path, public=ChainPublic(quotes=quote))
    assert c.post("/webhook", json=underlying()).status_code == 200
    payload = underlying(alert_id="spy-bid", open_close="CLOSE", side="sell", market_position=None,
                         prev_market_position=None)
    del payload["qty"]
    r = c.post("/webhook", json=payload)
    assert r.status_code == 200, r.text
    assert r.json()["selection"]["intent"] == "explicit_close"
    assert r.json()["selection"]["limit_source"] == "bid" and r.json()["selection"]["limit"] == "0.80"
    assert r.json()["selection"]["mid"] is None
    assert r.json()["order"]["quantity"] == "1"


def test_same_side_open_rejected_and_duplicate_skips_chain(tmp_path):
    public = ChainPublic(quotes={CHOSEN_CALL: option_quote(CHOSEN_CALL, "1.10", "1.30")})
    c, fake = make(tmp_path, public=public)
    assert c.post("/webhook", json=underlying()).status_code == 200
    chains = len(ending(fake, "/option-chain"))
    dup = c.post("/webhook", json=underlying())
    assert dup.status_code == 200 and dup.json()["duplicate"] is True
    assert len(ending(fake, "/option-chain")) == chains
    add = c.post("/webhook", json=underlying(alert_id="spy-add", prev_market_position="long"))
    assert add.status_code == 422 and "already open" in add.json()["error"]
    assert len(ending(fake, "/option-chain")) == chains
    # Explicit OPEN on the opposite side still closes the stored call.
    forced = underlying(side="sell", open_close="OPEN", alert_id="spy-force",
                        market_position=None, prev_market_position=None)
    r = c.post("/webhook", json=forced)
    assert r.status_code == 200, r.text
    assert r.json()["selection"]["action"] == "close" and r.json()["selection"]["intent"] == "opposite_side"


def test_underlying_keeps_safety_checks(tmp_path, capsys):
    c, fake = make(tmp_path, public=ChainPublic())
    missing = c.post("/webhook", json=underlying(symbol="IWM", alert_id="iwm"))
    assert missing.status_code == 422 and "ALLOWLIST" in missing.json()["error"]
    bad_oc = c.post("/webhook", json=underlying(open_close="MAYBE", alert_id="maybe"))
    assert bad_oc.status_code == 422 and "open_close" in bad_oc.json()["error"]
    assert fake.calls == []

    late_public = ChainPublic()
    c_late, fake_late = make(tmp_path / "late", public=late_public,
                             clock=lambda: datetime(2026, 10, 19, 15, 56, tzinfo=ET))
    late = c_late.post("/webhook", json=underlying(alert_id="late"))
    assert late.status_code == 422 and "window" in late.json()["error"]
    assert fake_late.calls == []

    rich = [("579", "0.55", "5.00", "5.40")]
    c_px, fake_px = make(tmp_path / "px", public=ChainPublic(chain=spy_chain(calls=rich, puts=[]),
                                                             greeks=greek_map(rich, "C")))
    priced = c_px.post("/webhook", json=underlying(alert_id="rich"))
    assert priced.status_code == 422 and "MAX_PREMIUM" in priced.json()["error"]
    assert priced.json()["selection"]["chosen_symbol"] == CHOSEN_CALL
    assert c_px.sb.picks and not c_px.sb.positions
    assert not ending(fake_px, "/preflight/single-leg") and not ending(fake_px, "/order")

    c_cap, _ = make(tmp_path / "cap", public=ChainPublic(), MAX_DAILY_OPENS="300")
    assert c_cap.post("/webhook", json=underlying(alert_id="a", strategy="A")).status_code == 200  # $145
    assert c_cap.post("/webhook", json=underlying(alert_id="b", strategy="B")).status_code == 200  # $290
    third = c_cap.post("/webhook", json=underlying(alert_id="c", strategy="C"))
    assert third.status_code == 422 and "spend cap" in third.json()["error"]
    assert third.json()["selection"]["limit"] == "1.45"
    assert ("SPY", "C", "PREFLIGHT") not in c_cap.sb.positions

    out = capsys.readouterr().out
    assert PASS not in out and SECRET not in out and "tok-1" not in out


def test_empty_chain_and_position_rpc_failure(tmp_path):
    empty = {"baseSymbol": "SPY", "calls": [], "puts": []}
    c, fake = make(tmp_path, public=ChainPublic(chain=empty, greeks={}))
    r = c.post("/webhook", json=underlying(alert_id="empty"))
    assert r.status_code == 422 and "no SPY call strikes" in r.json()["error"]
    assert not c.sb.picks and not ending(fake, "/preflight/single-leg")

    class Down(ChainPublic):
        def __call__(self, req):
            if req.url.path.endswith("/option-chain"):
                self.calls.append(req)
                return httpx.Response(400, json={"error": "no chain"})
            return FakePublic.__call__(self, req)

    c2, _ = make(tmp_path / "down", public=Down())
    r = c2.post("/webhook", json=underlying(alert_id="down"))
    assert r.status_code == 502 and "option chain failed" in r.json()["error"]
    assert not c2.sb.positions

    c3, fake3 = make(tmp_path / "mig", sb=FakeSupabase(missing_position_rpc=True), public=ChainPublic())
    occ_ok = c3.post("/webhook", json=alert(alert_id="occ"))
    assert occ_ok.status_code == 200 and occ_ok.json()["order_placed"] is False
    missing = c3.post("/webhook", json=underlying(alert_id="need-migration"))
    assert missing.status_code == 503 and "unavailable" in missing.json()["error"]
    assert not ending(fake3, "/option-chain")


def test_underlying_preflight_failure_releases_and_does_not_store_position(tmp_path):
    class BadPreflight(ChainPublic):
        def __call__(self, req):
            if req.url.path.endswith("/preflight/single-leg"):
                self.calls.append(req)
                return httpx.Response(400, json={"error": "nope"})
            return super().__call__(req)

    c, fake = make(tmp_path, public=BadPreflight())
    r = c.post("/webhook", json=underlying(alert_id="pf"))
    assert r.status_code == 502
    assert r.json()["selection"]["chosen_symbol"] == CHOSEN_CALL
    assert c.sb.alerts["pf"]["status"] == "PREFLIGHT_FAILED"
    assert not c.sb.positions
    assert list(c.sb.counters.values()) == [[0, Decimal(0)]]
    assert not ending(fake, "/order")


def test_underlying_pick_does_not_log_passphrase(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    c, fake = make(tmp_path, public=ChainPublic(), MAX_QTY="2", MAX_DAILY_OPENS="1000")
    r = c.post("/webhook", json=underlying(qty=2))
    assert r.status_code == 200, r.text
    assert r.json()["take_profit"]["attached"] is True
    blob = r.text + caplog.text + json.dumps(c.sb.picks) + json.dumps(c.sb.alerts)
    assert PASS not in blob and SECRET not in blob
    assert '"event": "take_profit"' in caplog.text
    assert not ending(fake, "/order")


def test_settings_defaults_overrides_and_live_gate():
    defaults = relay.Settings.from_env({})
    assert defaults.mode == "PREFLIGHT"
    assert defaults.delta_band_min == Decimal("0.50") and defaults.delta_band_max == Decimal("0.60")
    assert defaults.itm_points == Decimal("5") and defaults.expiry_days == 0
    assert defaults.open_limit_fallback == "ask" and defaults.close_limit_fallback == "bid"
    assert defaults.max_orders_per_day == 3 and defaults.max_daily_opens == Decimal("1500")
    assert defaults.max_premium == Decimal("5.00")
    assert defaults.tp_percent == Decimal("20") and defaults.tp_fraction == Decimal("0.50")
    assert defaults.tp_single is False and defaults.stop_loss_percent is None
    assert defaults.exits_count_toward_order_cap is False
    assert defaults.max_exit_orders_per_day == 6
    assert relay.Settings.from_env({"STOP_LOSS_PERCENT": "  "}).stop_loss_percent is None
    assert relay.Settings.from_env({"STOP_LOSS_PERCENT": "30"}).stop_loss_percent == Decimal("30")
    with pytest.raises(ValueError):
        relay.Settings.from_env({"STOP_LOSS_PERCENT": "0"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({"STOP_LOSS_PERCENT": "100"})
    assert relay.stop_price(Decimal("1.45"), Decimal("30")) == Decimal("1.02")
    assert relay.round_option_price(Decimal("1.015")) == Decimal("1.02")
    assert relay.option_tick(Decimal("3")) == Decimal("0.05")
    assert relay.round_option_price(Decimal("3")) == Decimal("3.00")
    assert relay.round_option_price(Decimal("4.51")) == Decimal("4.50")
    assert relay.round_option_price(Decimal("4.51"), penny=True) == Decimal("4.51")
    assert relay.price_text(Decimal("1.015")) == "1.015"
    assert relay.price_text(Decimal("1.020")) == "1.02"
    assert relay.price_text(Decimal("1")) == "1.00"
    blank = relay.Settings.from_env({"TP_PERCENT": "  ", "EXPIRY_DAYS": "", "ITM_POINTS": ""})
    assert blank.tp_percent == Decimal("20") and blank.expiry_days == 0 and blank.itm_points == Decimal("5")
    over = relay.Settings.from_env({
        "DELTA_BAND_MIN": "0.40", "DELTA_BAND_MAX": "0.70", "ITM_POINTS": "2", "EXPIRY_DAYS": "2",
        "OPEN_LIMIT_FALLBACK": "bid", "CLOSE_LIMIT_FALLBACK": "reject", "TP_PERCENT": "15",
        "TP_FRACTION": "0.25", "TP_SINGLE": "true", "MAX_EXIT_ORDERS_PER_DAY": "4",
        "EXITS_COUNT_TOWARD_ORDER_CAP": "yes", "MAX_ORDERS_PER_DAY": "5", "MAX_PREMIUM": "3.00",
        "MAX_DAILY_OPENS": "900",
    })
    assert over.delta_band_min == Decimal("0.40") and over.expiry_days == 2
    assert over.open_limit_fallback == "bid" and over.close_limit_fallback == "reject"
    assert over.tp_single is True and over.exits_count_toward_order_cap is True
    assert over.max_exit_orders_per_day == 4 and over.max_orders_per_day == 5
    assert relay.Settings.from_env({"MODE": "LIVE"}).mode == "PREFLIGHT"
    assert relay.Settings.from_env({"MODE": "LIVE", "LIVE_CONFIRM": "I_UNDERSTAND"}).mode == "LIVE"
    hidden = relay.Settings.from_env({
        "MODE": "PREFLIGHT", "ALPACA_KEY_ID": "alpaca-key", "ALPACA_SECRET_KEY": "alpaca-secret",
        "ALPACA_BASE_URL": "https://api.alpaca.markets",
    })
    assert hidden.mode == "PREFLIGHT" and hidden.alpaca_key_id == "" and hidden.alpaca_secret_key == ""
    assert hidden.alpaca_base_url == ""
    paper = relay.Settings.from_env({"MODE": "paper", "ALPACA_KEY_ID": "alpaca-key", "ALPACA_SECRET_KEY": "alpaca-secret"})
    assert paper.mode == "PAPER" and paper.alpaca_base_url == "https://paper-api.alpaca.markets"
    assert paper.alpaca_key_id == "alpaca-key" and paper.public_view()["alpaca_configured"] is True
    assert "alpaca-secret" not in json.dumps(paper.public_view())
    with pytest.raises(ValueError):
        relay.Settings.from_env({"MODE": "PAPER"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({
            "MODE": "PAPER", "ALPACA_KEY_ID": "alpaca-key", "ALPACA_SECRET_KEY": "alpaca-secret",
            "ALPACA_BASE_URL": "https://api.alpaca.markets",
        })
    with pytest.raises(ValueError):
        relay.Settings.from_env({"DELTA_BAND_MIN": "0.80", "DELTA_BAND_MAX": "0.20"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({"EXPIRY_DAYS": "31"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({"OPEN_LIMIT_FALLBACK": "mid"})
    assert relay.take_profit_qty(1, Decimal("0.50"), False) == 0
    assert relay.take_profit_qty(1, Decimal("0.50"), True) == 1
    assert relay.take_profit_qty(2, Decimal("0.50"), False) == 1
    assert relay.take_profit_qty(3, Decimal("0.50"), False) == 1
    assert relay.take_profit_qty(4, Decimal("0.50"), False) == 2
    assert relay.take_profit_qty(2, Decimal("0.10"), False) == 1
    assert relay.take_profit_qty(4, Decimal("0"), False) == 0
    assert relay.take_profit_limit(Decimal("1.45"), Decimal("20")) == Decimal("1.74")
    assert relay.take_profit_limit(Decimal("1.90"), Decimal("20")) == Decimal("2.28")
    with pytest.raises(relay.Reject):
        relay.limit_from_market(None, Decimal("1.37"), "reject")
    assert relay.limit_from_market(Decimal("1.11"), None, "bid") == (Decimal("1.11"), "bid")


def test_health_lists_tunables(tmp_path):
    c, _ = make(tmp_path, DELTA_BAND_MIN="0.42", TP_PERCENT="15", EXPIRY_DAYS="3")
    settings = c.get("/health").json()["settings"]
    assert settings["delta_band_min"] == "0.42" and settings["delta_band_max"] == "0.60"
    assert settings["itm_points"] == "5" and settings["expiry_days"] == 3
    assert settings["tp_percent"] == "15" and settings["tp_fraction"] == "0.50"
    assert settings["tp_single"] is False and settings["exits_count_toward_order_cap"] is False
    assert settings["max_orders_per_day"] == 3 and settings["max_daily_opens"] == "1500"
    assert settings["max_premium"] == "5.00" and settings["max_exit_orders_per_day"] == 6
    assert settings["open_limit_fallback"] == "ask" and settings["close_limit_fallback"] == "bid"
    assert settings["stop_loss_percent"] is None and settings["alpaca_configured"] is False
    assert PASS not in json.dumps(settings) and "secret" not in json.dumps(settings)
    health = c.get("/health").json()
    assert health["mode"] == "PREFLIGHT" and health["alpaca_configured"] is False and "alpaca" not in health


def test_env_changes_band_window_expiry_and_fallback(tmp_path):
    band = ChainPublic()
    c, fake = make(tmp_path, public=band, DELTA_BAND_MIN="0.75", DELTA_BAND_MAX="0.85")
    body = c.post("/webhook", json=underlying(alert_id="band")).json()
    assert body["selection"]["chosen_strike"] == "575"
    assert Decimal(body["selection"]["delta"]) == Decimal("0.80")
    assert not ending(fake, "/order")

    narrow = ChainPublic()
    c_itm, _ = make(tmp_path / "itm", public=narrow, ITM_POINTS="1")
    selected = c_itm.post("/webhook", json=underlying(alert_id="itm")).json()["selection"]
    assert {row["strike"] for row in selected["candidates"]} == {"579", "580"}
    assert selected["chosen_symbol"] == CHOSEN_CALL

    yymmdd = "261021"
    chain = spy_chain(yymmdd=yymmdd)
    greeks = greek_map(CALLS, "C", yymmdd) | greek_map(PUTS, "P", yymmdd)
    c_exp, fake_exp = make(tmp_path / "exp", public=ChainPublic(chain=chain, greeks=greeks), EXPIRY_DAYS="2")
    opened = c_exp.post("/webhook", json=underlying(alert_id="exp")).json()
    assert opened["selection"]["expiry"] == "2026-10-21"
    assert opened["selection"]["chosen_symbol"] == "SPY261021C00579000"
    requested = json.loads(ending(fake_exp, "/option-chain")[0].content)
    assert requested["expirationDate"] == "2026-10-21"

    calls = [("579", "0.55", "1.11", None), ("580", "0.40", "0.20", "0.30")]
    c_bid, _ = make(tmp_path / "bid", OPEN_LIMIT_FALLBACK="bid",
                    public=ChainPublic(chain=spy_chain(calls=calls, puts=[]), greeks=greek_map(calls, "C")))
    priced = c_bid.post("/webhook", json=underlying(alert_id="bid-open")).json()
    assert priced["selection"]["limit"] == "1.11" and priced["selection"]["limit_source"] == "bid"

    c_no, fake_no = make(tmp_path / "reject", OPEN_LIMIT_FALLBACK="reject",
                         public=ChainPublic(chain=spy_chain(calls=calls, puts=[]), greeks=greek_map(calls, "C")))
    rejected = c_no.post("/webhook", json=underlying(alert_id="no-mid"))
    assert rejected.status_code == 422 and "no mid" in rejected.json()["error"]
    assert not c_no.sb.positions and not ending(fake_no, "/preflight/single-leg")

    c_ask, _ = make(tmp_path / "ask-close", public=ChainPublic(
        quotes={CHOSEN_CALL: option_quote(CHOSEN_CALL, None, "0.95")}), CLOSE_LIMIT_FALLBACK="ask")
    assert c_ask.post("/webhook", json=underlying(alert_id="to-close")).status_code == 200
    closed = c_ask.post("/webhook", json=underlying(
        alert_id="ask-close", side="sell", open_close="CLOSE", market_position=None, prev_market_position=None))
    assert closed.status_code == 200, closed.text
    assert closed.json()["selection"]["limit_source"] == "ask" and closed.json()["selection"]["limit"] == "0.95"


def test_partial_take_profit_and_runner_flatten(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    quote = {CHOSEN_CALL: option_quote(CHOSEN_CALL, "1.10", "1.30", "0.10")}
    c, fake = make(tmp_path, public=ChainPublic(quotes=quote), MAX_QTY="4", MAX_DAILY_OPENS="1000",
                   MAX_ORDERS_PER_DAY="10")
    opened = c.post("/webhook", json=underlying(qty=4, alert_id="open4"))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    tp = body["take_profit"]
    assert tp["attached"] is True and tp["status"] == "RESTING"
    assert tp["quantity"] == 2 and tp["qty_open"] == 4 and tp["runners"] == 2 and tp["limit_price"] == "1.74"
    assert body["stop"]["attached"] is False
    assert tp["order"]["orderSide"] == "SELL" and tp["order"]["openCloseIndicator"] == "CLOSE"
    assert tp["order"]["quantity"] == "2" and tp["order"]["limitPrice"] == "1.74"
    assert "orderId" not in tp["order"] and tp["orderId"]
    assert body["order_placed"] is False and not ending(fake, "/order")
    preflights = ending(fake, "/preflight/single-leg")
    assert len(preflights) == 2
    assert json.loads(preflights[0].content)["openCloseIndicator"] == "OPEN"
    tp_request = json.loads(preflights[1].content)
    assert tp_request["limitPrice"] == "1.74" and "orderId" not in tp_request
    pos = c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert pos["qty"] == 4 and pos["qty_open"] == 4 and pos["tp_qty"] == 2
    assert pos["tp_status"] == "RESTING" and pos["tp_order_id"] == tp["orderId"]
    assert pos["entry_limit"] == "1.45" and pos["tp_limit"] == "1.74"
    assert '"event": "take_profit"' in caplog.text and '"limitPrice": "1.74"' in caplog.text
    assert PASS not in opened.text and PASS not in caplog.text

    flat = c.post("/webhook", json=underlying(
        qty=99, side="sell", market_position="flat", prev_market_position="long", alert_id="flat4"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["contracts_closed"] == 4 and closed["runners_closed"] == 2
    assert closed["take_profit_contracts_closed"] == 2
    assert closed["order"]["quantity"] == "4" and closed["order"]["openCloseIndicator"] == "CLOSE"
    assert closed["order"]["limitPrice"] == "1.20"
    assert closed["cancel_take_profit"]["orderId"] == tp["orderId"]
    assert closed["cancel_take_profit"]["quantity"] == 2
    assert "included in this sell-to-close" in closed["cancel_take_profit"]["note"]
    assert "included in this sell-to-close" in closed["note"]
    saved = c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert saved["status"] == "CLOSED" and saved["tp_status"] == "CANCELLED" and saved["qty_open"] == 0
    assert len(ending(fake, "/preflight/single-leg")) == 3
    assert not ending(fake, "/order")


def test_take_profit_fraction_and_single_contract(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic(), MAX_QTY="4", MAX_DAILY_OPENS="5000")
    three = c.post("/webhook", json=underlying(qty=3, alert_id="q3", strategy="Q3"))
    assert three.status_code == 200, three.text
    assert three.json()["take_profit"]["quantity"] == 1
    assert three.json()["take_profit"]["runners"] == 2 and three.json()["take_profit"]["qty_open"] == 3
    two = c.post("/webhook", json=underlying(qty=2, alert_id="q2", strategy="Q2"))
    assert two.json()["take_profit"]["quantity"] == 1
    assert two.json()["take_profit"]["runners"] == 1 and two.json()["take_profit"]["qty_open"] == 2
    one = c.post("/webhook", json=underlying(qty=1, alert_id="q1", strategy="Q1"))
    assert one.json()["take_profit"]["attached"] is False and one.json()["take_profit"]["quantity"] == 0
    assert c.sb.positions[("SPY", "Q1", "PREFLIGHT")]["qty_open"] == 1
    assert c.sb.positions[("SPY", "Q1", "PREFLIGHT")]["tp_status"] == "NONE"
    # One preflight per single-contract open; the two larger opens each add a take-profit preflight.
    assert len(ending(fake, "/preflight/single-leg")) == 5
    assert not ending(fake, "/order")


def test_tp_single_flatten_cancels_and_sells(tmp_path):
    quote = {CHOSEN_CALL: option_quote(CHOSEN_CALL, "1.10", "1.30")}
    c, fake = make(tmp_path, public=ChainPublic(quotes=quote), TP_SINGLE="true")
    opened = c.post("/webhook", json=underlying(alert_id="one"))
    assert opened.status_code == 200, opened.text
    tp = opened.json()["take_profit"]
    assert tp["attached"] is True and tp["quantity"] == 1 and tp["qty_open"] == 1 and tp["runners"] == 0
    assert len(ending(fake, "/preflight/single-leg")) == 2
    flat = c.post("/webhook", json=underlying(
        side="sell", market_position="flat", prev_market_position="long", alert_id="flat-one", qty=99))
    assert flat.status_code == 200, flat.text
    body = flat.json()
    assert body["contracts_closed"] == 1 and body["runners_closed"] == 0
    assert body["take_profit_contracts_closed"] == 1
    assert body["order"]["quantity"] == "1" and body["order"]["limitPrice"] == "1.20"
    assert body["order_placed"] is False
    assert body["cancel_take_profit"]["orderId"] == tp["orderId"]
    assert body["cancel_take_profit"]["quantity"] == 1
    assert "included in this sell-to-close" in body["note"]
    assert "included in this sell-to-close" in body["cancel_take_profit"]["note"]
    assert len(ending(fake, "/preflight/single-leg")) == 3
    assert ending(fake, "/quotes")
    assert not ending(fake, "/order")
    saved = c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert saved["status"] == "CLOSED" and saved["tp_status"] == "CANCELLED" and saved["qty_open"] == 0


def test_exits_do_not_consume_the_entry_cap(tmp_path):
    c, fake = make(tmp_path, MAX_QTY="2", MAX_DAILY_OPENS="1000")
    for i in range(3):
        r = c.post("/webhook", json=alert(alert_id=f"o{i}", qty=2, limit_price="1.00"))
        assert r.status_code == 200, r.text
        tp = r.json()["take_profit"]
        assert tp["attached"] is True and tp["quantity"] == 1 and tp["limit_price"] == "1.20"
        assert "orderId" not in tp["order"] and tp["order"]["openCloseIndicator"] == "CLOSE"
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 3
    blocked = c.post("/webhook", json=alert(alert_id="o4", qty=2, limit_price="1.00"))
    assert blocked.status_code == 422 and "MAX_ORDERS_PER_DAY" in blocked.json()["error"]
    closed = c.post("/webhook", json=alert(alert_id="c1", side="SELL", open_close="CLOSE", qty=1))
    assert closed.status_code == 200, closed.text
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 4
    assert not ending(fake, "/order")
    assert ("AAPL", "default", "PREFLIGHT") not in c.sb.positions


def test_exits_can_count_toward_the_order_cap(tmp_path):
    c, _ = make(tmp_path, EXITS_COUNT_TOWARD_ORDER_CAP="true")
    assert c.post("/webhook", json=alert(alert_id="1", limit_price="1.50")).status_code == 200
    assert c.post("/webhook", json=alert(alert_id="2", limit_price="1.50")).status_code == 200
    assert c.post("/webhook", json=alert(alert_id="4", side="SELL", open_close="CLOSE")).status_code == 200
    blocked = c.post("/webhook", json=alert(alert_id="5", side="SELL", open_close="CLOSE"))
    assert blocked.status_code == 422 and "MAX_ORDERS_PER_DAY" in blocked.json()["error"]
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 1


def test_exit_cap_skips_take_profit_and_keeps_the_entry(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic(), MAX_QTY="2", MAX_DAILY_OPENS="2000",
                   MAX_EXIT_ORDERS_PER_DAY="2", MAX_ORDERS_PER_DAY="10")
    for name in ("A", "B", "C"):
        r = c.post("/webhook", json=underlying(alert_id=name, strategy=name, qty=2))
        assert r.status_code == 200, r.text
        if name == "C":
            tp = r.json()["take_profit"]
    assert c.sb.positions[("SPY", "A", "PREFLIGHT")]["tp_status"] == "RESTING"
    assert c.sb.positions[("SPY", "A", "PREFLIGHT")]["qty_open"] == 2
    assert c.sb.positions[("SPY", "A", "PREFLIGHT")]["tp_qty"] == 1
    assert tp["attached"] is False and tp["quantity"] == 1
    assert "MAX_EXIT_ORDERS_PER_DAY" in tp["reason"]
    assert "order" in tp  # the sell that was not sent
    saved = c.sb.positions[("SPY", "C", "PREFLIGHT")]
    assert saved["status"] == "OPEN" and saved["qty"] == 2 and saved["qty_open"] == 2
    assert saved["tp_status"] == "NONE" and saved["tp_qty"] == 0
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 2
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3
    assert not ending(fake, "/order")


def test_take_profit_preflight_failure_keeps_the_entry(tmp_path):
    class TpFails(ChainPublic):
        def __init__(self):
            super().__init__()
            self.preflights = 0

        def __call__(self, req):
            if req.url.path.endswith("/preflight/single-leg"):
                self.calls.append(req)
                self.preflights += 1
                if self.preflights >= 2:
                    return httpx.Response(400, json={"error": "tp nope"})
                return httpx.Response(200, json={"estimatedCost": "ok"})
            return super().__call__(req)

    c, fake = make(tmp_path, public=TpFails(), MAX_QTY="2", MAX_DAILY_OPENS="1000")
    r = c.post("/webhook", json=underlying(qty=2, alert_id="tp-fail"))
    assert r.status_code == 200, r.text
    tp = r.json()["take_profit"]
    assert tp["attached"] is False and tp["reason"] == "preflight failed" and tp["preflight_status"] == 400
    pos = c.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert pos["status"] == "OPEN" and pos["qty"] == 2 and pos["qty_open"] == 2 and pos["tp_status"] == "NONE"
    assert c.sb.exit_orders.get(("2026-10-19", "PREFLIGHT"), 0) == 0
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 1
    assert not ending(fake, "/order")


def test_take_profit_limit_may_exceed_max_premium(tmp_path):
    calls = [("579", "0.55", "1.80", "2.00")]
    c, fake = make(tmp_path, MAX_QTY="2", MAX_DAILY_OPENS="1000", MAX_PREMIUM="2.00",
                   public=ChainPublic(chain=spy_chain(calls=calls, puts=[]), greeks=greek_map(calls, "C")))
    r = c.post("/webhook", json=underlying(qty=2, alert_id="rich-tp"))
    assert r.status_code == 200, r.text
    assert r.json()["selection"]["limit"] == "1.90"
    tp = r.json()["take_profit"]
    assert tp["attached"] is True and tp["limit_price"] == "2.28"
    assert json.loads(ending(fake, "/preflight/single-leg")[1].content)["limitPrice"] == "2.28"
    assert not ending(fake, "/order")


def test_live_open_does_not_place_the_take_profit(tmp_path):
    c, fake = make(tmp_path, mode="LIVE", confirm="I_UNDERSTAND", MAX_QTY="2")
    r = c.post("/webhook", json=alert(qty=2, limit_price="1.00"))
    assert r.status_code == 200, r.text
    assert r.json()["order_placed"] is True
    assert r.json()["take_profit"]["attached"] is True
    orders = ending(fake, "/order")
    assert len(orders) == 1
    assert len(ending(fake, "/preflight/single-leg")) == 2
    entry_id = json.loads(orders[0].content)["orderId"]
    assert r.json()["orderId"] == entry_id
    assert r.json()["take_profit"]["orderId"] != entry_id
    assert "orderId" not in r.json()["take_profit"]["order"]
    assert "orderId" not in json.loads(ending(fake, "/preflight/single-leg")[1].content)
    assert r.json()["stop"]["attached"] is False


CLOSE_QUOTE = {CHOSEN_CALL: option_quote(CHOSEN_CALL, "1.10", "1.30")}


def test_exit_plan_clamps_fills_and_sizes_the_flatten():
    loss = relay.exit_plan({
        "qty": 2, "tp_status": "RESTING", "tp_qty": 1, "tp_filled": 0, "tp_order_id": "tp-1",
        "stop_status": "RESTING", "stop_qty": 2, "stop_filled": 0, "stop_order_id": "st-1",
    })
    assert loss["still_open"] == 2 and loss["runners"] == 1 and loss["take_profit_contracts_closed"] == 1
    assert loss["cancel_take_profit"]["quantity"] == 1 and loss["cancel_stop"]["quantity"] == 2

    filled_whole = relay.exit_plan({
        "qty": 2, "tp_status": "FILLED", "tp_qty": 1, "tp_filled": 0, "tp_order_id": "tp-1",
        "stop_status": "RESTING", "stop_qty": 2, "stop_filled": 0, "stop_order_id": "st-1",
    })
    assert filled_whole["tp_filled"] == 1 and filled_whole["still_open"] == 1
    assert filled_whole["cancel_take_profit"] is None
    assert filled_whole["stop_reduced_to"] == 1
    assert filled_whole["cancel_stop"]["working_quantity"] == 1

    stopped = relay.exit_plan({
        "qty": 2, "tp_status": "RESTING", "tp_qty": 1, "tp_filled": 0, "tp_order_id": "tp-1",
        "stop_status": "FILLED", "stop_qty": 2, "stop_filled": 0, "stop_order_id": "st-1",
    })
    assert stopped["still_open"] == 0 and stopped["stop_filled"] == 2
    assert stopped["cancel_stop"] is None and stopped["stop_status_after"] == "FILLED"
    assert stopped["cancel_take_profit"]["quantity"] == 1

    partial = relay.exit_plan({
        "qty": 4, "tp_status": "PARTIAL", "tp_qty": 2, "tp_filled": 1, "tp_order_id": "tp-1",
        "stop_status": "NONE",
    })
    assert partial["still_open"] == 3 and partial["runners"] == 2
    assert partial["take_profit_contracts_closed"] == 1
    assert partial["cancel_take_profit"]["quantity"] == 1 and partial["cancel_take_profit"]["reason"] == "partial"

    assert relay.clamp_fills(4, 5, 5) == (4, 0)
    assert relay.clamp_fills(4, 3, 3) == (3, 1)
    with pytest.raises(relay.Reject):
        relay.stored_exit({"tp_status": "PARTIAL", "tp_qty": 2}, "tp")
    with pytest.raises(relay.Reject):
        relay.broker_fill({"status": "PARTIALLY_FILLED"}, 2)
    with pytest.raises(relay.Reject):
        relay.broker_fill({"status": "NOPE", "filledQuantity": "1"}, 2)


def test_qty2_loss_flatten_sells_the_unfilled_take_profit_and_the_runners(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic(quotes=CLOSE_QUOTE), MAX_QTY="2", MAX_DAILY_OPENS="1000",
                   STOP_LOSS_PERCENT="30")
    opened = c.post("/webhook", json=underlying(qty=2, alert_id="open2"))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    tp, stop = body["take_profit"], body["stop"]
    assert body["order_placed"] is False and body["selection"]["limit"] == "1.45"
    assert tp["attached"] is True and tp["quantity"] == 1 and tp["qty_open"] == 2 and tp["runners"] == 1
    assert tp["limit_price"] == "1.74" and "orderId" not in tp["order"]
    assert stop["attached"] is True and stop["status"] == "RESTING" and stop["quantity"] == 2
    assert stop["order_type"] == "STOP_LIMIT" and stop["stop_price"] == "1.02" and stop["limit_price"] == "1.02"
    assert stop["order"]["orderType"] == "STOP_LIMIT" and stop["order"]["quantity"] == "2"
    assert stop["order"]["stopPrice"] == "1.02" and stop["order"]["limitPrice"] == "1.02"
    assert stop["order"]["orderSide"] == "SELL" and stop["order"]["openCloseIndicator"] == "CLOSE"
    assert "orderId" not in stop["order"] and stop["orderId"] and "reported only" in stop["note"]
    preflights = ending(fake, "/preflight/single-leg")
    assert len(preflights) == 3
    stop_request = json.loads(preflights[2].content)
    assert stop_request["orderType"] == "STOP_LIMIT" and stop_request["stopPrice"] == "1.02"
    assert "orderId" not in stop_request
    assert c.get("/health").json()["settings"]["stop_loss_percent"] == "30"
    pos = saved_position(c)
    assert pos["qty_open"] == 2 and pos["tp_qty"] == 1 and pos["stop_qty"] == 2
    assert pos["stop_status"] == "RESTING" and pos["stop_limit"] == "1.02" and pos["tp_filled"] == 0
    assert not any(call.method == "GET" and "/order/" in call.url.path for call in fake.calls)
    assert not ending(fake, "/order")

    flat = c.post("/webhook", json=flatten_alert("flat-loss"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["contracts_closed"] == 2 and closed["runners_closed"] == 1
    assert closed["take_profit_contracts_closed"] == 1
    assert closed["order"]["quantity"] == "2" and closed["order"]["limitPrice"] == "1.20"
    assert closed["order"]["openCloseIndicator"] == "CLOSE" and closed["order_placed"] is False
    assert closed["cancel_take_profit"]["orderId"] == tp["orderId"] and closed["cancel_take_profit"]["quantity"] == 1
    assert closed["cancel_stop"]["orderId"] == stop["orderId"] and closed["cancel_stop"]["quantity"] == 2
    assert "included in this sell-to-close" in closed["note"]
    assert "Resting stop is cancelled." in closed["note"]
    saved = saved_position(c)
    assert saved["status"] == "CLOSED" and saved["qty_open"] == 0
    assert saved["tp_status"] == "CANCELLED" and saved["stop_status"] == "CANCELLED"
    assert saved["tp_filled"] == 0 and saved["stop_filled"] == 0
    assert len(ending(fake, "/preflight/single-leg")) == 4
    assert c.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 1
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 3
    assert not ending(fake, "/order")


def test_qty2_win_flattens_runners_after_the_take_profit_fills(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic(quotes=CLOSE_QUOTE), MAX_QTY="2", MAX_DAILY_OPENS="1000",
                   STOP_LOSS_PERCENT="30")
    opened = c.post("/webhook", json=underlying(qty=2, alert_id="open-win"))
    assert opened.status_code == 200, opened.text
    stop_id = opened.json()["stop"]["orderId"]
    pos = saved_position(c)
    pos["tp_status"] = "FILLED"
    pos["tp_filled"] = 1
    flat = c.post("/webhook", json=flatten_alert("flat-win"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["contracts_closed"] == 1 and closed["runners_closed"] == 1
    assert closed["take_profit_contracts_closed"] == 0
    assert closed["order"]["quantity"] == "1" and closed["order_placed"] is False
    assert "cancel_take_profit" not in closed
    assert closed["cancel_stop"]["orderId"] == stop_id
    assert closed["cancel_stop"]["quantity"] == 2 and closed["cancel_stop"]["working_quantity"] == 1
    assert closed["selection"]["stop_reduced_to"] == 1
    assert "reduced the stop" in closed["note"] and "Resting stop is cancelled." in closed["note"]
    saved = saved_position(c)
    assert saved["status"] == "CLOSED" and saved["tp_status"] == "FILLED" and saved["tp_filled"] == 1
    assert saved["stop_status"] == "CANCELLED" and saved["qty_open"] == 0
    assert not ending(fake, "/order")


def test_stop_hit_cancels_the_take_profit_and_sells_nothing(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic(), MAX_QTY="2", MAX_DAILY_OPENS="1000", STOP_LOSS_PERCENT="30")
    opened = c.post("/webhook", json=underlying(qty=2, alert_id="open-stop"))
    assert opened.status_code == 200, opened.text
    tp_id = opened.json()["take_profit"]["orderId"]
    pos = saved_position(c)
    pos["stop_status"] = "FILLED"
    pos["stop_filled"] = 2
    flat = c.post("/webhook", json=flatten_alert("flat-stop"))
    assert flat.status_code == 200, flat.text
    body = flat.json()
    assert body["contracts_closed"] == 0 and body["runners_closed"] == 0 and body["order_placed"] is False
    assert "order" not in body and "cancel_stop" not in body
    assert body["cancel_take_profit"]["orderId"] == tp_id and body["cancel_take_profit"]["quantity"] == 1
    assert "Nothing remains open" in body["note"]
    saved = saved_position(c)
    assert saved["status"] == "CLOSED" and saved["stop_status"] == "FILLED" and saved["stop_filled"] == 2
    assert saved["tp_status"] == "CANCELLED" and saved["qty_open"] == 0
    assert ending(fake, "/quotes") == []
    assert len(ending(fake, "/preflight/single-leg")) == 3
    assert c.sb.exit_orders[("2026-10-19", "PREFLIGHT")] == 2
    assert not ending(fake, "/order")


def test_partial_take_profit_fill_sells_only_what_remains(tmp_path):
    c, fake = make(tmp_path, public=ChainPublic(quotes=CLOSE_QUOTE), MAX_QTY="4", MAX_DAILY_OPENS="1000",
                   MAX_ORDERS_PER_DAY="10")
    opened = c.post("/webhook", json=underlying(qty=4, alert_id="open-part"))
    assert opened.status_code == 200, opened.text
    pos = saved_position(c)
    pos["tp_status"] = "PARTIAL"
    pos["tp_filled"] = 1
    flat = c.post("/webhook", json=flatten_alert("flat-part"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["contracts_closed"] == 3 and closed["runners_closed"] == 2
    assert closed["take_profit_contracts_closed"] == 1
    assert closed["order"]["quantity"] == "3"
    assert closed["cancel_take_profit"]["quantity"] == 1 and closed["cancel_take_profit"]["reason"] == "partial"
    saved = saved_position(c)
    assert saved["status"] == "CLOSED" and saved["tp_status"] == "CANCELLED" and saved["tp_filled"] == 1
    assert not ending(fake, "/order")


def test_live_win_reconciles_the_take_profit_fill_and_places_only_the_sell(tmp_path):
    public = StatusPublic(quotes=CLOSE_QUOTE)
    c, fake = make(tmp_path, mode="LIVE", confirm="I_UNDERSTAND", public=public, MAX_QTY="2",
                   MAX_DAILY_OPENS="1000", STOP_LOSS_PERCENT="30")
    opened = c.post("/webhook", json=underlying(qty=2, alert_id="live-win"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["order_placed"] is True
    assert opened.json()["stop"]["attached"] is True and opened.json()["stop"]["stop_price"] == "1.02"
    assert len(ending(fake, "/order")) == 1
    assert len(ending(fake, "/preflight/single-leg")) == 3
    pos = saved_position(c, "LIVE")
    public.statuses[pos["tp_order_id"]] = {"status": "FILLED", "filledQuantity": "1"}
    public.statuses[pos["stop_order_id"]] = {"status": "NEW", "filledQuantity": "0"}
    flat = c.post("/webhook", json=flatten_alert("live-flat-win"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["order_placed"] is True and closed["contracts_closed"] == 1
    assert closed["runners_closed"] == 1 and closed["order"]["quantity"] == "1"
    assert "cancel_take_profit" not in closed
    assert closed["cancel_stop"]["working_quantity"] == 1 and closed["cancel_stop"]["quantity"] == 2
    places = ending(fake, "/order")
    assert len(places) == 2
    assert [json.loads(call.content)["orderType"] for call in places] == ["LIMIT", "LIMIT"]
    assert json.loads(places[1].content)["quantity"] == "1"
    looked_up = {call.url.path.rsplit("/", 1)[-1] for call in fake.calls
                 if call.method == "GET" and "/order/" in call.url.path}
    assert looked_up == {pos["tp_order_id"], pos["stop_order_id"]}
    saved = saved_position(c, "LIVE")
    assert saved["tp_status"] == "FILLED" and saved["tp_filled"] == 1 and saved["stop_status"] == "CANCELLED"
    assert c.sb.counters[("2026-10-19", "LIVE")][0] == 1
    assert c.sb.exit_orders[("2026-10-19", "LIVE")] == 3


def test_live_missing_exit_order_sells_the_stored_open_quantity(tmp_path):
    public = StatusPublic(quotes=CLOSE_QUOTE)
    c, fake = make(tmp_path, mode="LIVE", confirm="I_UNDERSTAND", public=public, MAX_QTY="2",
                   MAX_DAILY_OPENS="1000", STOP_LOSS_PERCENT="30")
    assert c.post("/webhook", json=underlying(qty=2, alert_id="live-miss")).status_code == 200
    flat = c.post("/webhook", json=flatten_alert("live-flat-miss"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["contracts_closed"] == 2 and closed["order"]["quantity"] == "2" and closed["order_placed"] is True
    assert closed["cancel_take_profit"]["quantity"] == 1 and closed["cancel_stop"]["quantity"] == 2
    assert len(ending(fake, "/order")) == 2
    assert len([call for call in fake.calls if call.method == "GET" and "/order/" in call.url.path]) == 2
    saved = saved_position(c, "LIVE")
    assert saved["status"] == "CLOSED" and saved["tp_filled"] == 0 and saved["tp_status"] == "CANCELLED"


def test_live_order_status_failure_leaves_the_position_open(tmp_path):
    public = StatusPublic(order_status=500)
    c, fake = make(tmp_path, mode="LIVE", confirm="I_UNDERSTAND", public=public, MAX_QTY="2",
                   MAX_DAILY_OPENS="1000", STOP_LOSS_PERCENT="30")
    assert c.post("/webhook", json=underlying(qty=2, alert_id="live-down")).status_code == 200
    flat = c.post("/webhook", json=flatten_alert("live-flat-down"))
    assert flat.status_code == 502, flat.text
    assert "order status failed" in flat.json()["error"]
    assert saved_position(c, "LIVE")["status"] == "OPEN"
    assert len(ending(fake, "/order")) == 1


def test_alpaca_symbol_tick_and_zero_dte_cutoff():
    assert relay.alpaca_option_symbol("SPY   261019C00579000") == CHOSEN_CALL
    today = date(2026, 10, 19)
    at_1530 = datetime(2026, 10, 19, 15, 30, tzinfo=ET)
    at_1529 = datetime(2026, 10, 19, 15, 29, tzinfo=ET)
    assert "15:30" in relay.paper_entry_cutoff("SPY", today, at_1530)
    assert "15:45" in relay.paper_entry_cutoff("QQQ", today, at_1530)
    assert relay.paper_entry_cutoff("SPY", today, at_1529) is None
    assert "15:15" in relay.paper_entry_cutoff("AAPL", today, datetime(2026, 10, 19, 15, 15, tzinfo=ET))
    assert relay.paper_entry_cutoff("AAPL", today, datetime(2026, 10, 19, 15, 14, tzinfo=ET)) is None
    assert relay.paper_entry_cutoff("SPY", date(2026, 10, 21), datetime(2026, 10, 19, 15, 40, tzinfo=ET)) is None
    assert relay.alpaca_order_state({"status": "filled", "filled_qty": "2"}, 2) == ("FILLED", 2)
    assert relay.alpaca_order_state({"status": "new", "filled_qty": "0"}, 2) == ("RESTING", 0)
    assert relay.alpaca_order_state({"status": "partially_filled", "filled_qty": "1"}, 2) == ("PARTIAL", 1)
    assert relay.alpaca_order_state({"status": "canceled", "filled_qty": "1"}, 2) == ("CANCELLED", 1)
    with pytest.raises(relay.Reject):
        relay.alpaca_order_state({"status": "weird", "filled_qty": "0"}, 1)


def _paper(tmp_path, alpaca=None, **over):
    broker = alpaca if alpaca is not None else FakeAlpaca()
    over.setdefault("MAX_QTY", "2")
    over.setdefault("MAX_DAILY_OPENS", "1000")
    over.setdefault("STOP_LOSS_PERCENT", "30")
    client, public = make(tmp_path, mode="PAPER", public=ChainPublic(quotes=CLOSE_QUOTE), alpaca=broker, **over)
    return client, public, broker


def test_paper_health_checks_the_account_without_balances(tmp_path):
    client, _public, _broker = _paper(tmp_path)
    body = client.get("/health").json()
    assert body["mode"] == "PAPER" and body["alpaca_configured"] is True
    assert body["alpaca"]["ok"] is True and body["alpaca"]["status"] == "ACTIVE"
    assert body["alpaca"]["equity"] == "100000"
    assert "cash" not in body["alpaca"] and "buying_power" not in body["alpaca"]
    assert "alpaca-secret" not in client.get("/health").text and "alpaca-key" not in client.get("/health").text


def test_paper_rejects_a_missing_contract_without_an_order(tmp_path):
    broker = FakeAlpaca(missing={CHOSEN_CALL})
    client, public, broker = _paper(tmp_path, broker)
    response = client.post("/webhook", json=underlying(qty=2, alert_id="missing"))
    assert response.status_code == 422, response.text
    assert CHOSEN_CALL in response.json()["error"]
    assert broker.posts() == []
    assert ("SPY", "FlowVWAP", "PAPER") not in client.sb.positions
    assert client.sb.counters.get(("2026-10-19", "PAPER"), [0])[0] == 0
    assert not ending(public, "/order") and not ending(public, "/preflight/single-leg")


def test_paper_rejects_new_zero_dte_entries_at_the_alpaca_cutoff(tmp_path):
    late = datetime(2026, 10, 19, 15, 30, tzinfo=ET)
    client, public, broker = _paper(tmp_path, clock=lambda: late)
    response = client.post("/webhook", json=underlying(qty=2, alert_id="late-spy"))
    assert response.status_code == 422, response.text
    assert "15:30" in response.json()["error"] and "15:45" in response.json()["error"]
    assert not ending(public, "/option-chain")
    assert broker.posts() == []

    earlier = datetime(2026, 10, 19, 15, 15, tzinfo=ET)
    aapl, aapl_public, aapl_broker = _paper(tmp_path / "aapl", clock=lambda: earlier)
    blocked = aapl.post("/webhook", json=underlying(symbol="AAPL", underlying_price="180", alert_id="late-aapl"))
    assert blocked.status_code == 422, blocked.text
    assert "15:15" in blocked.json()["error"]
    assert not ending(aapl_public, "/option-chain")
    assert aapl_broker.posts() == []


def test_paper_qty2_places_the_entry_take_profit_and_stop(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    client, public, broker = _paper(tmp_path)
    opened = client.post("/webhook", json=underlying(qty=2, alert_id="paper-open"))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["order_placed"] is True and body["broker"] == "alpaca" and body["mode"] == "PAPER"
    assert body["entry_status"] == "FILLED" and body["filled_qty"] == 2
    entry, take_profit, stop = broker.posts()
    assert entry["symbol"] == CHOSEN_CALL and " " not in entry["symbol"]
    assert entry["side"] == "buy" and entry["type"] == "limit" and entry["position_intent"] == "buy_to_open"
    assert entry["limit_price"] == "1.45" and entry["qty"] == "2" and entry["time_in_force"] == "day"
    assert take_profit["side"] == "sell" and take_profit["type"] == "limit"
    assert take_profit["position_intent"] == "sell_to_close" and take_profit["qty"] == "1"
    assert take_profit["limit_price"] == "1.74"
    assert stop["type"] == "stop_limit" and stop["side"] == "sell" and stop["qty"] == "2"
    assert stop["stop_price"] == "1.02" and stop["limit_price"] == "1.02"
    assert stop["position_intent"] == "sell_to_close"
    assert body["take_profit"]["orderId"] == "alp-2" and body["stop"]["orderId"] == "alp-3"
    assert not ending(public, "/order") and not ending(public, "/preflight/single-leg")
    pos = saved_position(client, "PAPER")
    assert pos["status"] == "OPEN" and pos["entry_status"] == "FILLED" and pos["exits_placed"] is True
    assert pos["tp_qty"] == 1 and pos["stop_qty"] == 2 and pos["stop_limit"] == "1.02"
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    assert client.sb.exit_orders[("2026-10-19", "PAPER")] == 2
    blob = opened.text + caplog.text
    assert "alpaca-secret" not in blob and "alpaca-key" not in blob and PASS not in opened.text


def test_paper_pending_fill_attaches_exits_on_keepalive(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    broker = FakeAlpaca(entry_status="new")
    client, _public, broker = _paper(tmp_path, broker)
    opened = client.post("/webhook", json=underlying(qty=2, alert_id="paper-wait"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["entry_status"] == "PENDING" and opened.json()["filled_qty"] == 0
    assert opened.json()["take_profit"]["attached"] is False
    assert len(broker.posts()) == 1
    pos = saved_position(client, "PAPER")
    assert pos["entry_status"] == "PENDING" and pos["exits_placed"] is False and pos["qty_open"] == 0
    order = broker.orders[opened.json()["alpaca_order_id"]]
    order["status"] = "filled"
    order["filled_qty"] = "2"
    order["filled_avg_price"] = "1.45"
    kept = client.get("/keepalive", headers={"Authorization": "Bearer cron-s"})
    assert kept.status_code == 200, kept.text
    assert kept.json()["reconciled"][0]["exits_placed"] is True
    assert len(broker.posts()) == 3
    assert saved_position(client, "PAPER")["exits_placed"] is True
    again = client.get("/keepalive", headers={"Authorization": "Bearer cron-s"})
    assert again.status_code == 200, again.text
    assert len(broker.posts()) == 3


def test_paper_flatten_cancels_working_exits_and_sells_the_remainder(tmp_path):
    client, public, broker = _paper(tmp_path)
    opened = client.post("/webhook", json=underlying(qty=2, alert_id="paper-loss"))
    assert opened.status_code == 200, opened.text
    take_profit = opened.json()["take_profit"]["orderId"]
    stop = opened.json()["stop"]["orderId"]
    flat = client.post("/webhook", json=flatten_alert("paper-flat-loss"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["order_placed"] is True and closed["contracts_closed"] == 2
    assert closed["runners_closed"] == 1 and closed["take_profit_contracts_closed"] == 1
    assert closed["order"]["qty"] == "2" and closed["order"]["limit_price"] == "1.20"
    assert closed["order"]["position_intent"] == "sell_to_close"
    assert closed["cancel_take_profit"]["orderId"] == take_profit
    assert closed["cancel_stop"]["orderId"] == stop
    assert set(broker.deletes) == {take_profit, stop}
    assert saved_position(client, "PAPER")["status"] == "CLOSED"
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    assert client.sb.exit_orders[("2026-10-19", "PAPER")] == 3
    assert not ending(public, "/order")


def test_paper_win_sells_only_the_runner(tmp_path):
    client, _public, broker = _paper(tmp_path)
    opened = client.post("/webhook", json=underlying(qty=2, alert_id="paper-win"))
    take_profit = opened.json()["take_profit"]["orderId"]
    stop = opened.json()["stop"]["orderId"]
    filled = broker.orders[take_profit]
    filled["status"] = "filled"
    filled["filled_qty"] = "1"
    filled["filled_avg_price"] = "1.74"
    flat = client.post("/webhook", json=flatten_alert("paper-flat-win"))
    assert flat.status_code == 200, flat.text
    closed = flat.json()
    assert closed["contracts_closed"] == 1 and closed["runners_closed"] == 1
    assert closed["order"]["qty"] == "1" and closed["order"]["limit_price"] == "1.20"
    assert take_profit not in broker.deletes and stop in broker.deletes
    saved = saved_position(client, "PAPER")
    assert saved["status"] == "CLOSED" and saved["tp_status"] == "FILLED" and saved["tp_filled"] == 1
    assert saved["stop_status"] == "CANCELLED"


def test_paper_stop_hit_sells_nothing(tmp_path):
    client, public, broker = _paper(tmp_path)
    opened = client.post("/webhook", json=underlying(qty=2, alert_id="paper-stopped"))
    take_profit = opened.json()["take_profit"]["orderId"]
    stop = opened.json()["stop"]["orderId"]
    broker.orders[stop]["status"] = "filled"
    broker.orders[stop]["filled_qty"] = "2"
    flat = client.post("/webhook", json=flatten_alert("paper-flat-stop"))
    assert flat.status_code == 200, flat.text
    body = flat.json()
    assert body["order_placed"] is False and body["contracts_closed"] == 0
    assert "order" not in body
    assert body["cancel_take_profit"]["orderId"] == take_profit
    assert stop not in broker.deletes and take_profit in broker.deletes
    assert len(broker.posts()) == 3
    saved = saved_position(client, "PAPER")
    assert saved["status"] == "CLOSED" and saved["stop_status"] == "FILLED" and saved["stop_filled"] == 2
    assert saved["tp_status"] == "CANCELLED"
    assert client.sb.exit_orders[("2026-10-19", "PAPER")] == 2
    assert not ending(public, "/quotes")


def test_paper_pending_flatten_cancels_the_entry_and_does_not_sell(tmp_path):
    broker = FakeAlpaca(entry_status="new")
    client, _public, broker = _paper(tmp_path, broker)
    opened = client.post("/webhook", json=underlying(qty=2, alert_id="paper-unfilled"))
    entry_id = opened.json()["alpaca_order_id"]
    flat = client.post("/webhook", json=flatten_alert("paper-cancel-entry"))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order_placed"] is False and flat.json()["contracts_closed"] == 0
    assert broker.posts() == [broker.posts()[0]]
    assert entry_id in broker.deletes
    assert saved_position(client, "PAPER")["status"] == "CLOSED"


def test_paper_exits_ignore_the_entry_cap(tmp_path):
    client, _public, broker = _paper(tmp_path, MAX_ORDERS_PER_DAY="1", EXITS_COUNT_TOWARD_ORDER_CAP="true")
    assert client.post("/webhook", json=underlying(qty=2, alert_id="cap-open")).status_code == 200
    blocked = client.post("/webhook", json=underlying(qty=2, alert_id="cap-again", strategy="Other"))
    assert blocked.status_code == 422, blocked.text
    assert "MAX_ORDERS_PER_DAY" in blocked.json()["error"]
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    assert client.sb.exit_orders[("2026-10-19", "PAPER")] == 2
    flat = client.post("/webhook", json=flatten_alert("cap-flat"))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order_placed"] is True and flat.json()["contracts_closed"] == 2
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    assert client.sb.exit_orders[("2026-10-19", "PAPER")] == 3
    assert len(broker.posts()) == 4


def test_paper_penny_program_keeps_the_cent_above_three_dollars(tmp_path):
    calls = [("579", "0.55", "4.00", "4.20")]
    chain = spy_chain(calls=calls, puts=[])
    greeks = greek_map(calls, "C")

    def run(ppind):
        broker = FakeAlpaca(ppind=ppind)
        client, _public = make(
            tmp_path / str(ppind), mode="PAPER", public=ChainPublic(chain=chain, greeks=greeks), alpaca=broker,
            MAX_QTY="2", MAX_DAILY_OPENS="5000", MAX_PREMIUM="5", TP_PERCENT="10")
        opened = client.post("/webhook", json=underlying(qty=2, alert_id=f"penny-{ppind}"))
        assert opened.status_code == 200, opened.text
        return broker.posts()[1]["limit_price"]

    assert run(False) == "4.50"
    assert run(True) == "4.51"


def test_paper_occ_open_is_placed_and_not_stored(tmp_path):
    client, public, broker = _paper(tmp_path, STOP_LOSS_PERCENT="")
    response = client.post("/webhook", json=alert(alert_id="occ-paper"))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["order_placed"] is True and body["broker"] == "alpaca" and body["selection"] is None
    assert broker.posts()[0]["symbol"] == "AAPL261120C00250000"
    assert broker.posts()[0]["position_intent"] == "buy_to_open"
    assert client.sb.positions == {}
    assert not ending(public, "/order")
