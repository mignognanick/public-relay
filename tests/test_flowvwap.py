"""FlowVWAP rules: strike window, underlying exits, session gates, and the shared daily cap."""
import os
from datetime import date, datetime
from decimal import Decimal

os.environ["RELAY_NO_AUTOAPP"] = "1"

import httpx
import pytest

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
import flowvwap  # noqa: E402
import index as relay  # noqa: E402
from test_index import (  # noqa: E402
    ET, OPEN_TIME, PASS, CALLS, PUTS, ChainPublic, FakeAlpaca, MultiChainPublic,
    chain_for, greeks_for, make, occ, option_quote, qqq_alert, underlying,
)

DAY = date(2026, 10, 19)
SPOT = Decimal("580")
FLOW_CALLS = [
    ("579", "0.72", "2.20", "2.40"),  # 1.00 ITM, outside the 0.50 window
    ("580", "0.55", "1.20", "1.40"),
    ("581", "0.42", "0.40", "0.55"),  # OTM
]
FLOW_PUTS = [
    ("579", "-0.42", "0.40", "0.55"),
    ("580", "-0.55", "1.10", "1.30"),
    ("581", "-0.72", "2.10", "2.30"),
]
FLOW_CALL = occ("580", "C")
FLOW_PUT = occ("580", "P")


def _leg(strike, delta):
    return relay.Leg(f"S{strike}", Decimal(strike), Decimal(delta), "greeks", Decimal("1"), Decimal("1.10"))


def test_strike_window_is_atm_to_half_a_point_and_the_delta_band():
    assert relay.in_window(Decimal("580"), SPOT, "C", Decimal("0.50"))
    assert relay.in_window(Decimal("579.50"), SPOT, "C", Decimal("0.50"))
    assert not relay.in_window(Decimal("579.49"), SPOT, "C", Decimal("0.50"))
    assert not relay.in_window(Decimal("580.01"), SPOT, "C", Decimal("0.50"))
    assert relay.in_window(Decimal("580.50"), SPOT, "P", Decimal("0.50"))
    assert not relay.in_window(Decimal("580.51"), SPOT, "P", Decimal("0.50"))
    # 0.55 is the center of 0.50-0.60 and beats a deeper strike that is also in band.
    window = [_leg("579.80", "0.60"), _leg("580", "0.55"), _leg("579.60", "0.70")]
    assert relay.choose_leg(window, SPOT, Decimal("0.50"), Decimal("0.60")).symbol == "S580"
    profile = relay.Settings.from_env({}).for_strategy("FlowVWAP")
    assert profile.delta_band_min == Decimal("0.50") and profile.delta_band_max == Decimal("0.60")
    assert profile.itm_points == Decimal("0.50") and profile.order_qty == 5 and profile.max_premium is None
    qqq = relay.Settings.from_env({}).for_strategy("QQQ_EMA_VWAP")
    assert qqq.delta_band_min == Decimal("0.60") and qqq.delta_band_max == Decimal("0.75")
    assert qqq.itm_points == Decimal("5") and qqq.max_qty == 1 and qqq.flow_rules is False


def test_stop_target_king_cap_and_minimum_room():
    long = flowvwap.exit_levels(SPOT, "BUY", None)
    assert long["stop"] == Decimal("579.50") and long["target"] == Decimal("581")
    assert long["room"] == Decimal("1") and long["capped"] is False and long["skip"] is None

    short = flowvwap.exit_levels(SPOT, "SELL", None)
    assert short["stop"] == Decimal("580.50") and short["target"] == Decimal("579")
    assert short["room"] == Decimal("1") and short["skip"] is None

    # King 582.50 caps a long at 580.50 (two $1 strikes before King). Room is exactly 0.50.
    exact = flowvwap.exit_levels(SPOT, "BUY", Decimal("582.50"))
    assert exact["target"] == Decimal("580.50") and exact["room"] == Decimal("0.50")
    assert exact["capped"] is True and exact["skip"] is None

    tight = flowvwap.exit_levels(SPOT, "BUY", Decimal("582.49"))
    assert tight["target"] == Decimal("580.49") and tight["room"] == Decimal("0.49") and tight["skip"]

    # King far above the +1.00 target does not pull the target back.
    far = flowvwap.exit_levels(SPOT, "BUY", Decimal("590"))
    assert far["target"] == Decimal("581") and far["capped"] is False and far["skip"] is None

    # Short: two strikes before a King at 577.50 is 579.50, which is inside the 1.00 target.
    capped_short = flowvwap.exit_levels(SPOT, "SELL", Decimal("577.50"))
    assert capped_short["target"] == Decimal("579.50") and capped_short["room"] == Decimal("0.50")
    assert capped_short["skip"] is None
    too_close = flowvwap.exit_levels(SPOT, "SELL", Decimal("577.51"))
    assert too_close["room"] == Decimal("0.49") and too_close["skip"]

    # King only one point overhead: two strikes before it is already behind the entry.
    wrong = flowvwap.exit_levels(SPOT, "BUY", Decimal("581"))
    assert wrong["room"] < 0 and wrong["skip"]
    # A King that is not in the trade's direction does not cap the +1.00 target.
    assert flowvwap.exit_levels(SPOT, "BUY", Decimal("570"))["target"] == Decimal("581")
    assert flowvwap.exit_levels(SPOT, "SELL", Decimal("590"))["target"] == Decimal("579")
    assert flowvwap.exit_levels(SPOT, "SELL", Decimal("590"))["skip"] is None


def test_time_window_flatten_flip_and_regime():
    monday = datetime(2026, 10, 19, 9, 30, tzinfo=ET)
    assert flowvwap.entry_block(monday) is None
    assert flowvwap.entry_block(monday.replace(hour=11, minute=29)) is None
    assert flowvwap.entry_block(monday.replace(minute=29))  # 09:29
    assert flowvwap.entry_block(monday.replace(hour=11, minute=30))
    assert flowvwap.entry_block(datetime(2026, 10, 17, 10, 0, tzinfo=ET))  # Saturday
    assert flowvwap.flatten_due(monday.replace(hour=11, minute=30))
    assert not flowvwap.flatten_due(monday.replace(hour=11, minute=29))

    assert flowvwap.flip_skip(Decimal("580"), Decimal("581"))
    assert flowvwap.flip_skip(Decimal("580"), Decimal("579"))
    assert flowvwap.flip_skip(Decimal("580"), Decimal("581.00"))
    assert flowvwap.flip_skip(Decimal("580"), Decimal("581.01")) is None
    assert flowvwap.flip_skip(None, Decimal("580")) is None
    assert flowvwap.flip_skip(Decimal("580"), None) is None

    assert flowvwap.regime(Decimal("560"), Decimal("570"), "TREND_SHORT") == ("verified", [])
    assert flowvwap.regime(Decimal("560"), Decimal("570"), "trend_short") == ("verified", [])
    assert flowvwap.regime(Decimal("570"), Decimal("570"), "TREND_SHORT")[0] == "blocked"
    assert flowvwap.regime(Decimal("560"), Decimal("570"), "TREND_LONG")[0] == "blocked"
    status, missing = flowvwap.regime(None, Decimal("570"), "TREND_SHORT")
    assert status == "unverified" and "open_930" in missing
    status, missing = flowvwap.regime(Decimal("580"), None, "TREND_SHORT")
    assert status == "unverified" and "prior_rth_low" in missing
    status, missing = flowvwap.regime(Decimal("580"), Decimal("570"), None)
    assert status == "unverified" and missing == ["daybreak_bias"]
    status, missing = flowvwap.regime(None, None, "")
    assert status == "unverified" and len(missing) == 3

    assert flowvwap.exit_signal(monday.replace(hour=10), "BUY", Decimal("579.50"),
                                Decimal("579.50"), Decimal("581")) == "stop"
    assert flowvwap.exit_signal(monday.replace(hour=10), "BUY", Decimal("581"),
                                Decimal("579.50"), Decimal("581")) == "target"
    assert flowvwap.exit_signal(monday.replace(hour=10), "BUY", Decimal("580.20"),
                                Decimal("579.50"), Decimal("581")) is None
    assert flowvwap.exit_signal(monday.replace(hour=10), "SELL", Decimal("580.50"),
                                Decimal("580.50"), Decimal("579")) == "stop"
    assert flowvwap.exit_signal(monday.replace(hour=10), "SELL", Decimal("579"),
                                Decimal("580.50"), Decimal("579")) == "target"
    assert flowvwap.exit_signal(monday.replace(hour=11, minute=30), "BUY", Decimal("580"),
                                Decimal("579.50"), Decimal("581")) == "flatten"


def test_session_facts_from_public_and_alpaca_bars():
    daily = {"regularMarket": {"bars": [
        {"timestamp": "2026-10-16T09:30:00-04:00", "open": "575", "low": "570.25", "high": "578", "close": "576"},
        {"timestamp": "2026-10-19T09:30:00-04:00", "open": "572.10", "low": "571", "high": "581", "close": "580"},
    ]}}
    minutes = {"regularMarket": {"bars": [
        {"timestamp": "2026-10-19T09:30:00-04:00", "open": "572.40", "low": "572", "high": "573", "close": "572.80"},
        {"timestamp": "2026-10-19T09:31:00-04:00", "open": "572.80", "low": "572.5", "high": "573", "close": "572.9"},
    ]}}
    facts = flowvwap.session_facts(daily, minutes, DAY)
    assert facts["open_930"] == Decimal("572.40")
    assert facts["prior_rth_low"] == Decimal("570.25")
    # No minute bar: the daily open is the 9:30 open.
    fallback = flowvwap.session_facts(daily, {"regularMarket": {"bars": []}}, DAY)
    assert fallback["open_930"] == Decimal("572.10") and fallback["prior_rth_low"] == Decimal("570.25")
    empty = flowvwap.session_facts({}, {}, DAY)
    assert empty["open_930"] is None and empty["prior_rth_low"] is None

    alpaca_daily = {"bars": [
        {"t": "2026-10-16T04:00:00Z", "o": 575, "l": 569.5, "h": 578, "c": 576},
        {"t": "2026-10-19T04:00:00Z", "o": 571, "l": 570, "h": 582, "c": 580},
    ]}
    alpaca_min = {"bars": [{"t": "2026-10-19T13:30:00Z", "o": 571.25, "l": 571, "h": 572, "c": 571.8}]}
    alt = flowvwap.session_facts(alpaca_daily, alpaca_min, DAY)
    assert alt["open_930"] == Decimal("571.25") and alt["prior_rth_low"] == Decimal("569.5")


class FlowPublic(ChainPublic):
    def __init__(self, daily=None, minutes=None, **kw):
        super().__init__(**kw)
        self.daily = daily if daily is not None else {"regularMarket": {"bars": []}}
        self.minutes = minutes if minutes is not None else {"regularMarket": {"bars": []}}

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if "/historicdata/" in path:
            self.calls.append(req)
            payload = self.daily if path.endswith("/ONE_DAY") else self.minutes
            return httpx.Response(200, json=payload)
        return super().__call__(req)


def flow_chain(**kw):
    chain = chain_for("SPY", calls=FLOW_CALLS, puts=FLOW_PUTS)
    greeks = greeks_for("SPY", calls=FLOW_CALLS, puts=FLOW_PUTS)
    return FlowPublic(chain=chain, greeks=greeks, **kw)


def flow_alert(**kw):
    base = underlying(strategy="FlowVWAP", underlying_price="580", qty=1, alert_id="flow-1")
    base.update(kw)
    return base


def bars(open_px="560", prior_low="570"):
    daily = {"regularMarket": {"bars": [
        {"timestamp": "2026-10-16T09:30:00-04:00", "open": "575", "low": prior_low, "high": "578", "close": "576"},
        {"timestamp": "2026-10-19T09:30:00-04:00", "open": open_px, "low": open_px, "high": "581", "close": "580"},
    ]}}
    minutes = {"regularMarket": {"bars": [
        {"timestamp": "2026-10-19T09:30:00-04:00", "open": open_px, "low": open_px, "high": "573", "close": open_px},
    ]}}
    return daily, minutes


def test_flow_entry_buys_five_near_atm_and_skips_the_premium_cap(tmp_path, caplog):
    caplog.set_level("INFO", logger="relay")
    daily, minutes = bars()
    public = flow_chain(daily=daily, minutes=minutes)
    client, _fake = make(tmp_path, public=public, MAX_PREMIUM="0.50", MAX_DAILY_OPENS="5000",
                         STOP_LOSS_PERCENT="30", TP_PERCENT="50")
    missing = client.post("/levels", json={"flip": "500", "king": "590", "daybreak_bias": "TREND_SHORT"})
    assert missing.status_code == 401

    # Bias was not posted, so the regime is unverified and the entry is allowed.
    opened = client.post("/webhook", json=flow_alert())
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["selection"]["chosen_symbol"] == FLOW_CALL
    assert body["selection"]["right"] == "C" and body["selection"]["chosen_strike"] == "580"
    assert body["selection"]["delta"] == "0.55"
    assert body["order"]["quantity"] == "5"
    assert Decimal(body["selection"]["limit"]) == Decimal("1.30")
    assert Decimal(body["selection"]["limit"]) > Decimal("0.50")
    assert body["selection"]["regime"] == "unverified"
    assert body["selection"]["entry_underlying"] == "580"
    assert body["selection"]["stop_underlying"] == "579.5"
    assert body["selection"]["target_underlying"] == "581"
    assert body["take_profit"]["attached"] is False
    assert body["take_profit"]["reason"] == flowvwap.UNDERLYING_EXIT
    assert body["stop"]["attached"] is False
    assert "unverified" in caplog.text
    saved = client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]
    assert saved["qty"] == 5 and saved["regime"] == "unverified"
    assert saved["entry_underlying"] == "580" and saved["stop_underlying"] == "579.5"


def test_long_call_and_short_put_with_a_verified_regime(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    daily, minutes = bars(open_px="560", prior_low="570")
    public = flow_chain(
        daily=daily, minutes=minutes,
        quotes={FLOW_CALL: option_quote(FLOW_CALL, "1.00", "1.20"),
                FLOW_PUT: option_quote(FLOW_PUT, "0.90", "1.10")})
    client, _fake = make(tmp_path, public=public, MAX_DAILY_OPENS="5000", MAX_ORDERS_PER_DAY="5")
    posted = client.post(
        "/levels",
        headers={"Authorization": "Bearer cron-s"},
        json={"flip": "500", "king": "590", "daybreak_bias": "TREND_SHORT"},
    )
    assert posted.status_code == 200, posted.text
    again = client.post(
        "/levels",
        headers={"Authorization": "Bearer cron-s"},
        json={"symbol": "SPY", "day": "2026-10-19", "flip": "500", "king": "590", "daybreak_bias": "TREND_SHORT"},
    )
    assert again.status_code == 200 and again.json()["flip"] == "500"

    call = client.post("/webhook", json=flow_alert())
    assert call.status_code == 200, call.text
    assert call.json()["selection"]["regime"] == "verified"
    assert call.json()["selection"]["right"] == "C" and call.json()["order"]["quantity"] == "5"
    assert call.json()["selection"]["target_capped"] is False

    flat = client.post("/webhook", json=flow_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="flat-1", qty=99))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order"]["quantity"] == "5"
    assert client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "CLOSED"

    # King has to sit below a short. 570 caps the put at 572, which is past the 1.00 target.
    assert client.post(
        "/levels", headers={"Authorization": "Bearer cron-s"},
        json={"flip": "500", "king": "570", "daybreak_bias": "TREND_SHORT"}).status_code == 200
    put = client.post("/webhook", json=flow_alert(
        side="sell", market_position="short", prev_market_position="flat", alert_id="flow-put"))
    assert put.status_code == 200, put.text
    assert put.json()["selection"]["chosen_symbol"] == FLOW_PUT
    assert put.json()["selection"]["right"] == "P"
    assert put.json()["selection"]["stop_underlying"] == "580.5"
    assert put.json()["selection"]["target_underlying"] == "579"
    assert put.json()["order"]["quantity"] == "5"


def test_flip_skip_regime_block_and_king_room(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    daily, minutes = bars(open_px="580.40", prior_low="590")
    public = flow_chain(daily=daily, minutes=minutes)
    client, _fake = make(tmp_path, public=public, MAX_DAILY_OPENS="5000")
    headers = {"Authorization": "Bearer cron-s"}
    # Open 580.40 is within 1.00 of flip 581, even though the regime would pass.
    assert client.post("/levels", headers=headers, json={
        "flip": "581", "king": "590", "daybreak_bias": "TREND_SHORT"}).status_code == 200
    skipped = client.post("/webhook", json=flow_alert(alert_id="flip"))
    assert skipped.status_code == 422 and "flip skip" in skipped.json()["error"]
    assert ("SPY", "FlowVWAP", "PREFLIGHT") not in client.sb.positions

    # Same prices, flip far away, but the open is not below the prior low.
    blocked_public = flow_chain(daily=daily, minutes=minutes)
    blocked, _fake = make(tmp_path / "blocked", public=blocked_public, MAX_DAILY_OPENS="5000")
    assert blocked.post("/levels", headers=headers, json={
        "flip": "500", "king": "590", "daybreak_bias": "TREND_LONG"}).status_code == 200
    regime = blocked.post("/webhook", json=flow_alert(alert_id="regime"))
    assert regime.status_code == 422 and "regime" in regime.json()["error"]

    # Verified regime, but two strikes before King leaves under 0.50 of room.
    tight_public = flow_chain(daily=daily, minutes=minutes)
    tight, _fake = make(tmp_path / "tight", public=tight_public, MAX_DAILY_OPENS="5000")
    assert tight.post("/levels", headers=headers, json={
        "flip": "500", "king": "582.49", "daybreak_bias": "TREND_SHORT"}).status_code == 200
    # prior low 590 and open 580.40 is below it, bias is short, flip is far. Room is the skip.
    room = tight.post("/webhook", json=flow_alert(alert_id="room"))
    assert room.status_code == 422 and "room" in room.json()["error"]
    assert ("SPY", "FlowVWAP", "PREFLIGHT") not in tight.sb.positions


def test_entry_window_and_monitor_stop_target_and_flatten(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    headers = {"Authorization": "Bearer cron-s"}
    clock = {"now": datetime(2026, 10, 19, 9, 29, tzinfo=ET)}
    public = flow_chain()
    quotes = {
        "SPY": {"instrument": {"symbol": "SPY", "type": "EQUITY"}, "outcome": "SUCCESS",
                "last": "580.10", "bid": "580.00", "ask": "580.20"},
        FLOW_CALL: option_quote(FLOW_CALL, "1.00", "1.20"),
    }
    public.quotes = quotes
    client, _fake = make(tmp_path, public=public, clock=lambda: clock["now"], MAX_DAILY_OPENS="5000")
    early = client.post("/webhook", json=flow_alert(alert_id="early"))
    assert early.status_code == 422 and "09:30-11:30" in early.json()["error"]

    clock["now"] = datetime(2026, 10, 19, 9, 30, tzinfo=ET)
    opened = client.post("/webhook", json=flow_alert(alert_id="open-930"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["selection"]["regime"] == "unverified"

    denied = client.get("/flowvwap-monitor")
    assert denied.status_code == 401
    holding = client.get("/flowvwap-monitor", headers=headers)
    assert holding.status_code == 200, holding.text
    assert holding.json()["checked"] == 1 and holding.json()["actions"][0]["action"] == "hold"

    clock["now"] = datetime(2026, 10, 19, 10, 15, tzinfo=ET)
    public.quotes["SPY"]["last"] = "579.50"
    stopped = client.post("/flowvwap-monitor", headers=headers)
    assert stopped.status_code == 200, stopped.text
    action = stopped.json()["actions"][0]
    assert action["action"] == "stop" and action["quantity"] == 5 and action["order_placed"] is False
    assert client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "CLOSED"
    again = client.get("/flowvwap-monitor", headers=headers)
    assert again.json()["checked"] == 0 and again.json()["actions"] == []

    # Target sells the whole position, and 11:30 flattens even when price has not reached it.
    clock["now"] = datetime(2026, 10, 19, 10, 0, tzinfo=ET)
    public.quotes["SPY"]["last"] = "581.00"
    assert client.post("/webhook", json=flow_alert(alert_id="second")).status_code == 200
    targeted = client.get("/flowvwap-monitor", headers=headers)
    assert targeted.json()["actions"][0]["action"] == "target"
    assert targeted.json()["actions"][0]["quantity"] == 5

    clock["now"] = datetime(2026, 10, 19, 10, 5, tzinfo=ET)
    public.quotes["SPY"]["last"] = "580.10"
    assert client.post("/webhook", json=flow_alert(alert_id="third")).status_code == 200
    clock["now"] = datetime(2026, 10, 19, 11, 30, tzinfo=ET)
    flattened = client.get("/flowvwap-monitor", headers=headers)
    assert flattened.json()["actions"][0]["action"] == "flatten"
    assert flattened.json()["actions"][0]["quantity"] == 5
    assert client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "CLOSED"
    late_entry = client.post("/webhook", json=flow_alert(alert_id="after-1130"))
    assert late_entry.status_code == 422 and "09:30-11:30" in late_entry.json()["error"]
    # The open position is already closed, so the same minute does not sell again.
    assert client.get("/flowvwap-monitor", headers=headers).json()["checked"] == 0


def test_paper_monitor_sells_once(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    clock = {"now": OPEN_TIME}
    quotes = {
        "SPY": {"instrument": {"symbol": "SPY", "type": "EQUITY"}, "outcome": "SUCCESS",
                "last": "579.40", "bid": "579.30", "ask": "579.50"},
        FLOW_CALL: option_quote(FLOW_CALL, "0.90", "1.10"),
    }
    public = flow_chain(quotes=quotes)
    broker = FakeAlpaca()
    client, _fake = make(
        tmp_path, mode="PAPER", public=public, alpaca=broker, clock=lambda: clock["now"],
        MAX_DAILY_OPENS="5000")
    opened = client.post("/webhook", json=flow_alert(alert_id="paper-open"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["order_placed"] is True
    assert opened.json()["take_profit"]["reason"] == flowvwap.UNDERLYING_EXIT
    assert opened.json()["stop"]["reason"] == flowvwap.UNDERLYING_EXIT
    buys = [order for order in broker.posts() if order["position_intent"] == "buy_to_open"]
    assert buys[0]["qty"] == "5"
    clock["now"] = datetime(2026, 10, 19, 10, 20, tzinfo=ET)
    stopped = client.get("/flowvwap-monitor", headers={"Authorization": "Bearer cron-s"})
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["actions"][0]["action"] == "stop"
    assert stopped.json()["actions"][0]["quantity"] == 5
    sells = [order for order in broker.posts() if order["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == "5" and sells[0]["position_intent"] == "sell_to_close"
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"
    client.get("/flowvwap-monitor", headers={"Authorization": "Bearer cron-s"})
    assert len([order for order in broker.posts() if order["side"] == "sell"]) == 1


def test_daily_cap_stays_shared_with_qqq(tmp_path):
    qqq_calls = CALLS
    multi = MultiChainPublic(
        {"SPY": chain_for("SPY", calls=FLOW_CALLS, puts=FLOW_PUTS),
         "QQQ": chain_for("QQQ", calls=qqq_calls, puts=PUTS)},
        greeks_for("SPY", calls=FLOW_CALLS, puts=FLOW_PUTS) | greeks_for("QQQ", calls=qqq_calls, puts=PUTS),
    )
    client, _fake = make(tmp_path, public=multi, MAX_DAILY_OPENS="5000")
    first = client.post("/webhook", json=flow_alert(alert_id="cap-1"))
    assert first.status_code == 200, first.text
    assert first.json()["order"]["quantity"] == "5"
    qqq = client.post("/webhook", json=qqq_alert(alert_id="cap-qqq"))
    assert qqq.status_code == 200, qqq.text
    assert qqq.json()["selection"]["chosen_strike"] == "577" and qqq.json()["selection"]["delta"] == "0.66"
    assert qqq.json()["order"]["quantity"] == "1"
    assert qqq.json()["take_profit"]["attached"] is True
    third = client.post("/webhook", json={
        "passphrase": PASS, "symbol": "AAPL261120C00250000", "side": "BUY", "open_close": "OPEN",
        "limit_price": "1.50", "qty": 1, "alert_id": "cap-3", "strategy": "Tape",
    })
    assert third.status_code == 200, third.text
    fourth = client.post("/webhook", json={
        "passphrase": PASS, "symbol": "AAPL261120C00250000", "side": "BUY", "open_close": "OPEN",
        "limit_price": "1.50", "qty": 1, "alert_id": "cap-4", "strategy": "Tape",
    })
    assert fourth.status_code == 422 and "MAX_ORDERS_PER_DAY" in fourth.json()["error"]
    assert client.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3
    assert client.sb.positions[("SPY", "FlowVWAP", "PREFLIGHT")]["status"] == "OPEN"
    assert client.sb.positions[("QQQ", "QQQ_EMA_VWAP", "PREFLIGHT")]["qty"] == 1


def test_three_flow_round_trips_then_the_shared_cap(tmp_path):
    quotes = {FLOW_CALL: option_quote(FLOW_CALL, "1.00", "1.20")}
    client, _fake = make(tmp_path, public=flow_chain(quotes=quotes), MAX_DAILY_OPENS="5000")
    for n in range(3):
        opened = client.post("/webhook", json=flow_alert(alert_id=f"trip-{n}"))
        assert opened.status_code == 200, opened.text
        flat = client.post("/webhook", json=flow_alert(
            side="sell", market_position="flat", prev_market_position="long", alert_id=f"flat-{n}", qty=1))
        assert flat.status_code == 200, flat.text
        assert flat.json()["order"]["quantity"] == "5"
    fourth = client.post("/webhook", json=flow_alert(alert_id="trip-4"))
    assert fourth.status_code == 422 and "MAX_ORDERS_PER_DAY" in fourth.json()["error"]
    assert client.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 3
