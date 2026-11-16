"""FlowVWAP PAPER limit entry, and the optional Resend notices for those events."""
import json
import os
from datetime import datetime, timedelta
from decimal import Decimal

os.environ["RELAY_NO_AUTOAPP"] = "1"

import httpx
import pytest

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
import index as relay  # noqa: E402
from api.notify import contract_pnl, money, scrub  # noqa: E402
from test_flowvwap import FLOW_CALL, flow_alert, flow_chain  # noqa: E402
from test_index import ET, OPEN_TIME, PASS, SECRET, FakeAlpaca, make, option_quote, qqq_alert, two_chains  # noqa: E402
from test_premarketes import CALL_LIMIT, es_alert, es_public  # noqa: E402

CRON = {"Authorization": "Bearer cron-s"}
MAIL = {
    "NOTIFY_EMAIL_ENABLED": "true",
    "RESEND_API_KEY": "re_test_key_not_real",
    "NOTIFY_EMAIL_FROM": "relay-bot@example.com",
    "NOTIFY_EMAIL_TO": "ops@example.com",
}


def spy_quote(last):
    return {
        "instrument": {"symbol": "SPY", "type": "EQUITY"}, "outcome": "SUCCESS",
        "last": last, "bid": last, "ask": last,
    }


class Resend:
    def __init__(self, status=200):
        self.calls = []
        self.status = status

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.calls.append(req)
        return httpx.Response(self.status, json={"id": "msg-1"})

    def messages(self):
        return [json.loads(call.content) for call in self.calls]


def paper(tmp_path, broker=None, clock=None, quotes=None, notify=None, **env):
    clock = clock or {"now": OPEN_TIME}
    quotes = quotes if quotes is not None else {
        "SPY": spy_quote("580"),
        FLOW_CALL: option_quote(FLOW_CALL, "1.20", "1.40"),
    }
    broker = broker if broker is not None else FakeAlpaca(entry_status="new")
    client, public = make(
        tmp_path, mode="PAPER", public=flow_chain(quotes=quotes), alpaca=broker,
        clock=lambda: clock["now"], notify=notify, MAX_DAILY_OPENS="5000", **env)
    return client, public, broker, clock


def test_limit_floors_down_to_the_tick_and_pennies_follow_ppind():
    # Under $3 the tick is a cent. At/above $3 it is a nickel unless the contract is penny-program.
    assert relay.floor_option_price(Decimal("1.239")) == Decimal("1.23")
    assert relay.floor_option_price(Decimal("0.604")) == Decimal("0.60")
    assert relay.floor_option_price(Decimal("3.04")) == Decimal("3.00")
    assert relay.floor_option_price(Decimal("3.72")) == Decimal("3.70")
    assert relay.floor_option_price(Decimal("4.12")) == Decimal("4.10")
    assert relay.floor_option_price(Decimal("3.04"), penny=True) == Decimal("3.04")
    assert relay.floor_option_price(Decimal("4.12"), penny=True) == Decimal("4.12")
    # A positive remainder smaller than one tick is raised to that tick. Zero is not a price.
    assert relay.floor_option_price(Decimal("0.004")) == Decimal("0.01")
    assert relay.flow_entry_limit(Decimal("1.30"), Decimal("0.40")) == Decimal("0.90")
    assert relay.flow_entry_limit(Decimal("4.52"), Decimal("0.40")) == Decimal("4.10")
    assert relay.flow_entry_limit(Decimal("4.52"), Decimal("0.40"), penny=True) == Decimal("4.12")
    assert relay.flow_entry_limit(Decimal("0.40"), Decimal("0.40")) is None
    assert relay.flow_entry_limit(Decimal("0.30"), Decimal("0.40")) is None
    per, total = contract_pnl(Decimal("0.90"), Decimal("0.50"), 5)
    assert per == Decimal("-40.0") and total == Decimal("-200.0")
    assert money(per) == "-40.00" and money(total) == "-200.00"


def test_health_shows_the_flow_limit_and_hides_mail_secrets(tmp_path):
    plain, _ = make(tmp_path)
    health = plain.get("/health").json()
    flow = health["settings"]["strategies"]["FlowVWAP"]
    assert flow["entry_limit_offset"] == "0.40"
    assert flow["entry_limit_timeout_sec"] == 300
    assert flow["notify_email"] is True
    assert health["settings"]["notify_email_enabled"] is False
    assert health["settings"]["notify_email_configured"] is False
    for name in ("QQQ_EMA_VWAP", "PremarketES", "PremarketES_MU"):
        assert health["settings"]["strategies"][name]["entry_limit_offset"] is None
        assert health["settings"]["strategies"][name]["notify_email"] is False

    configured, _ = make(tmp_path / "mail", **MAIL)
    body = configured.get("/health").text
    assert configured.get("/health").json()["settings"]["notify_email_enabled"] is True
    assert configured.get("/health").json()["settings"]["notify_email_configured"] is True
    assert MAIL["RESEND_API_KEY"] not in body
    assert MAIL["NOTIFY_EMAIL_FROM"] not in body
    assert MAIL["NOTIFY_EMAIL_TO"] not in body
    assert PASS not in body and SECRET not in body


def test_paper_places_the_resting_limit_and_will_not_double_enter(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    client, _public, broker, _clock = paper(tmp_path)
    opened = client.post("/webhook", json=flow_alert(alert_id="lim-1"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["entry_status"] == "PENDING" and opened.json()["filled_qty"] == 0
    buy = broker.posts()[0]
    assert buy["side"] == "buy" and buy["type"] == "limit" and buy["time_in_force"] == "day"
    assert buy["position_intent"] == "buy_to_open" and buy["qty"] == "5"
    assert buy["limit_price"] == "0.90"  # mid 1.30 minus 0.40, already on a cent
    saved = client.sb.positions[("SPY", "FlowVWAP", "PAPER")]
    assert saved["status"] == "OPEN" and saved["entry_status"] == "PENDING"
    assert saved["entry_order_id"] == opened.json()["alpaca_order_id"]
    assert saved["entry_limit"] == "0.90"
    assert saved["entry_placed_at"]
    assert saved["alert_underlying"] == "580"
    assert saved["stop_underlying"] is None and saved["target_underlying"] is None
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    assert client.sb.counters[("2026-10-19", "PAPER")][1] == Decimal("450")  # 0.90 * 5 * 100

    again = client.post("/webhook", json=flow_alert(alert_id="lim-1"))
    assert again.status_code == 200 and again.json()["duplicate"] is True
    second = client.post("/webhook", json=flow_alert(alert_id="lim-2"))
    assert second.status_code == 422 and "already open" in second.json()["error"]
    assert len(broker.posts()) == 1

    holding = client.get("/flowvwap-monitor", headers=CRON)
    assert holding.status_code == 200, holding.text
    assert holding.json()["actions"][0]["action"] == "working"
    assert broker.deletes == []


def test_non_positive_limit_is_a_skip_and_does_not_spend_the_cap(tmp_path, caplog):
    caplog.set_level("INFO", logger="relay")
    client, _public, broker, _clock = paper(
        tmp_path, STRATEGY_OVERRIDES='{"FlowVWAP": {"entry_limit_offset": "2"}}')
    skipped = client.post("/webhook", json=flow_alert(alert_id="too-low"))
    assert skipped.status_code == 422 and "limit_non_positive" in skipped.json()["error"]
    assert broker.posts() == []
    assert ("SPY", "FlowVWAP", "PAPER") not in client.sb.positions
    assert client.sb.counters.get(("2026-10-19", "PAPER"), [0, Decimal(0)])[0] == 0
    assert "limit_non_positive" in caplog.text


def test_fill_anchors_the_stop_to_spy_at_the_fill(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    quotes = {"SPY": spy_quote("581"), FLOW_CALL: option_quote(FLOW_CALL, "0.40", "0.60")}
    client, public, broker, clock = paper(tmp_path, quotes=quotes)
    opened = client.post("/webhook", json=flow_alert(alert_id="fill-1"))
    assert opened.status_code == 200, opened.text
    order = broker.orders[opened.json()["alpaca_order_id"]]
    order["status"] = "filled"
    order["filled_qty"] = "5"
    order["filled_avg_price"] = "0.90"
    order["filled_at"] = "2026-10-19T14:05:00Z"
    # Alert SPY was 580, so an alert-time stop would be 579.50 and 580.40 would not stop.
    # Fill-time SPY is 581, so the stop is 580.50 and 580.40 is a stop.
    seen = client.get("/flowvwap-monitor", headers=CRON)
    assert seen.status_code == 200, seen.text
    assert seen.json()["actions"][0]["action"] == "hold"
    saved = client.sb.positions[("SPY", "FlowVWAP", "PAPER")]
    assert saved["entry_underlying"] == "581"
    assert saved["stop_underlying"] == "580.5"
    assert saved["target_underlying"] == "582"
    assert saved["entry_filled"] == 5
    public.quotes["SPY"]["last"] = "580.40"
    clock["now"] = OPEN_TIME + timedelta(minutes=5)
    stopped = client.get("/flowvwap-monitor", headers=CRON)
    assert stopped.json()["actions"][0]["action"] == "stop"
    assert stopped.json()["actions"][0]["quantity"] == 5
    sells = [order for order in broker.posts() if order["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == "5"


def test_timeout_cancels_keeps_the_cap_and_a_webhook_can_do_it(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    caplog.set_level("INFO", logger="relay")
    client, _public, broker, clock = paper(tmp_path, MAX_ORDERS_PER_DAY="1")
    opened = client.post("/webhook", json=flow_alert(alert_id="wait"))
    order_id = opened.json()["alpaca_order_id"]
    clock["now"] = OPEN_TIME + timedelta(seconds=299)
    still = client.get("/flowvwap-monitor", headers=CRON)
    assert still.json()["actions"][0]["action"] == "working"
    assert broker.deletes == []

    clock["now"] = OPEN_TIME + timedelta(seconds=300)
    timed = client.get("/flowvwap-monitor", headers=CRON)
    assert timed.status_code == 200, timed.text
    assert timed.json()["actions"][0]["action"] == "skip"
    assert timed.json()["actions"][0]["reason"] == "limit_timeout"
    assert order_id in broker.deletes
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"
    assert "limit_timeout" in caplog.text
    assert client.sb.counters[("2026-10-19", "PAPER")][0] == 1
    blocked = client.post("/webhook", json=flow_alert(alert_id="after-timeout"))
    assert blocked.status_code == 422 and "MAX_ORDERS_PER_DAY" in blocked.json()["error"]
    again = client.get("/flowvwap-monitor", headers=CRON)
    assert again.json()["checked"] == 0

    # A different webhook, not the monitor, also cancels a working limit that has aged out.
    other, _public2, broker2, clock2 = paper(tmp_path / "hook")
    first = other.post("/webhook", json=flow_alert(alert_id="hook-wait"))
    assert first.status_code == 200, first.text
    clock2["now"] = OPEN_TIME + timedelta(seconds=301)
    occ = other.post("/webhook", json={
        "passphrase": PASS, "symbol": "AAPL261120C00250000", "side": "BUY", "open_close": "OPEN",
        "limit_price": "1.50", "qty": 1, "alert_id": "occ-tick",
    })
    assert occ.status_code == 200, occ.text
    assert first.json()["alpaca_order_id"] in broker2.deletes
    assert other.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"


def test_flat_and_1130_cancel_a_working_limit(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    client, _public, broker, clock = paper(tmp_path)
    opened = client.post("/webhook", json=flow_alert(alert_id="work"))
    order_id = opened.json()["alpaca_order_id"]
    flat = client.post("/webhook", json=flow_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="flat-work", qty=1))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order_placed"] is False
    assert order_id in flat.json()["cancelled_order_ids"]
    assert [post for post in broker.posts() if post["side"] == "sell"] == []
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"

    late_clock = {"now": datetime(2026, 10, 19, 11, 26, tzinfo=ET)}
    later, _public2, broker2, _clock2 = paper(tmp_path / "late", clock=late_clock)
    opened = later.post("/webhook", json=flow_alert(alert_id="late-work"))
    assert opened.status_code == 200, opened.text
    late_clock["now"] = datetime(2026, 10, 19, 11, 30, tzinfo=ET)
    flattened = later.get("/flowvwap-monitor", headers=CRON)
    assert flattened.status_code == 200, flattened.text
    assert flattened.json()["actions"][0]["action"] == "flatten"
    assert flattened.json()["actions"][0]["cancelled"] is True
    assert opened.json()["alpaca_order_id"] in broker2.deletes
    assert [post for post in broker2.posts() if post["side"] == "sell"] == []
    assert later.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"


def test_partial_fill_is_kept_and_the_rest_is_cancelled(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    quotes = {"SPY": spy_quote("581"), FLOW_CALL: option_quote(FLOW_CALL, "0.40", "0.60")}
    client, public, broker, clock = paper(tmp_path, quotes=quotes)
    opened = client.post("/webhook", json=flow_alert(alert_id="part"))
    order_id = opened.json()["alpaca_order_id"]
    order = broker.orders[order_id]
    order["status"] = "partially_filled"
    order["filled_qty"] = "2"
    order["filled_avg_price"] = "0.90"
    held = client.get("/flowvwap-monitor", headers=CRON)
    assert held.json()["actions"][0]["action"] == "working"
    assert held.json()["actions"][0]["entry_filled"] == 2
    assert broker.deletes == []
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["entry_filled"] == 2
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["stop_underlying"] == "580.5"

    clock["now"] = OPEN_TIME + timedelta(seconds=300)
    expired = client.get("/flowvwap-monitor", headers=CRON)
    assert order_id in broker.deletes
    assert expired.json()["actions"][0]["action"] == "hold"
    assert expired.json()["actions"][0]["remainder"] == "cancelled"
    saved = client.sb.positions[("SPY", "FlowVWAP", "PAPER")]
    assert saved["status"] == "OPEN" and saved["entry_filled"] == 2
    public.quotes["SPY"]["last"] = "580.40"
    stopped = client.get("/flowvwap-monitor", headers=CRON)
    assert stopped.json()["actions"][0]["action"] == "stop"
    assert stopped.json()["actions"][0]["quantity"] == 2
    sells = [post for post in broker.posts() if post["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == "2"

    # 11:30 sells the filled contracts and cancels the working remainder.
    late = {"now": datetime(2026, 10, 19, 11, 26, tzinfo=ET)}
    quotes2 = {"SPY": spy_quote("580.20"), FLOW_CALL: option_quote(FLOW_CALL, "0.40", "0.60")}
    client2, _public2, broker2, _clock2 = paper(tmp_path / "flat1130", clock=late, quotes=quotes2)
    opened2 = client2.post("/webhook", json=flow_alert(alert_id="part-1130"))
    resting = broker2.orders[opened2.json()["alpaca_order_id"]]
    resting["status"] = "partially_filled"
    resting["filled_qty"] = "2"
    resting["filled_avg_price"] = "0.90"
    late["now"] = datetime(2026, 10, 19, 11, 30, tzinfo=ET)
    flattened = client2.get("/flowvwap-monitor", headers=CRON)
    assert flattened.status_code == 200, flattened.text
    assert flattened.json()["actions"][0]["action"] == "flatten"
    assert flattened.json()["actions"][0]["quantity"] == 2
    assert opened2.json()["alpaca_order_id"] in broker2.deletes
    sells = [post for post in broker2.posts() if post["side"] == "sell"]
    assert len(sells) == 1 and sells[0]["qty"] == "2"
    assert client2.sb.positions[("SPY", "FlowVWAP", "PAPER")]["status"] == "CLOSED"


def test_offset_off_is_an_immediate_fill_and_other_strategies_stay_on_the_mid(tmp_path):
    client, _public, broker, _clock = paper(
        tmp_path, broker=FakeAlpaca(),
        STRATEGY_OVERRIDES='{"FlowVWAP": {"entry_limit_offset": 0}}')
    opened = client.post("/webhook", json=flow_alert(alert_id="now"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["entry_status"] == "FILLED"
    assert broker.posts()[0]["limit_price"] == "1.30"
    assert client.sb.positions[("SPY", "FlowVWAP", "PAPER")]["entry_placed_at"] is None

    es_broker = FakeAlpaca()
    es, _fake = make(
        tmp_path / "es", mode="PAPER", public=es_public(), alpaca=es_broker, MAX_DAILY_OPENS="5000")
    assert es.post("/webhook", json=es_alert(alert_id="es-paper")).status_code == 200
    assert es_broker.posts()[0]["limit_price"] == f"{CALL_LIMIT:.2f}"

    qqq_broker = FakeAlpaca()
    qqq, _fake = make(
        tmp_path / "qqq", mode="PAPER", public=two_chains(), alpaca=qqq_broker, MAX_DAILY_OPENS="5000")
    assert qqq.post("/webhook", json=qqq_alert(alert_id="qqq-paper")).status_code == 200
    assert qqq_broker.posts()[0]["limit_price"] == "3.30"


def _bodies(resend: Resend) -> str:
    return "\n".join(message["text"] + "\n" + message["subject"] for message in resend.messages())


def _assert_no_secrets(text: str, caplog_text: str):
    for secret in (PASS, SECRET, MAIL["RESEND_API_KEY"], "cron-s", MAIL["NOTIFY_EMAIL_FROM"], MAIL["NOTIFY_EMAIL_TO"]):
        assert secret not in text
        assert secret not in caplog_text


def test_mail_stays_off_until_configured_then_one_email_per_event(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    caplog.set_level("INFO", logger="relay")
    quiet = Resend()
    client, _public, broker, _clock = paper(tmp_path, notify=quiet)
    assert client.post("/webhook", json=flow_alert(alert_id="quiet")).status_code == 200
    assert quiet.calls == []

    resend = Resend()
    quotes = {"SPY": spy_quote("581"), FLOW_CALL: option_quote(FLOW_CALL, "0.40", "0.60")}
    live, public, broker, clock = paper(tmp_path / "mail", quotes=quotes, notify=resend, **MAIL)
    opened = live.post("/webhook", json=flow_alert(alert_id="mail-1"))
    assert opened.status_code == 200, opened.text
    assert len(resend.calls) == 1
    placed = resend.messages()[0]
    assert placed["to"] == ["ops@example.com"]
    assert "0.90" in placed["text"] and "1.30" in placed["text"] and FLOW_CALL in placed["text"]
    assert placed["subject"].startswith("FlowVWAP limit placed")
    assert resend.calls[0].headers["authorization"] == "Bearer re_test_key_not_real"
    live.get("/flowvwap-monitor", headers=CRON)
    assert len(resend.calls) == 1  # still working; the placed notice is not sent again

    order = broker.orders[opened.json()["alpaca_order_id"]]
    order["status"] = "filled"
    order["filled_qty"] = "5"
    order["filled_avg_price"] = "0.90"
    order["filled_at"] = "2026-10-19T14:05:00Z"
    live.get("/flowvwap-monitor", headers=CRON)
    assert len(resend.calls) == 2
    filled = resend.messages()[1]
    assert "Fill price: 0.90" in filled["text"]
    assert "2026-10-19 10:05:00 ET" in filled["text"]
    live.get("/flowvwap-monitor", headers=CRON)
    assert len(resend.calls) == 2

    public.quotes["SPY"]["last"] = "580.40"
    clock["now"] = OPEN_TIME + timedelta(minutes=5)
    stopped = live.get("/flowvwap-monitor", headers=CRON)
    assert stopped.json()["actions"][0]["action"] == "stop"
    assert len(resend.calls) == 3
    exit_mail = resend.messages()[2]
    assert "Reason: stop" in exit_mail["text"]
    assert "P&L per contract: -40.00" in exit_mail["text"]
    assert "P&L total: -200.00" in exit_mail["text"]
    live.get("/flowvwap-monitor", headers=CRON)
    assert len(resend.calls) == 3
    _assert_no_secrets(_bodies(resend), caplog.text)


def test_timeout_and_flat_each_send_one_cancel_notice(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    caplog.set_level("INFO", logger="relay")
    resend = Resend()
    client, _public, _broker, clock = paper(tmp_path, notify=resend, **MAIL)
    assert client.post("/webhook", json=flow_alert(alert_id="age")).status_code == 200
    clock["now"] = OPEN_TIME + timedelta(seconds=300)
    timed = client.get("/flowvwap-monitor", headers=CRON)
    assert timed.json()["actions"][0]["reason"] == "limit_timeout"
    reasons = [message["text"] for message in resend.messages()]
    assert any("Reason: limit_timeout" in text for text in reasons)
    client.get("/flowvwap-monitor", headers=CRON)
    assert len(resend.calls) == 2  # placed + timeout, and the second tick adds nothing

    resend2 = Resend()
    client2, _public2, _broker2, _clock2 = paper(tmp_path / "flat", notify=resend2, **MAIL)
    assert client2.post("/webhook", json=flow_alert(alert_id="flat-mail")).status_code == 200
    flat = client2.post("/webhook", json=flow_alert(
        side="sell", market_position="flat", prev_market_position="long", alert_id="flat-mail-2", qty=1))
    assert flat.status_code == 200, flat.text
    assert any("Reason: flat" in message["text"] for message in resend2.messages())
    assert not any("Event: exit" in message["text"] for message in resend2.messages())
    _assert_no_secrets(_bodies(resend) + _bodies(resend2), caplog.text)


def test_a_mail_failure_does_not_fail_the_order_and_other_strategies_stay_quiet(tmp_path, caplog):
    caplog.set_level("INFO", logger="relay")
    resend = Resend(status=500)
    client, _public, broker, _clock = paper(tmp_path, notify=resend, **MAIL)
    opened = client.post("/webhook", json=flow_alert(alert_id="boom"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["order_placed"] is True
    assert broker.posts()
    assert "notify_failed" in caplog.text
    assert MAIL["RESEND_API_KEY"] not in caplog.text

    missing, _public2, broker2, _clock2 = paper(
        tmp_path / "nokey", NOTIFY_EMAIL_ENABLED="true", notify=Resend())
    assert missing.post("/webhook", json=flow_alert(alert_id="nokey")).status_code == 200
    assert missing.notify.calls == []
    assert "notify_not_configured" in caplog.text

    es_mail = Resend()
    es, _fake = make(
        tmp_path / "es", mode="PAPER", public=es_public(), alpaca=FakeAlpaca(),
        notify=es_mail, MAX_DAILY_OPENS="5000", **MAIL)
    assert es.post("/webhook", json=es_alert(alert_id="es-mail")).status_code == 200
    assert es_mail.calls == []
    assert scrub("limit re_test_key_not_real", ("re_test_key_not_real",)) == "limit [redacted]"
