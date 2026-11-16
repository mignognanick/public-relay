"""PremarketES live orders on Public.com. Every other strategy stays on Alpaca paper.

MODE=LIVE is not enough. LIVE_CONFIRM=I_UNDERSTAND is also required. The live set is a
built-in ceiling of PremarketES; LIVE_STRATEGIES can only remove names from that set.
"""
import json
import os
import uuid
from decimal import Decimal

os.environ["RELAY_NO_AUTOAPP"] = "1"

import httpx
import pytest

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
import index as relay  # noqa: E402
from test_index import (  # noqa: E402
    ET, PASS, FakeAlpaca, MultiChainPublic, chain_for, greeks_for, make, option_quote, qqq_alert,
)
from test_premarketes import (  # noqa: E402
    ES_CALL, ES_CALLS, ES_PUTS, es_alert,
)
from test_premarketes_mu import mu_alert, mu_public  # noqa: E402

CASH_ACCOUNT = "CASH1"


def portfolio_body(cash="500.00", options=None):
    buying = {"cashOnlyBuyingPower": cash, "buyingPower": cash}
    if options is not None:
        buying["optionsBuyingPower"] = options
    return {
        "accountId": CASH_ACCOUNT,
        "accountType": "BROKERAGE",
        "buyingPower": buying,
        "cash": cash,
    }


class LivePublic(MultiChainPublic):
    """Chain stand-in plus the portfolio snapshot live entries read. No real Public calls."""

    def __init__(self, chains, greeks=None, quotes=None, portfolio=None, preflight_status=200,
                 preflight_body=None, portfolio_status=200):
        super().__init__(chains, greeks, quotes)
        self.portfolio = portfolio_body() if portfolio is None else portfolio
        self.preflight_status = preflight_status
        self.preflight_body = preflight_body
        self.portfolio_status = portfolio_status

    def __call__(self, req: httpx.Request):
        if req.url.path.endswith("/portfolio/v2"):
            self.calls.append(req)
            assert req.headers["Authorization"] == "Bearer tok-1"
            if self.portfolio_status >= 400:
                return httpx.Response(self.portfolio_status, json={"error": "portfolio down"})
            return httpx.Response(200, json=self.portfolio)
        if req.url.path.endswith("/preflight/single-leg") and (
            self.preflight_status >= 400 or self.preflight_body is not None
        ):
            self.calls.append(req)
            assert req.headers["Authorization"] == "Bearer tok-1"
            body = self.preflight_body if self.preflight_body is not None else {"error": "nope"}
            return httpx.Response(self.preflight_status, json=body)
        return super().__call__(req)


def live_public(calls=None, quotes=None, **kw):
    call_rows = ES_CALLS if calls is None else calls
    chains = {"SPY": chain_for("SPY", calls=call_rows, puts=ES_PUTS),
              "QQQ": chain_for("QQQ")}
    greeks = greeks_for("SPY", calls=call_rows, puts=ES_PUTS) | greeks_for("QQQ")
    return LivePublic(chains, greeks, quotes, **kw)


def live_make(tmp_path, public=None, alpaca=None, **over):
    over.setdefault("PUBLIC_ACCOUNT_ID", CASH_ACCOUNT)
    over.setdefault("MAX_DAILY_OPENS", "5000")
    over.setdefault("ALLOWLIST", "AAPL,SPY,QQQ")
    if alpaca is not None:
        over.setdefault("ALPACA_KEY_ID", "alpaca-key")
        over.setdefault("ALPACA_SECRET_KEY", "alpaca-secret")
    return make(
        tmp_path, mode="LIVE", confirm="I_UNDERSTAND",
        public=public or live_public(), alpaca=alpaca, **over)


def orders(fake):
    return [call for call in fake.calls if call.url.path.endswith("/order") and call.method == "POST"]


def order_body(call):
    return json.loads(call.content)


def test_live_switches_and_the_allowlist_cannot_widen():
    assert relay.LIVE_STRATEGY_CEILING == frozenset({"PremarketES"})
    both = relay.Settings.from_env({"MODE": "LIVE", "LIVE_CONFIRM": "I_UNDERSTAND"})
    assert both.mode == "LIVE"
    assert both.live_strategies == frozenset({"PremarketES"})
    assert both.live_max_orders_per_day == 1
    assert both.live_max_premium == Decimal("2.50")
    assert both.live_max_daily_opens == Decimal("250.00")

    assert relay.Settings.from_env({"MODE": "LIVE"}).mode == "PREFLIGHT"
    assert relay.Settings.from_env({"MODE": "LIVE", "LIVE_CONFIRM": "yes"}).mode == "PREFLIGHT"
    paper = relay.Settings.from_env({
        "MODE": "PAPER", "LIVE_CONFIRM": "I_UNDERSTAND",
        "ALPACA_KEY_ID": "alpaca-key", "ALPACA_SECRET_KEY": "alpaca-secret",
    })
    assert paper.mode == "PAPER"
    assert paper.for_strategy("PremarketES").order_qty == 5
    forced = relay.Settings.from_env({
        "STRATEGY_OVERRIDES": '{"PremarketES": {"order_qty": 9, "max_qty": 9, "max_premium": "9.00"}}',
    })
    live = relay.live_execution_profile(forced.for_strategy("PremarketES"), forced)
    assert live.order_qty == 1 and live.max_qty == 1 and live.max_premium == Decimal("2.50")
    assert forced.for_strategy("PremarketES").order_qty == 9

    widened = relay.Settings.from_env({"LIVE_STRATEGIES": "PremarketES, FlowVWAP, QQQ_EMA_VWAP"})
    assert widened.live_strategies == frozenset({"PremarketES"})
    assert "PremarketES_MU" not in widened.live_strategies
    assert relay.Settings.from_env({"LIVE_STRATEGIES": "FlowVWAP"}).live_strategies == frozenset()
    assert relay.Settings.from_env({"LIVE_STRATEGIES": "PremarketES"}).live_strategies == frozenset({"PremarketES"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({"LIVE_MAX_PREMIUM": "2.51"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({"LIVE_MAX_ORDERS_PER_DAY": "2"})
    with pytest.raises(ValueError):
        relay.Settings.from_env({"LIVE_MAX_DAILY_OPENS": "251"})
    tighter = relay.Settings.from_env({"LIVE_MAX_PREMIUM": "1.25", "LIVE_MAX_DAILY_OPENS": "100"})
    assert tighter.live_max_premium == Decimal("1.25")
    assert tighter.live_max_daily_opens == Decimal("100")


def test_health_lists_live_eligibility_without_secrets(tmp_path):
    client, _fake = live_make(tmp_path)
    body = client.get("/health").json()
    settings = body["settings"]
    assert body["mode"] == "LIVE"
    assert settings["live_strategies"] == ["PremarketES"]
    assert settings["live_qty"] == 1
    assert settings["live_max_orders_per_day"] == 1
    assert settings["live_max_premium"] == "2.50"
    assert settings["live_max_daily_opens"] == "250.00"
    assert settings["live_account_configured"] is True
    dumped = json.dumps(body)
    assert CASH_ACCOUNT not in dumped
    assert "secret" not in dumped and PASS not in dumped
    assert settings["strategies"]["PremarketES"]["order_qty"] == 5


def test_one_switch_does_not_send_premarketes_live(tmp_path):
    public = live_public()
    client, fake = make(
        tmp_path, mode="LIVE", confirm="yes", public=public,
        PUBLIC_ACCOUNT_ID=CASH_ACCOUNT, MAX_DAILY_OPENS="5000")
    opened = client.post("/webhook", json=es_alert())
    assert opened.status_code == 200, opened.text
    assert opened.json()["mode"] == "PREFLIGHT"
    assert opened.json()["order_placed"] is False
    assert opened.json()["order"]["quantity"] == "5"
    assert orders(fake) == []


def test_live_entry_is_one_contract_limit_after_preflight_and_cash_check(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    public = live_public(quotes=quotes)
    client, fake = live_make(tmp_path, public=public)
    opened = client.post("/webhook", json=es_alert())
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["mode"] == "LIVE" and body["order_placed"] is True and body["broker"] == "public"
    assert body["selection"]["chosen_symbol"] == ES_CALL
    assert body["selection"]["qty"] == 1
    assert body["order"]["orderSide"] == "BUY"
    assert body["order"]["orderType"] == "LIMIT"
    assert body["order"]["expiration"]["timeInForce"] == "DAY"
    assert body["order"]["openCloseIndicator"] == "OPEN"
    assert body["order"]["quantity"] == "1"
    assert body["order"]["limitPrice"] == "0.90"
    sent = orders(fake)
    assert len(sent) == 1
    assert sent[0].url.path == f"/userapigateway/trading/{CASH_ACCOUNT}/order"
    placed = order_body(sent[0])
    assert placed["orderType"] == "LIMIT" and placed["quantity"] == "1"
    assert placed["limitPrice"] == "0.90" and placed["openCloseIndicator"] == "OPEN"
    assert placed["orderId"] == str(uuid.uuid5(uuid.NAMESPACE_URL, "public-relay:OPEN:es-1"))
    assert body["orderId"] == placed["orderId"]
    preflights = [call for call in fake.calls if call.url.path.endswith("/preflight/single-leg")]
    assert len(preflights) == 1
    assert order_body(preflights[0])["openCloseIndicator"] == "OPEN"
    assert any(call.url.path.endswith("/portfolio/v2") for call in fake.calls)
    saved = client.sb.positions[("SPY", "PremarketES", "LIVE")]
    assert saved["qty"] == 1 and saved["symbol"] == ES_CALL and saved["status"] == "OPEN"
    assert ("SPY", "PremarketES", "PAPER") not in client.sb.positions
    assert client.sb.counters[("2026-10-19", "LIVE")] == [1, Decimal("90.00")]
    assert body["take_profit"]["attached"] is False
    assert not any(order_body(call).get("openCloseIndicator") == "CLOSE" for call in sent)


def test_live_premium_cap_rejects_above_two_fifty_and_uses_mid_or_ask(tmp_path):
    rich = [("581", "0.36", "2.80", "3.20")] + [row for row in ES_CALLS if row[0] != "581"]
    client, fake = live_make(tmp_path, public=live_public(calls=rich))
    rejected = client.post("/webhook", json=es_alert(alert_id="rich"))
    assert rejected.status_code == 422, rejected.text
    assert "2.50" in rejected.json()["error"]
    assert orders(fake) == []
    assert client.sb.counters == {}

    at_cap = [("581", "0.36", "2.20", "2.60")] + [row for row in ES_CALLS if row[0] != "581"]
    capped, capped_fake = live_make(tmp_path / "cap", public=live_public(calls=at_cap))
    opened = capped.post("/webhook", json=es_alert(alert_id="at-cap"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["order"]["limitPrice"] == "2.40"
    assert opened.json()["order"]["quantity"] == "1"

    ask_only = [("581", "0.36", None, "2.40")] + [row for row in ES_CALLS if row[0] != "581"]
    asked, _asked_fake = live_make(tmp_path / "ask", public=live_public(calls=ask_only))
    ask_open = asked.post("/webhook", json=es_alert(alert_id="ask-only"))
    assert ask_open.status_code == 200, ask_open.text
    assert ask_open.json()["order"]["limitPrice"] == "2.40"
    assert ask_open.json()["selection"]["limit_source"] == "ask"

    ask_over = [("581", "0.36", None, "2.80")] + [row for row in ES_CALLS if row[0] != "581"]
    over, over_fake = live_make(tmp_path / "ask-over", public=live_public(calls=ask_over))
    blocked = over.post("/webhook", json=es_alert(alert_id="ask-over"))
    assert blocked.status_code == 422 and "2.50" in blocked.json()["error"]
    assert orders(over_fake) == []
    assert orders(capped_fake)  # the in-cap entry was the only live order in that app


def test_insufficient_settled_cash_rejects_before_the_order(tmp_path):
    poor = live_public(portfolio=portfolio_body(cash="89.99"))
    client, fake = live_make(tmp_path, public=poor)
    rejected = client.post("/webhook", json=es_alert(alert_id="poor"))
    assert rejected.status_code == 422, rejected.text
    assert "settled cash" in rejected.json()["error"]
    assert orders(fake) == []
    assert client.sb.positions == {}
    assert client.sb.counters == {}

    options_short = live_public(portfolio=portfolio_body(cash="500.00", options="50.00"))
    short, short_fake = live_make(tmp_path / "options", public=options_short)
    blocked = short.post("/webhook", json=es_alert(alert_id="options-short"))
    assert blocked.status_code == 422 and "settled cash" in blocked.json()["error"]
    assert orders(short_fake) == []

    missing, missing_fake = live_make(tmp_path / "blank", PUBLIC_ACCOUNT_ID="")
    no_account = missing.post("/webhook", json=es_alert(alert_id="no-account"))
    assert no_account.status_code == 422 and "PUBLIC_ACCOUNT_ID" in no_account.json()["error"]
    assert orders(missing_fake) == []
    assert not any(call.url.path.endswith("/portfolio/v2") for call in missing_fake.calls)


def test_preflight_failure_and_buying_power_requirement_reject(tmp_path):
    failed = live_public(preflight_status=400)
    client, fake = live_make(tmp_path, public=failed)
    rejected = client.post("/webhook", json=es_alert(alert_id="pf"))
    assert rejected.status_code == 502, rejected.text
    assert rejected.json()["stage"] == "preflight"
    assert orders(fake) == []
    assert list(client.sb.counters.values()) == [[0, Decimal(0)]]
    assert client.sb.positions == {}

    costly = live_public(
        portfolio=portfolio_body(cash="400.00"),
        preflight_body={"buyingPowerRequirement": "500.00"})
    cost_client, cost_fake = live_make(tmp_path / "cost", public=costly)
    blocked = cost_client.post("/webhook", json=es_alert(alert_id="bp"))
    assert blocked.status_code == 422, blocked.text
    assert "buying power" in blocked.json()["error"]
    assert orders(cost_fake) == []
    assert list(cost_client.sb.counters.values()) == [[0, Decimal(0)]]


def test_duplicate_alert_id_does_not_buy_twice(tmp_path):
    client, fake = live_make(tmp_path)
    first = client.post("/webhook", json=es_alert())
    assert first.status_code == 200 and first.json()["order_placed"] is True
    before = len(orders(fake))
    second = client.post("/webhook", json=es_alert())
    assert second.status_code == 200 and second.json()["duplicate"] is True
    assert len(orders(fake)) == before == 1
    assert client.sb.counters[("2026-10-19", "LIVE")][0] == 1


def test_second_live_entry_hits_the_one_a_day_cap(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    client, fake = live_make(tmp_path, public=live_public(quotes=quotes))
    assert client.post("/webhook", json=es_alert()).status_code == 200
    flat = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="es-flat"))
    assert flat.status_code == 200, flat.text
    again = client.post("/webhook", json=es_alert(
        side="buy", market_position="long", prev_market_position="flat", alert_id="es-2"))
    assert again.status_code == 422 and "MAX_ORDERS_PER_DAY" in again.json()["error"]
    assert len(orders(fake)) == 2  # the entry and the exit, not a second entry
    assert client.sb.counters[("2026-10-19", "LIVE")][0] == 1


def test_live_exit_sells_the_held_contract_at_the_bid_and_leaves_paper_alone(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    client, fake = live_make(tmp_path, public=live_public(quotes=quotes))
    assert client.post("/webhook", json=es_alert()).status_code == 200
    client.sb.positions[("SPY", "PremarketES", "PAPER")] = {
        "underlying": "SPY", "strategy": "PremarketES", "mode": "PAPER", "symbol": ES_CALL,
        "entry_side": "BUY", "right_side": "C", "qty": 5, "qty_open": 5, "strike": "581",
        "opened_alert_id": "paper-row", "status": "OPEN", "tp_status": "NONE", "stop_status": "NONE",
    }
    flat = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", qty=99, alert_id="es-flat"))
    assert flat.status_code == 200, flat.text
    body = flat.json()
    assert body["order_placed"] is True and body["contracts_closed"] == 1
    assert body["order"]["orderSide"] == "SELL"
    assert body["order"]["openCloseIndicator"] == "CLOSE"
    assert body["order"]["orderType"] == "LIMIT"
    assert body["order"]["quantity"] == "1"
    assert body["order"]["limitPrice"] == "0.70"
    assert body["order"]["instrument"]["symbol"] == ES_CALL
    sells = [order_body(call) for call in orders(fake) if order_body(call)["orderSide"] == "SELL"]
    assert len(sells) == 1
    assert sells[0]["quantity"] == "1" and sells[0]["openCloseIndicator"] == "CLOSE"
    assert sells[0]["orderType"] == "LIMIT" and sells[0]["limitPrice"] == "0.70"
    assert client.sb.positions[("SPY", "PremarketES", "LIVE")]["status"] == "CLOSED"
    assert client.sb.positions[("SPY", "PremarketES", "PAPER")]["status"] == "OPEN"
    assert client.sb.counters[("2026-10-19", "LIVE")][0] == 1

    missing = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="es-again"))
    assert missing.status_code == 422, missing.text
    assert "no open position" in missing.json()["error"]
    assert len([call for call in orders(fake) if order_body(call)["orderSide"] == "SELL"]) == 1


def test_live_exit_preflight_failure_leaves_the_position_open(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    public = live_public(quotes=quotes)
    client, fake = live_make(tmp_path, public=public)
    assert client.post("/webhook", json=es_alert()).status_code == 200
    public.preflight_status = 400
    flat = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="es-flat"))
    assert flat.status_code == 502 and flat.json()["stage"] == "preflight"
    assert client.sb.positions[("SPY", "PremarketES", "LIVE")]["status"] == "OPEN"
    assert len(orders(fake)) == 1


def test_raw_occ_cannot_open_or_sell_a_live_premarketes_position(tmp_path):
    client, fake = live_make(tmp_path)
    raw = {
        "passphrase": PASS, "symbol": ES_CALL, "side": "BUY", "open_close": "OPEN",
        "limit_price": "0.90", "qty": 1, "alert_id": "raw-open", "strategy": "PremarketES",
    }
    opened = client.post("/webhook", json=raw)
    assert opened.status_code == 422, opened.text
    assert orders(fake) == []
    short = {
        "passphrase": PASS, "symbol": ES_CALL, "side": "SELL", "open_close": "CLOSE",
        "limit_price": "0.10", "qty": 5, "alert_id": "raw-close", "strategy": "PremarketES",
    }
    closed = client.post("/webhook", json=short)
    assert closed.status_code == 422, closed.text
    assert orders(fake) == []
    assert client.sb.positions == {}


def test_other_strategies_stay_on_paper_while_premarketes_is_live(tmp_path):
    broker = FakeAlpaca()
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    client, fake = live_make(tmp_path, public=live_public(quotes=quotes), alpaca=broker)
    qqq = client.post("/webhook", json=qqq_alert(alert_id="qqq-live-process"))
    assert qqq.status_code == 200, qqq.text
    assert qqq.json()["broker"] == "alpaca" and qqq.json()["mode"] == "PAPER"
    assert qqq.json()["order"]["qty"] == "1"
    assert orders(fake) == []
    assert client.sb.positions[("QQQ", "QQQ_EMA_VWAP", "PAPER")]["status"] == "OPEN"
    assert ("QQQ", "QQQ_EMA_VWAP", "LIVE") not in client.sb.positions

    live = client.post("/webhook", json=es_alert(alert_id="es-live"))
    assert live.status_code == 200, live.text
    assert live.json()["broker"] == "public" and live.json()["order"]["quantity"] == "1"
    assert len(orders(fake)) == 1
    assert client.sb.positions[("SPY", "PremarketES", "LIVE")]["qty"] == 1
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    assert client.sb.counters[("2026-10-19", "LIVE")][0] == 1

    broker_mu = FakeAlpaca()
    mu_client, mu_fake = live_make(tmp_path / "mu", public=mu_public(), alpaca=broker_mu)
    mu = mu_client.post("/webhook", json=mu_alert(alert_id="mu-paper"))
    assert mu.status_code == 200, mu.text
    assert mu.json()["broker"] == "alpaca" and mu.json()["mode"] == "PAPER"
    assert mu.json()["order"]["qty"] == "5"
    assert orders(mu_fake) == []
    assert mu_client.sb.positions[("MU", "PremarketES_MU", "PAPER")]["qty"] == 5
    assert ("MU", "PremarketES_MU", "LIVE") not in mu_client.sb.positions

    removed, removed_fake = live_make(
        tmp_path / "removed", public=live_public(), alpaca=FakeAlpaca(),
        LIVE_STRATEGIES="FlowVWAP")
    paper_es = removed.post("/webhook", json=es_alert(alert_id="es-paper"))
    assert paper_es.status_code == 200, paper_es.text
    assert paper_es.json()["broker"] == "alpaca" and paper_es.json()["order"]["qty"] == "5"
    assert orders(removed_fake) == []
    assert removed.sb.positions[("SPY", "PremarketES", "PAPER")]["qty"] == 5


def test_flowvwap_monitor_stays_on_paper_while_premarketes_is_live(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    flow_call = "SPY261019C00580000"
    quotes = {
        "SPY": {"instrument": {"symbol": "SPY", "type": "EQUITY"}, "outcome": "SUCCESS",
                "last": "579.40", "bid": "579.30", "ask": "579.50"},
        flow_call: option_quote(flow_call, "0.90", "1.10"),
    }
    broker = FakeAlpaca()
    client, fake = live_make(tmp_path, public=live_public(quotes=quotes), alpaca=broker)
    client.sb.positions[("SPY", "FlowVWAP", "PAPER")] = {
        "underlying": "SPY", "strategy": "FlowVWAP", "mode": "PAPER", "symbol": flow_call,
        "entry_side": "BUY", "status": "OPEN", "qty": 5, "qty_open": 5, "strike": "580",
        "right_side": "C", "entry_underlying": "580", "stop_underlying": "579.50",
        "target_underlying": "581", "opened_alert_id": "flow-seed",
        "tp_status": "NONE", "stop_status": "NONE",
    }
    report = client.post("/flowvwap-monitor", headers={"Authorization": "Bearer cron-s"})
    assert report.status_code == 200, report.text
    action = report.json()["actions"][0]
    assert action["action"] == "stop" and action["order_placed"] is True and action["quantity"] == 5
    assert orders(fake) == []
    sells = [body for body in broker.posts() if body.get("position_intent") == "sell_to_close"]
    assert len(sells) == 1 and sells[0]["qty"] == "5" and sells[0]["type"] == "limit"
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"


def test_live_decisions_are_logged_without_secrets(tmp_path, caplog):
    caplog.set_level("INFO", logger="relay")
    client, _fake = live_make(tmp_path, public=live_public(portfolio=portfolio_body(cash="10.00")))
    rejected = client.post("/webhook", json=es_alert(alert_id="logged"))
    assert rejected.status_code == 422, rejected.text
    lines = []
    for record in caplog.records:
        if record.name != "relay":
            continue
        lines.append(json.loads(record.message))
    events = {line["event"] for line in lines}
    assert "live_cash" in events and "response" in events
    blob = json.dumps(lines)
    assert "test-secret" not in blob and PASS not in blob
    assert "tok-1" not in blob and "svc-key" not in blob and CASH_ACCOUNT not in blob
    cash = next(line for line in lines if line["event"] == "live_cash")
    assert cash["ok"] is False and cash["settled"] == "10.00" and cash["required"] == "90.00"
