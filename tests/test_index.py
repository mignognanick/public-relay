import json
import os
from datetime import datetime
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

    def __init__(self, fail=False):
        self.alerts, self.counters, self.fail, self.calls = {}, {}, fail, []

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
