"""PremarketES: SPY 0DTE, slightly OTM delta 0.35-0.40, qty 5, flatten on the close alert.

The profile shares MAX_DAILY_OPENS and MAX_ORDERS_PER_DAY with FlowVWAP and QQQ_EMA_VWAP.
It does not use FlowVWAP's regime, flip, King room, or 09:30-11:30 window.
"""
import os
from datetime import datetime
from decimal import Decimal

os.environ["RELAY_NO_AUTOAPP"] = "1"

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
import flowvwap  # noqa: E402
import index as relay  # noqa: E402
from test_index import (  # noqa: E402
    ET, OPEN_TIME, PASS, CALLS, PUTS, FakeAlpaca, MultiChainPublic,
    chain_for, greeks_for, make, occ, option_quote, qqq_alert, underlying,
)

SPOT = Decimal("580")
# ITM strikes sit in the 0.35-0.40 band so the old ATM-through-ITM window would pick them.
# The 6-point OTM strikes are a better band match, so a window wider than 5 points would pick them.
ES_CALLS = [
    ("579", "0.375", "1.40", "1.60"),  # 1.00 ITM
    ("580", "0.55", "1.20", "1.40"),    # ATM, FlowVWAP's 0.50-0.60 pick
    ("581", "0.36", "0.80", "1.00"),    # 1.00 OTM, PremarketES call
    ("586", "0.375", "0.20", "0.30"),   # 6.00 OTM, outside the 5-point window
]
ES_PUTS = [
    ("581", "-0.375", "1.20", "1.40"),  # 1.00 ITM
    ("580", "-0.55", "1.10", "1.30"),   # ATM
    ("579", "-0.36", "0.70", "0.90"),   # 1.00 OTM, PremarketES put
    ("574", "-0.375", "0.15", "0.25"),  # 6.00 OTM, outside the window
]
ES_CALL = occ("581", "C")
ES_PUT = occ("579", "P")
CALL_LIMIT = Decimal("0.90")  # mid of 0.80 / 1.00
CALL_NOTIONAL = CALL_LIMIT * 5 * 100


def es_public(quotes=None):
    chains = {
        "SPY": chain_for("SPY", calls=ES_CALLS, puts=ES_PUTS),
        "QQQ": chain_for("QQQ", calls=CALLS, puts=PUTS),
    }
    greeks = greeks_for("SPY", calls=ES_CALLS, puts=ES_PUTS) | greeks_for("QQQ", calls=CALLS, puts=PUTS)
    return MultiChainPublic(chains, greeks, quotes)


def es_alert(**kw):
    base = underlying(strategy="PremarketES", underlying_price="580", qty=1, alert_id="es-1")
    base.update(kw)
    return base


def test_profile_is_otm_five_lots_without_premium_exits_or_flow_rules():
    defaults = relay.Settings.from_env({})
    profile = defaults.for_strategy("PremarketES")
    assert profile.delta_band_min == Decimal("0.35") and profile.delta_band_max == Decimal("0.40")
    assert profile.itm_points == Decimal("0") and profile.otm_points == Decimal("5")
    assert profile.order_qty == 5 and profile.max_qty == 5
    assert profile.take_profit is False and profile.stop_loss_percent is None
    assert profile.flow_rules is False
    assert profile.max_orders_per_day == defaults.max_orders_per_day
    assert profile.max_daily_opens == defaults.max_daily_opens == Decimal("1500")

    held = relay.Settings.from_env({
        "STOP_LOSS_PERCENT": "30", "TP_SINGLE": "true", "TP_PERCENT": "15", "MAX_QTY": "1",
    })
    overridden = held.for_strategy("PremarketES")
    assert overridden.take_profit is False and overridden.stop_loss_percent is None
    assert overridden.order_qty == 5 and overridden.max_qty == 5
    assert overridden.tp_single is True and overridden.tp_percent == Decimal("15")

    flow = held.for_strategy("FlowVWAP")
    assert flow.delta_band_min == Decimal("0.50") and flow.delta_band_max == Decimal("0.60")
    assert flow.itm_points == Decimal("0.50") and flow.otm_points == Decimal("0")
    assert flow.order_qty == 5 and flow.max_qty == 5 and flow.flow_rules is True
    assert flow.take_profit is True and flow.stop_loss_percent is None
    qqq = held.for_strategy("QQQ_EMA_VWAP")
    assert qqq.delta_band_min == Decimal("0.60") and qqq.delta_band_max == Decimal("0.75")
    assert qqq.itm_points == Decimal("5") and qqq.otm_points == Decimal("0")
    assert qqq.max_qty == 1 and qqq.order_qty is None and qqq.flow_rules is False
    assert qqq.take_profit is True and qqq.tp_percent == Decimal("50") and qqq.tp_single is True
    assert qqq.stop_loss_percent is None


def test_otm_window_includes_five_points_and_leaves_the_itm_window_alone():
    assert relay.in_window(Decimal("581"), SPOT, "C", Decimal("0"), Decimal("5"))
    assert relay.in_window(Decimal("585"), SPOT, "C", Decimal("0"), Decimal("5"))
    assert not relay.in_window(Decimal("585.01"), SPOT, "C", Decimal("0"), Decimal("5"))
    assert relay.in_window(Decimal("580"), SPOT, "C", Decimal("0"), Decimal("5"))
    assert not relay.in_window(Decimal("579.99"), SPOT, "C", Decimal("0"), Decimal("5"))
    assert relay.in_window(Decimal("575"), SPOT, "P", Decimal("0"), Decimal("5"))
    assert not relay.in_window(Decimal("574.99"), SPOT, "P", Decimal("0"), Decimal("5"))
    assert not relay.in_window(Decimal("580.01"), SPOT, "P", Decimal("0"), Decimal("5"))
    # FlowVWAP and the global default still stop at the ITM side. OTM stays out.
    assert relay.in_window(Decimal("579.50"), SPOT, "C", Decimal("0.50"))
    assert not relay.in_window(Decimal("580.01"), SPOT, "C", Decimal("0.50"))
    assert relay.in_window(Decimal("575"), SPOT, "C")
    assert not relay.in_window(Decimal("574"), SPOT, "C")


def test_long_buys_the_otm_call_and_short_buys_the_otm_put(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    client, _fake = make(
        tmp_path, public=es_public(quotes), MAX_DAILY_OPENS="5000",
        STOP_LOSS_PERCENT="30", TP_PERCENT="50", TP_SINGLE="true")
    opened = client.post("/webhook", json=es_alert())
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["selection"]["right"] == "C"
    assert body["selection"]["chosen_symbol"] == ES_CALL
    assert body["selection"]["chosen_strike"] == "581"
    assert Decimal(body["selection"]["delta"]) == Decimal("0.36")
    assert Decimal("0.35") <= abs(Decimal(body["selection"]["delta"])) <= Decimal("0.40")
    assert body["order"]["orderSide"] == "BUY" and body["order"]["quantity"] == "5"
    assert Decimal(body["selection"]["limit"]) == CALL_LIMIT
    assert "regime" not in body["selection"]
    assert "target_underlying" not in body["selection"]
    assert "stop_underlying" not in body["selection"]
    assert body["take_profit"]["attached"] is False and body["take_profit"]["quantity"] == 0
    assert body["take_profit"]["reason"] == relay.TAKE_PROFIT_OFF
    assert body["stop"]["attached"] is False
    assert body["stop"]["reason"] == "STOP_LOSS_PERCENT is not set"
    saved = client.sb.positions[("SPY", "PremarketES", "PREFLIGHT")]
    assert saved["qty"] == 5 and saved["status"] == "OPEN" and saved["symbol"] == ES_CALL
    assert client.sb.counters[("2026-10-19", "PREFLIGHT")] == [1, CALL_NOTIONAL]

    flat = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="es-flat", qty=1))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order"]["quantity"] == "5"
    short = client.post("/webhook", json=es_alert(
        side="sell", market_position="short", prev_market_position="flat", alert_id="es-put", qty=99))
    assert short.status_code == 200, short.text
    put = short.json()
    assert put["selection"]["right"] == "P" and put["selection"]["chosen_symbol"] == ES_PUT
    assert put["selection"]["chosen_strike"] == "579"
    assert Decimal(put["selection"]["delta"]) == Decimal("-0.36")
    assert put["order"]["orderSide"] == "BUY" and put["order"]["quantity"] == "5"
    assert put["take_profit"]["attached"] is False and put["stop"]["attached"] is False
    assert client.sb.positions[("SPY", "PremarketES", "PREFLIGHT")]["symbol"] == ES_PUT


def test_close_alert_flattens_only_the_premarketes_position(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    client, _fake = make(tmp_path, public=es_public(quotes), MAX_DAILY_OPENS="5000", MAX_ORDERS_PER_DAY="5")
    assert client.post("/webhook", json=es_alert()).status_code == 200
    assert client.post("/webhook", json=underlying(
        strategy="FlowVWAP", underlying_price="580", qty=1, alert_id="flow-open")).status_code == 200
    flow = client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert flow["status"] == "OPEN" and flow["qty"] == 5 and flow["symbol"] == occ("580", "C")

    flat = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="es-close", qty=1))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order"]["orderSide"] == "SELL" and flat.json()["order"]["quantity"] == "5"
    assert flat.json()["order"]["instrument"]["symbol"] == ES_CALL
    assert client.sb.positions[("SPY", "PremarketES", "PREFLIGHT")]["status"] == "CLOSED"
    assert client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "OPEN"


def test_daily_caps_stay_shared_and_a_five_lot_spends_limit_times_qty_times_100(tmp_path):
    client, _fake = make(tmp_path, public=es_public(), MAX_DAILY_OPENS="500")
    opened = client.post("/webhook", json=es_alert(alert_id="spend"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["order"]["quantity"] == "5"
    assert client.sb.counters[("2026-10-19", "PREFLIGHT")] == [1, CALL_NOTIONAL]
    blocked = client.post("/webhook", json=qqq_alert(alert_id="qqq-spend"))
    assert blocked.status_code == 422 and "spend" in blocked.json()["error"]
    assert ("QQQ", "QQQ_EMA_VWAP", "PREFLIGHT") not in client.sb.positions
    assert client.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 1

    roomy, _fake = make(tmp_path / "orders", public=es_public(), MAX_DAILY_OPENS="5000")
    assert roomy.post("/webhook", json=es_alert(alert_id="ord-es")).status_code == 200
    qqq = roomy.post("/webhook", json=qqq_alert(alert_id="ord-qqq"))
    assert qqq.status_code == 200, qqq.text
    assert qqq.json()["selection"]["chosen_strike"] == "577" and qqq.json()["selection"]["delta"] == "0.66"
    assert qqq.json()["order"]["quantity"] == "1" and qqq.json()["take_profit"]["attached"] is True
    flow = roomy.post("/webhook", json=underlying(
        strategy="FlowVWAP", underlying_price="580", qty=1, alert_id="ord-flow"))
    assert flow.status_code == 200, flow.text
    assert flow.json()["selection"]["chosen_strike"] == "580" and flow.json()["selection"]["delta"] == "0.55"
    assert flow.json()["order"]["quantity"] == "5"
    assert flow.json()["take_profit"]["reason"] == flowvwap.UNDERLYING_EXIT
    fourth = roomy.post("/webhook", json={
        "passphrase": PASS, "symbol": "AAPL261120C00250000", "side": "BUY", "open_close": "OPEN",
        "limit_price": "1.50", "qty": 1, "alert_id": "ord-4", "strategy": "Tape",
    })
    assert fourth.status_code == 422 and "MAX_ORDERS_PER_DAY" in fourth.json()["error"]
    assert roomy.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3


def test_entries_use_the_regular_window_and_the_flow_monitor_ignores_them(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    clock = {"now": datetime(2026, 10, 19, 9, 30, tzinfo=ET)}
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    client, _fake = make(
        tmp_path, public=es_public(quotes), clock=lambda: clock["now"], MAX_DAILY_OPENS="5000")
    early = client.post("/webhook", json=es_alert(alert_id="early"))
    assert early.status_code == 422 and "09:31-15:55" in early.json()["error"]

    clock["now"] = datetime(2026, 10, 19, 9, 31, tzinfo=ET)
    opened = client.post("/webhook", json=es_alert(alert_id="open-931"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["selection"]["chosen_strike"] == "581"

    clock["now"] = datetime(2026, 10, 19, 11, 45, tzinfo=ET)
    closed = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="flat-931", qty=1))
    assert closed.status_code == 200, closed.text
    later = client.post("/webhook", json=es_alert(alert_id="after-1130"))
    assert later.status_code == 200, later.text

    holding = client.get("/flowvwap-monitor", headers={"Authorization": "Bearer cron-s"})
    assert holding.status_code == 200, holding.text
    assert holding.json()["checked"] == 0 and holding.json()["actions"] == []
    assert client.sb.positions[("SPY", "PremarketES", "PREFLIGHT")]["status"] == "OPEN"

    clock["now"] = datetime(2026, 10, 19, 15, 56, tzinfo=ET)
    late = client.post("/webhook", json=es_alert(alert_id="late"))
    assert late.status_code == 422 and "09:31-15:55" in late.json()["error"]


def test_preflight_returns_the_otm_contract_and_places_nothing(tmp_path):
    client, fake = make(tmp_path, public=es_public(), MAX_DAILY_OPENS="5000")
    preview = client.post("/preflight", json=es_alert(alert_id="preview"))
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["passed"] is True and body["order_placed"] is False
    assert body["contract"] == ES_CALL and body["delta"] == "0.36" and body["premium"] == "0.90"
    assert client.sb.positions == {} and client.sb.counters == {} and client.sb.alerts == {}
    assert not any(call.url.path.endswith("/order") for call in fake.calls)

    health = client.get("/health").json()["settings"]
    assert health["daily_caps"] == "shared"
    listed = health["strategies"]["PremarketES"]
    assert listed["delta_band_min"] == "0.35" and listed["delta_band_max"] == "0.40"
    assert listed["itm_points"] == "0" and listed["otm_points"] == "5"
    assert listed["order_qty"] == 5 and listed["max_qty"] == 5
    assert listed["take_profit"] is False and listed["stop_loss_percent"] is None
    assert listed["flow_rules"] is False
    assert health["strategies"]["FlowVWAP"]["flow_rules"] is True
    assert health["strategies"]["FlowVWAP"]["delta_band_min"] == "0.50"
    assert health["strategies"]["QQQ_EMA_VWAP"]["delta_band_min"] == "0.60"
    assert health["strategies"]["QQQ_EMA_VWAP"]["max_qty"] == 1


def test_paper_open_places_five_and_no_take_profit_or_stop_then_the_close_sells_them(tmp_path):
    quotes = {ES_CALL: option_quote(ES_CALL, "0.70", "0.90")}
    broker = FakeAlpaca()
    client, public = make(
        tmp_path, mode="PAPER", public=es_public(quotes), alpaca=broker,
        MAX_DAILY_OPENS="5000", STOP_LOSS_PERCENT="30", TP_PERCENT="50")
    opened = client.post("/webhook", json=es_alert(alert_id="paper-open"))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["order_placed"] is True and body["order"]["qty"] == "5"
    assert body["selection"]["chosen_symbol"] == ES_CALL
    assert body["take_profit"]["attached"] is False and body["take_profit"]["reason"] == relay.TAKE_PROFIT_OFF
    assert body["stop"]["attached"] is False
    assert body["stop"]["reason"] == "STOP_LOSS_PERCENT is not set"
    posts = broker.posts()
    assert len(posts) == 1
    assert posts[0]["position_intent"] == "buy_to_open" and posts[0]["qty"] == "5"
    assert posts[0]["side"] == "buy" and posts[0]["type"] == "limit"
    saved = client.sb.positions[("SPY", "PremarketES", "PAPER")]
    assert saved["qty"] == 5 and saved["exits_placed"] is True
    assert saved["tp_status"] == "NONE" and saved["stop_status"] == "NONE"
    assert client.sb.counters[("2026-10-19", "PAPER")][1] == CALL_NOTIONAL

    flat = client.post("/webhook", json=es_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="paper-flat", qty=1))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order_placed"] is True
    sells = [order for order in broker.posts() if order["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["qty"] == "5" and sells[0]["position_intent"] == "sell_to_close"
    assert sells[0]["type"] == "limit" and "stop_price" not in sells[0]
    assert client.sb.positions[("SPY", "PremarketES", "PAPER")]["status"] == "CLOSED"
    assert not any(call.url.path.endswith("/order") for call in public.calls)
