"""PremarketES_MU: MU 0DTE, delta 0.125-0.15, ATM through 70 points OTM, qty 5.

The profile shares MAX_DAILY_OPENS and MAX_ORDERS_PER_DAY with the other strategies.
It does not set max_premium, so the global cap applies. A chain for a later expiry
is rejected instead of being used as that date.
"""
import json
import logging
import os
import pathlib
import sys
from datetime import date, datetime
from decimal import Decimal

os.environ["RELAY_NO_AUTOAPP"] = "1"

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "api"))
import index as relay  # noqa: E402
from test_index import (  # noqa: E402
    ET, CALLS, PUTS, ChainPublic, FakeAlpaca, MultiChainPublic,
    chain_for, chain_row, ending, greeks_for, make, occ, option_quote, qqq_alert, underlying,
)
from test_premarketes import es_alert, es_public  # noqa: E402

SPOT = Decimal("760")
# $5 strikes, matching the near-money MU 0DTE grid. 0.15 and 0.125 are equally far from
# the band middle (0.1375); the one closer to spot wins. 0.1375 sits 75 points OTM.
MU_CALLS = [
    ("755", "0.14", "1.40", "1.60"),    # 5 ITM, in band, outside the window
    ("760", "0.50", "2.20", "2.40"),     # ATM
    ("795", "0.30", "1.10", "1.30"),     # 35 OTM, seven $5 strikes, outside the band
    ("800", "0.15", "0.80", "1.00"),     # 40 OTM, chosen call
    ("830", "0.125", "0.40", "0.50"),    # 70 OTM, in band, farther from spot
    ("835", "0.1375", "0.20", "0.30"),   # 75 OTM, band middle, outside the window
]
MU_PUTS = [
    ("765", "-0.14", "1.20", "1.40"),    # ITM
    ("760", "-0.50", "2.00", "2.20"),
    ("720", "-0.15", "0.70", "0.90"),    # 40 OTM, chosen put
    ("690", "-0.125", "0.30", "0.40"),   # 70 OTM
    ("685", "-0.1375", "0.15", "0.25"),  # 75 OTM, outside the window
]
MU_CALL = occ("800", "C", "MU")
MU_PUT = occ("720", "P", "MU")
CALL_LIMIT = Decimal("0.90")  # mid of 0.80 / 1.00
CALL_NOTIONAL = CALL_LIMIT * 5 * 100
TUESDAY = datetime(2026, 10, 20, 10, 0, tzinfo=ET)


def mu_public(quotes=None, calls=MU_CALLS, puts=MU_PUTS, yymmdd="261019"):
    mu_chain = chain_for("MU", calls=calls, puts=puts, yymmdd=yymmdd)
    # A later-expiry contract with the same strike must not replace today's pick.
    mu_chain["calls"].append(chain_row(occ("800", "C", "MU", "261021"), "800", "0.1375", "0.10", "0.20"))
    chains = {
        "MU": mu_chain,
        "SPY": chain_for("SPY", calls=CALLS, puts=PUTS, yymmdd=yymmdd),
        "QQQ": chain_for("QQQ", calls=CALLS, puts=PUTS, yymmdd=yymmdd),
    }
    greeks = (
        greeks_for("MU", calls=calls, puts=puts, yymmdd=yymmdd)
        | greeks_for("SPY", calls=CALLS, puts=PUTS, yymmdd=yymmdd)
        | greeks_for("QQQ", calls=CALLS, puts=PUTS, yymmdd=yymmdd)
    )
    return MultiChainPublic(chains, greeks, quotes)


def mu_alert(**kw):
    base = underlying(
        symbol="MU", strategy="PremarketES_MU", underlying_price="760", qty=1, alert_id="mu-1")
    base.update(kw)
    return base


def test_profile_follows_the_global_premium_cap_and_leaves_spy_and_qqq_alone():
    defaults = relay.Settings.from_env({})
    assert defaults.allowlist == {"QQQ"}
    profile = defaults.for_strategy("PremarketES_MU")
    assert "MU" in profile.allowlist
    assert "MU" not in defaults.for_strategy("PremarketES").allowlist
    assert "MU" not in defaults.for_strategy("FlowVWAP").allowlist
    assert "MU" not in defaults.for_strategy("QQQ_EMA_VWAP").allowlist
    assert "max_premium" not in relay.BUILTIN_STRATEGY_OVERRIDE_RAW["PremarketES_MU"]
    assert profile.delta_band_min == Decimal("0.125") and profile.delta_band_max == Decimal("0.15")
    assert profile.itm_points == Decimal("0") and profile.otm_points == Decimal("70")
    assert profile.order_qty == 5 and profile.max_qty == 5
    assert profile.take_profit is False and profile.stop_loss_percent is None
    assert profile.flow_rules is False
    assert profile.max_premium == defaults.max_premium == Decimal("5.00")
    assert profile.max_orders_per_day == defaults.max_orders_per_day
    assert profile.max_daily_opens == defaults.max_daily_opens == Decimal("1500")
    assert profile.expiry_days == 0

    capped = relay.Settings.from_env({"MAX_PREMIUM": "4.00", "STOP_LOSS_PERCENT": "30", "TP_SINGLE": "true"})
    mu = capped.for_strategy("PremarketES_MU")
    assert mu.max_premium == Decimal("4.00")
    assert mu.take_profit is False and mu.stop_loss_percent is None
    assert mu.order_qty == 5 and mu.tp_single is True
    assert capped.for_strategy("PremarketES").otm_points == Decimal("5")
    assert capped.for_strategy("PremarketES").delta_band_min == Decimal("0.35")
    assert capped.for_strategy("FlowVWAP").max_premium is None and capped.for_strategy("FlowVWAP").flow_rules is True
    assert capped.for_strategy("QQQ_EMA_VWAP").max_qty == 1
    assert capped.for_strategy("QQQ_EMA_VWAP").delta_band_min == Decimal("0.60")
    assert capped.for_strategy("Tape") is capped

    assert relay.occ_symbol("MU", date(2026, 11, 16), "C", Decimal("822.5")) == "MU261116C00822500"
    parsed = relay.parse_occ("MU261116C00822500")
    assert parsed["underlying"] == "MU" and parsed["strike"] == Decimal("822.5")
    assert relay.in_window(Decimal("830"), SPOT, "C", Decimal("0"), Decimal("70"))
    assert not relay.in_window(Decimal("830.01"), SPOT, "C", Decimal("0"), Decimal("70"))
    assert relay.in_window(Decimal("760"), SPOT, "C", Decimal("0"), Decimal("70"))
    assert not relay.in_window(Decimal("759.99"), SPOT, "C", Decimal("0"), Decimal("70"))
    assert relay.in_window(Decimal("690"), SPOT, "P", Decimal("0"), Decimal("70"))
    assert not relay.in_window(Decimal("689.99"), SPOT, "P", Decimal("0"), Decimal("70"))
    # PremarketES is still five points, and the global window is still the ITM side.
    assert relay.in_window(Decimal("585"), Decimal("580"), "C", Decimal("0"), Decimal("5"))
    assert not relay.in_window(Decimal("585.01"), Decimal("580"), "C", Decimal("0"), Decimal("5"))
    assert relay.in_window(Decimal("575"), Decimal("580"), "C")

    widened = relay.Settings.from_env({
        "STRATEGY_OVERRIDES": '{"PremarketES_MU": {"otm_points": "80"}}',
    }).for_strategy("PremarketES_MU")
    assert widened.otm_points == Decimal("80") and widened.delta_band_min == Decimal("0.125")
    with pytest.raises(ValueError, match="otm_points"):
        relay.Settings.from_env({"STRATEGY_OVERRIDES": '{"PremarketES_MU": {"otm_points": "80.01"}}'})


def test_chain_picks_the_band_ignores_pm_log_and_shares_the_daily_pool(tmp_path, monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "cron-s")
    quotes = {MU_CALL: option_quote(MU_CALL, "0.70", "0.90"), MU_PUT: option_quote(MU_PUT, "0.60", "0.80")}
    client, _fake = make(tmp_path, public=mu_public(quotes), MAX_DAILY_OPENS="5000")
    denied = client.post("/webhook", json=underlying(
        symbol="MU", strategy="Tape", underlying_price="760", alert_id="mu-tape"))
    assert denied.status_code == 422 and "ALLOWLIST" in denied.json()["error"]
    opened = client.post("/webhook", json=mu_alert(pm_log="premarket note", qty=99))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["selection"]["right"] == "C"
    assert body["selection"]["chosen_symbol"] == MU_CALL
    assert body["selection"]["chosen_strike"] == "800"
    assert body["selection"]["expiry"] == "2026-10-19"
    assert Decimal(body["selection"]["delta"]) == Decimal("0.15")
    assert body["selection"]["underlying"] == "MU"
    strikes = {row["strike"] for row in body["selection"]["candidates"]}
    assert strikes == {"760", "795", "800", "830"}
    assert "755" not in strikes and "835" not in strikes
    assert body["order"]["orderSide"] == "BUY" and body["order"]["quantity"] == "5"
    assert Decimal(body["selection"]["limit"]) == CALL_LIMIT
    assert "pm_log" not in body["selection"]
    assert "regime" not in body["selection"]
    assert "target_underlying" not in body["selection"]
    assert body["take_profit"]["attached"] is False and body["take_profit"]["reason"] == relay.TAKE_PROFIT_OFF
    assert body["stop"]["attached"] is False
    assert body["stop"]["reason"] == "STOP_LOSS_PERCENT is not set"
    saved = client.sb.positions[("MU", "PremarketES_MU", "PREFLIGHT")]
    assert saved["qty"] == 5 and saved["status"] == "OPEN" and saved["symbol"] == MU_CALL
    assert client.sb.counters[("2026-10-19", "PREFLIGHT")] == [1, CALL_NOTIONAL]

    spy = client.post("/webhook", json=underlying(alert_id="spy-open"))
    assert spy.status_code == 200, spy.text
    assert spy.json()["selection"]["chosen_symbol"] == occ("579", "C")
    holding = client.get("/flowvwap-monitor", headers={"Authorization": "Bearer cron-s"})
    assert holding.status_code == 200, holding.text
    assert holding.json()["checked"] == 0 and holding.json()["actions"] == []
    assert client.sb.positions[("MU", "PremarketES_MU", "PREFLIGHT")]["status"] == "OPEN"

    flat = client.post("/webhook", json=mu_alert(
        side="sell", market_position="flat", prev_market_position="long",
        alert_id="mu-flat", qty=1, pm_log="flatten note"))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order"]["orderSide"] == "SELL" and flat.json()["order"]["quantity"] == "5"
    assert flat.json()["order"]["instrument"]["symbol"] == MU_CALL
    assert client.sb.positions[("MU", "PremarketES_MU", "PREFLIGHT")]["status"] == "CLOSED"
    assert client.sb.positions[("SPY", "Tape", "PREFLIGHT")]["status"] == "OPEN"

    short = client.post("/webhook", json=mu_alert(
        side="sell", market_position="short", prev_market_position="flat", alert_id="mu-put", qty=2))
    assert short.status_code == 200, short.text
    put = short.json()
    assert put["selection"]["right"] == "P" and put["selection"]["chosen_symbol"] == MU_PUT
    assert put["selection"]["chosen_strike"] == "720"
    assert Decimal(put["selection"]["delta"]) == Decimal("-0.15")
    assert put["order"]["quantity"] == "5"
    assert put["take_profit"]["attached"] is False and put["stop"]["attached"] is False

    health = client.get("/health").json()["settings"]
    assert health["daily_caps"] == "shared"
    listed = health["strategies"]["PremarketES_MU"]
    assert listed["delta_band_min"] == "0.125" and listed["delta_band_max"] == "0.15"
    assert listed["itm_points"] == "0" and listed["otm_points"] == "70"
    assert listed["max_premium"] == "5.00"
    assert listed["order_qty"] == 5 and listed["take_profit"] is False
    assert listed["stop_loss_percent"] is None and listed["flow_rules"] is False
    assert health["strategies"]["PremarketES"]["otm_points"] == "5"
    assert health["strategies"]["PremarketES"]["delta_band_min"] == "0.35"
    assert health["strategies"]["FlowVWAP"]["flow_rules"] is True
    assert health["strategies"]["QQQ_EMA_VWAP"]["max_qty"] == 1

    tight, _fake = make(tmp_path / "cap", public=mu_public(), MAX_DAILY_OPENS="500")
    spent = tight.post("/webhook", json=mu_alert(alert_id="mu-spend"))
    assert spent.status_code == 200, spent.text
    blocked = tight.post("/webhook", json=qqq_alert(alert_id="qqq-spend"))
    assert blocked.status_code == 422 and "spend" in blocked.json()["error"]
    assert ("QQQ", "QQQ_EMA_VWAP", "PREFLIGHT") not in tight.sb.positions
    assert tight.sb.counters[("2026-10-19", "PREFLIGHT")][0] == 1


def test_open_uses_the_global_four_dollar_cap(tmp_path):
    rich = [("800", "0.15", "4.40", "4.60")]
    chain = chain_for("MU", calls=rich, puts=[], yymmdd="261019")
    greeks = greeks_for("MU", calls=rich, puts=[], yymmdd="261019")
    client, fake = make(
        tmp_path, public=MultiChainPublic({"MU": chain}, greeks), MAX_PREMIUM="4.00", MAX_DAILY_OPENS="5000")
    rejected = client.post("/webhook", json=mu_alert(alert_id="rich"))
    assert rejected.status_code == 422, rejected.text
    assert "MAX_PREMIUM" in rejected.json()["error"] and "4.00" in rejected.json()["error"]
    assert rejected.json()["selection"]["chosen_symbol"] == MU_CALL
    assert rejected.json()["selection"]["limit"] == "4.50"
    assert ("MU", "PremarketES_MU", "PREFLIGHT") not in client.sb.positions
    assert not ending(fake, "/preflight/single-leg") and not ending(fake, "/order")


def test_missing_same_day_expiry_rejects_and_does_not_use_a_later_date(tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="relay")
    later = chain_for("MU", calls=MU_CALLS, puts=MU_PUTS, yymmdd="261021")
    later["calls"].append({
        "outcome": "SUCCESS",
        "bid": "0.80",
        "ask": "1.00",
        "optionDetails": {"strikePrice": "800", "greeks": {"delta": "0.15"}},
    })
    client, fake = make(
        tmp_path, public=ChainPublic(chain=later, greeks={}), clock=lambda: TUESDAY, MAX_DAILY_OPENS="5000")
    rejected = client.post("/webhook", json=mu_alert(alert_id="tuesday", pm_log="no expiry"))
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["error"] == "no MU call expiry on 2026-10-20; not using a later date"
    assert "MU261021" not in rejected.text and "MU261020" not in rejected.text
    requested = ending(fake, "/option-chain")
    assert len(requested) == 1
    assert json.loads(requested[0].content)["expirationDate"] == "2026-10-20"
    assert json.loads(requested[0].content)["instrument"]["symbol"] == "MU"
    assert not client.sb.positions and not client.sb.picks
    assert not ending(fake, "/preflight/single-leg") and not ending(fake, "/order")
    assert '"event": "expiry_unavailable"' in caplog.text
    assert "not using a later date" in caplog.text
    assert "2026-10-20" in caplog.text

    empty = {"baseSymbol": "MU", "calls": [], "puts": []}
    blank, blank_fake = make(
        tmp_path / "empty", public=ChainPublic(chain=empty, greeks={}),
        clock=lambda: TUESDAY, MAX_DAILY_OPENS="5000")
    missing = blank.post("/webhook", json=mu_alert(alert_id="empty-tuesday"))
    assert missing.status_code == 422, missing.text
    assert "no MU call strikes" in missing.json()["error"] and "2026-10-20" in missing.json()["error"]
    assert "MU261021" not in missing.text
    assert not blank.sb.positions and not ending(blank_fake, "/order")


def test_paper_flat_closes_the_mu_position(tmp_path):
    quotes = {MU_CALL: option_quote(MU_CALL, "0.70", "0.90")}
    broker = FakeAlpaca()
    client, public = make(
        tmp_path, mode="PAPER", public=mu_public(quotes), alpaca=broker,
        MAX_DAILY_OPENS="5000", STOP_LOSS_PERCENT="30", TP_PERCENT="50")
    opened = client.post("/webhook", json=mu_alert(alert_id="paper-open", pm_log="paper"))
    assert opened.status_code == 200, opened.text
    body = opened.json()
    assert body["order_placed"] is True and body["order"]["qty"] == "5"
    assert body["selection"]["chosen_symbol"] == MU_CALL
    assert body["take_profit"]["attached"] is False and body["take_profit"]["reason"] == relay.TAKE_PROFIT_OFF
    assert body["stop"]["attached"] is False
    assert body["stop"]["reason"] == "STOP_LOSS_PERCENT is not set"
    posts = broker.posts()
    assert len(posts) == 1
    assert posts[0]["symbol"] == MU_CALL
    assert posts[0]["position_intent"] == "buy_to_open" and posts[0]["qty"] == "5"
    saved = client.sb.positions[("MU", "PremarketES_MU", "PAPER")]
    assert saved["qty"] == 5 and saved["symbol"] == MU_CALL
    assert saved["tp_status"] == "NONE" and saved["stop_status"] == "NONE"
    assert client.sb.counters[("2026-10-19", "PAPER")][1] == CALL_NOTIONAL

    flat = client.post("/webhook", json=mu_alert(
        side="sell", market_position="flat", prev_market_position="long",
        alert_id="paper-flat", qty=1, pm_log="exit"))
    assert flat.status_code == 200, flat.text
    assert flat.json()["order_placed"] is True
    sells = [order for order in broker.posts() if order["side"] == "sell"]
    assert len(sells) == 1
    assert sells[0]["symbol"] == MU_CALL
    assert sells[0]["qty"] == "5" and sells[0]["position_intent"] == "sell_to_close"
    assert client.sb.positions[("MU", "PremarketES_MU", "PAPER")]["status"] == "CLOSED"
    assert not any(call.url.path.endswith("/order") for call in public.calls)


def test_paper_mu_entry_stops_at_1515_and_spy_premarketes_does_not(tmp_path):
    at_cutoff = datetime(2026, 10, 19, 15, 15, tzinfo=ET)
    broker = FakeAlpaca()
    client, public = make(
        tmp_path, mode="PAPER", public=mu_public(), alpaca=broker,
        clock=lambda: at_cutoff, MAX_DAILY_OPENS="5000")
    blocked = client.post("/webhook", json=mu_alert(alert_id="late-mu"))
    assert blocked.status_code == 422, blocked.text
    assert "15:15" in blocked.json()["error"] and "MU" in blocked.json()["error"]
    assert not ending(public, "/option-chain")
    assert broker.posts() == []

    spy_broker = FakeAlpaca()
    spy, _spy_public = make(
        tmp_path / "spy", mode="PAPER", public=es_public(), alpaca=spy_broker,
        clock=lambda: at_cutoff, MAX_DAILY_OPENS="5000")
    opened = spy.post("/webhook", json=es_alert(alert_id="spy-1515"))
    assert opened.status_code == 200, opened.text
    assert opened.json()["selection"]["chosen_strike"] == "581"
    assert opened.json()["selection"]["delta"] == "0.36"
    assert spy_broker.posts()[0]["symbol"] == occ("581", "C")
    assert spy.sb.positions[("SPY", "PremarketES", "PAPER")]["status"] == "OPEN"
