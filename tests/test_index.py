import json
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
        self.positions, self.picks = {}, []
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
            c = self.counters.setdefault((b["p_day"], b["p_mode"]), [0, Decimal(0)])
            n = Decimal(b["p_notional"])
            if c[0] >= b["p_max_orders"]:
                return httpx.Response(200, json=f"MAX_ORDERS_PER_DAY ({b['p_max_orders']}) reached")
            if b["p_is_open"] and c[1] + n > Decimal(b["p_max_opens"]):
                return httpx.Response(200, json=f"daily OPEN spend cap: spent ${c[1]} + ${n} > ${b['p_max_opens']}")
            c[0] += 1
            c[1] += n if b["p_is_open"] else 0
            return httpx.Response(200, json=None)
        if p == "/rest/v1/rpc/relay_release":
            c = self.counters[(b["p_day"], b["p_mode"])]
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
                  "/rest/v1/rpc/relay_record_close", "/rest/v1/contract_picks")
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
                "strike": b["p_strike"], "opened_alert_id": b["p_alert_id"], "status": "OPEN",
            }
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        if p == "/rest/v1/rpc/relay_record_close":
            key = (b["p_underlying"], b["p_strategy"], b["p_mode"])
            row = self.positions.get(key)
            if not row or row["status"] != "OPEN" or row["symbol"] != b["p_symbol"]:
                return httpx.Response(200, json=f"no open position for {key[0]}/{key[1]}")
            row["status"] = "CLOSED"
            row["closed_alert_id"] = b["p_alert_id"]
            return httpx.Response(200, content=b"null", headers={"content-type": "application/json"})
        if p == "/rest/v1/contract_picks" and req.method == "POST":
            self.picks.append(b)
            return httpx.Response(201, content=b"", headers={"content-type": "application/json"})
        return httpx.Response(404)


def make(tmp_path, mode="PREFLIGHT", confirm="", clock=lambda: OPEN_TIME, sb=None, public=None, **over):
    env = {"PUBLIC_SECRET": SECRET, "WEBHOOK_PASSPHRASE": PASS, "MODE": mode, "LIVE_CONFIRM": confirm,
           "ALLOWLIST": "AAPL,SPY", "SUPABASE_URL": "https://x.supabase.co", "SUPABASE_SERVICE_KEY": "svc-key", **over}
    fake = public or FakePublic()
    sb = sb or FakeSupabase()
    a = relay.create_app(relay.Settings.from_env(env), transport=httpx.MockTransport(fake),
                         store_transport=httpx.MockTransport(sb), clock=clock)
    c = TestClient(a)
    c.sb = sb
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
    r = c.post("/webhook", json=alert(limit_price="2.01"))
    assert r.status_code == 422 and "MAX_PREMIUM" in r.json()["error"]
    assert fake.calls == []


def test_preflight_mode_never_calls_order(tmp_path):
    c, fake = make(tmp_path)
    r = c.post("/webhook", json=alert())
    assert r.status_code == 200, r.text
    assert r.json()["order_placed"] is False
    assert "selection" not in r.json()
    assert any(p.endswith("/preflight/single-leg") for p in fake.paths())
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
    # CLOSE still allowed by spend cap; then order-count cap (3) hits
    assert c.post("/webhook", json=alert(alert_id="4", side="SELL", open_close="CLOSE")).status_code == 200
    r = c.post("/webhook", json=alert(alert_id="5", side="SELL", open_close="CLOSE"))
    assert r.status_code == 422 and "MAX_ORDERS_PER_DAY" in r.json()["error"]


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
    assert any(x.url.path == "/rest/v1/daily_counters" for x in c.sb.calls)
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


def occ(strike, right, underlying="SPY"):
    millis = int(Decimal(strike) * 1000)
    return f"{underlying}261019{right}{millis:08d}"


def chain_row(symbol, strike, delta, bid, ask):
    return {
        "instrument": {"symbol": symbol, "type": "OPTION"},
        "outcome": "SUCCESS",
        "bid": bid,
        "ask": ask,
        "optionDetails": {"strikePrice": strike, "greeks": {"delta": delta} if delta is not None else {}},
    }


def spy_chain(call_delta=None, put_delta=None, calls=CALLS, puts=PUTS):
    return {
        "baseSymbol": "SPY",
        "calls": [chain_row(occ(s, "C"), s, call_delta if call_delta is not None else d, b, a) for s, d, b, a in calls],
        "puts": [chain_row(occ(s, "P"), s, put_delta if put_delta is not None else d, b, a) for s, d, b, a in puts],
    }


def greek_map(rows, right):
    return {occ(strike, right): delta for strike, delta, _bid, _ask in rows}


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


def underlying(**kw):
    base = {
        "passphrase": PASS, "symbol": "SPY", "side": "buy", "qty": 1, "underlying_price": "580",
        "strategy": "FlowVWAP", "alert_id": "spy-1", "market_position": "long", "prev_market_position": "flat",
    }
    base.update(kw)
    return base


def ending(fake, suffix):
    return [call for call in fake.calls if call.url.path.endswith(suffix)]


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

    rich = [("579", "0.55", "2.00", "2.20")]
    c_px, fake_px = make(tmp_path / "px", public=ChainPublic(chain=spy_chain(calls=rich, puts=[]),
                                                             greeks=greek_map(rich, "C")))
    priced = c_px.post("/webhook", json=underlying(alert_id="rich"))
    assert priced.status_code == 422 and "MAX_PREMIUM" in priced.json()["error"]
    assert priced.json()["selection"]["chosen_symbol"] == CHOSEN_CALL
    assert c_px.sb.picks and not c_px.sb.positions
    assert not ending(fake_px, "/preflight/single-leg") and not ending(fake_px, "/order")

    c_cap, _ = make(tmp_path / "cap", public=ChainPublic())
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


def test_underlying_pick_does_not_log_passphrase(tmp_path, capsys):
    c, fake = make(tmp_path, public=ChainPublic())
    r = c.post("/webhook", json=underlying())
    assert r.status_code == 200
    blob = r.text + capsys.readouterr().out + json.dumps(c.sb.picks) + json.dumps(c.sb.alerts)
    assert PASS not in blob and SECRET not in blob
    assert not ending(fake, "/order")
