import json
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from quant_engine.risk import (
    AccountSnapshot, Action, EntryRequest, ExitRequest, FileStateStore, InMemoryStateStore, InvalidRequest,
    Reason, StateUnavailable,
)
from quant_engine.risk.engine import ENGINE_ACTOR
from quant_engine.tests.support import NOW, Clock, make_engine, option_entry, snapshot, stock_entry

D = Decimal
RISK_REASONS = {Reason.KILL_SWITCH, Reason.DAILY_LOSS_LIMIT, Reason.WEEKLY_LOSS_LIMIT,
                Reason.AGGREGATE_RISK_LIMIT, Reason.GROSS_EXPOSURE_LIMIT, Reason.TRADE_RISK_LIMIT,
                Reason.STATE_UNAVAILABLE}


# ---------------------------------------------------------------- normal operation
def test_option_entry_is_sized_to_the_per_trade_budget():
    engine = make_engine()
    decision = engine.evaluate_entry(option_entry(requested_qty=5), snapshot())
    assert decision.approved and decision.action == Action.ENTRY
    assert decision.qty == 2
    assert decision.planned_risk == D("240.00")
    assert decision.detail["reduced_from"] == 5
    assert decision.detail["binding_limits"] == ["TRADE_RISK_LIMIT"]
    assert engine.status()["open_risk"] == "240.00"


def test_stock_entry_is_sized_by_stop_distance():
    decision = make_engine().evaluate_entry(stock_entry(), snapshot())
    assert decision.approved and decision.qty == 250 and decision.planned_risk == D("250")


def test_requested_quantity_below_the_budget_is_kept():
    decision = make_engine().evaluate_entry(option_entry(requested_qty=1), snapshot())
    assert decision.qty == 1 and decision.detail["binding_limits"] == ["REQUESTED_QTY"]
    assert decision.detail["reduced_from"] is None


def test_string_inputs_from_a_webhook_are_accepted():
    request = EntryRequest(request_id="SPY-1-2026-11-24T15:00:00Z", strategy="FlowVWAP", symbol="spy",
                           side=" long ", entry_price="1.20", requested_qty="1", multiplier="100", mode="paper")
    decision = make_engine().evaluate_entry(request, snapshot(equity="100000.00"))
    assert decision.approved and decision.qty == 1
    assert make_engine().evaluate_entry(option_entry(requested_qty="2.0"), snapshot()).qty == 2


def test_preflight_mode_entries_are_evaluated_like_paper():
    assert make_engine().evaluate_entry(option_entry(mode="PREFLIGHT"), snapshot()).approved


def test_duplicate_request_returns_the_original_approval_and_reserves_once():
    engine = make_engine()
    first = engine.evaluate_entry(option_entry("dup", requested_qty=1), snapshot())
    again = engine.evaluate_entry(option_entry("dup", requested_qty=2), snapshot())
    assert first.approved and again.approved
    assert again.qty == 1 and again.detail["duplicate"] is True
    assert engine.status()["open_risk"] == "120.00"


def test_confirm_fill_opens_the_position_at_the_filled_size():
    engine = make_engine()
    engine.evaluate_entry(option_entry("f1"), snapshot())
    result = engine.confirm_entry_fill("f1", 1)
    assert result == {"position_id": "f1", "status": "OPEN", "qty": 1, "planned_risk": "120.00",
                      "approved_risk": "240.00", "risk_overrun": False, "fill_beyond_stop": False}
    assert engine.status()["positions"]["f1"]["status"] == "OPEN"


def test_zero_fill_releases_the_reservation():
    engine = make_engine()
    engine.evaluate_entry(option_entry("f0"), snapshot())
    assert engine.confirm_entry_fill("f0", 0)["status"] == "RELEASED"
    assert engine.status()["open_risk"] == "0.00"


def test_worse_fill_price_raises_recorded_risk_and_is_reported():
    engine = make_engine()
    engine.evaluate_entry(option_entry("slip"), snapshot())
    result = engine.confirm_entry_fill("slip", 2, fill_price="1.30")
    assert result["risk_overrun"] is True and result["planned_risk"] == "260.00"
    assert engine.status()["open_risk"] == "260.00"


def test_fill_through_the_stop_keeps_the_approved_risk_and_is_flagged():
    engine = make_engine()
    engine.evaluate_entry(stock_entry("gap", requested_qty=10), snapshot())
    result = engine.confirm_entry_fill("gap", 10, fill_price="198.50")
    assert result["fill_beyond_stop"] is True and result["planned_risk"] == "10.00"


@pytest.mark.parametrize("filled", [3, -1, "abc", True, 1.5])
def test_confirm_fill_rejects_bad_quantities(filled):
    engine = make_engine()
    engine.evaluate_entry(option_entry("bad"), snapshot())
    with pytest.raises(InvalidRequest):
        engine.confirm_entry_fill("bad", filled)


def test_confirm_fill_for_unknown_reservation_raises():
    with pytest.raises(KeyError):
        make_engine().confirm_entry_fill("nope", 1)


def test_release_drops_only_pending_reservations():
    engine = make_engine()
    engine.evaluate_entry(option_entry("p1"), snapshot())
    engine.evaluate_entry(option_entry("p2", requested_qty=1), snapshot())
    engine.confirm_entry_fill("p2", 1)
    assert engine.release_entry("p1") is True
    assert engine.release_entry("p2") is False
    assert engine.release_entry("missing") is False
    assert engine.status()["open_risk"] == "120.00"


def test_exit_fill_frees_headroom_for_new_entries():
    engine = make_engine()
    engine.evaluate_entry(option_entry("a"), snapshot())
    engine.evaluate_entry(option_entry("b"), snapshot())
    assert not engine.evaluate_entry(option_entry("c"), snapshot()).approved
    assert engine.record_exit_fill("a", 1)["remaining"] == 1
    assert engine.record_exit_fill("a", 5)["remaining"] == 0
    assert "a" not in engine.status()["positions"]
    assert engine.evaluate_entry(option_entry("c"), snapshot()).qty == 2


def test_exit_fill_for_an_untracked_position_is_a_no_op():
    assert make_engine().record_exit_fill("ghost", 1) == {"position_id": "ghost", "tracked": False, "remaining": None}


# ---------------------------------------------------------------- invalid data
GOOD = {"request_id": "x1", "strategy": "S", "symbol": "SPY", "side": "LONG", "entry_price": "1.20",
        "multiplier": 100, "mode": "PAPER"}


@pytest.mark.parametrize("field,value", [
    ("request_id", ""), ("request_id", None), ("request_id", 123), ("request_id", "has space"),
    ("request_id", "x" * 129),
    ("strategy", ""), ("strategy", None), ("symbol", "SPY$"), ("symbol", 5),
    ("side", "BUY"), ("side", ""), ("side", None), ("side", 1),
    ("entry_price", "0"), ("entry_price", "-1"), ("entry_price", "abc"), ("entry_price", "NaN"),
    ("entry_price", "inf"), ("entry_price", None), ("entry_price", True), ("entry_price", []),
    ("stop_price", "1.20"), ("stop_price", "2"), ("stop_price", "-1"), ("stop_price", "abc"),
    ("requested_qty", 0), ("requested_qty", -1), ("requested_qty", 1.5), ("requested_qty", "abc"),
    ("requested_qty", True), ("requested_qty", 10**7),
    ("multiplier", 0), ("multiplier", 1.5), ("multiplier", True), ("multiplier", 10_001),
    ("cost_per_unit", "-0.01"), ("cost_per_unit", "abc"), ("cost_per_unit", None),
    ("mode", "LIVEX"), ("mode", None),
])
def test_invalid_entry_fields_are_rejected_without_touching_state(field, value):
    engine = make_engine()
    decision = engine.evaluate_entry(EntryRequest(**{**GOOD, field: value}), snapshot())
    assert not decision.approved and decision.reasons == (Reason.INVALID_REQUEST,)
    assert decision.qty == 0
    assert engine.status()["positions"] == {}


def test_short_without_a_stop_is_rejected_as_unbounded():
    decision = make_engine().evaluate_entry(EntryRequest(**{**GOOD, "side": "SHORT"}), snapshot())
    assert decision.reasons == (Reason.INVALID_REQUEST,) and "unbounded" in decision.messages[0]


def test_short_with_stop_below_entry_is_rejected():
    request = EntryRequest(**{**GOOD, "side": "SHORT", "stop_price": "1.00"})
    assert make_engine().evaluate_entry(request, snapshot()).reasons == (Reason.INVALID_REQUEST,)


def test_non_request_object_is_rejected():
    decision = make_engine().evaluate_entry({"request_id": "x"}, snapshot())  # type: ignore[arg-type]
    assert decision.reasons == (Reason.INVALID_REQUEST,) and decision.request_id == ""


@pytest.mark.parametrize("kwargs", [
    {"equity": "0"}, {"equity": "-1"}, {"equity": "abc"}, {"equity": "NaN"}, {"equity": None},
    {"as_of": datetime(2026, 11, 24, 10, 0)}, {"as_of": "2026-11-24T15:00:00Z"},
    {"as_of": NOW + timedelta(minutes=1)}, {"day_start_equity": "-5"}, {"week_start_equity": "abc"},
])
def test_invalid_snapshot_is_rejected(kwargs):
    fields = {"equity": "100000", "as_of": NOW, **kwargs}
    decision = make_engine().evaluate_entry(option_entry(), AccountSnapshot(**fields))
    assert decision.reasons == (Reason.INVALID_SNAPSHOT,)


def test_none_as_of_is_rejected():
    decision = make_engine().evaluate_entry(option_entry(), AccountSnapshot(equity="1", as_of=None))
    assert decision.reasons == (Reason.INVALID_SNAPSHOT,)


def test_stale_snapshot_is_rejected_and_the_age_limit_is_inclusive():
    engine = make_engine()
    assert engine.evaluate_entry(option_entry("ok"), snapshot(as_of=NOW - timedelta(seconds=120))).approved
    stale = engine.evaluate_entry(option_entry("old"), snapshot(as_of=NOW - timedelta(seconds=121)))
    assert stale.reasons == (Reason.STALE_SNAPSHOT,)


def test_small_clock_skew_is_tolerated():
    assert make_engine().evaluate_entry(option_entry(), snapshot(as_of=NOW + timedelta(seconds=3))).approved


def test_live_entries_are_never_approved():
    engine = make_engine()
    decision = engine.evaluate_entry(option_entry(mode="LIVE"), snapshot())
    assert decision.reasons == (Reason.LIVE_TRADING_DISABLED,)
    assert engine.status()["positions"] == {}


# ---------------------------------------------------------------- limit breaches
def test_budget_below_one_unit_is_a_trade_risk_rejection():
    decision = make_engine().evaluate_entry(option_entry(entry_price="2.60"), snapshot())
    assert not decision.approved and Reason.TRADE_RISK_LIMIT in decision.reasons
    assert "one unit risks 260.00" in decision.messages[0]


def test_aggregate_open_risk_caps_concurrent_positions():
    engine = make_engine()
    assert engine.evaluate_entry(option_entry("a"), snapshot()).qty == 2
    second = engine.evaluate_entry(option_entry("b"), snapshot())
    assert second.qty == 2 and "AGGREGATE_RISK_LIMIT" in second.detail["binding_limits"]
    third = engine.evaluate_entry(option_entry("c"), snapshot())
    assert not third.approved and third.reasons == (Reason.AGGREGATE_RISK_LIMIT,)
    assert engine.status()["open_risk"] == "480.00"


def test_aggregate_headroom_shrinks_the_size_instead_of_rejecting():
    engine = make_engine()
    engine.evaluate_entry(stock_entry("s1"), snapshot())  # 250 of the 500 aggregate
    engine.evaluate_entry(stock_entry("s2", requested_qty=100), snapshot())  # 350
    third = engine.evaluate_entry(stock_entry("s3", entry_price="50", stop_price="49"), snapshot())
    assert third.qty == 150 and third.detail["binding_limits"] == ["AGGREGATE_RISK_LIMIT"]


def test_daily_loss_breach_latches_for_the_rest_of_the_day():
    clock = Clock()
    engine = make_engine(clock=clock)
    assert engine.evaluate_entry(option_entry("open"), snapshot(clock=clock)).approved
    engine.record_exit_fill("open", 2)
    breach = engine.evaluate_entry(option_entry("b1"), snapshot("99250", clock=clock))
    assert breach.reasons == (Reason.DAILY_LOSS_LIMIT,)
    assert breach.detail["day_loss"] == "750.00" and breach.detail["day_loss_limit"] == "750.00"
    clock.advance(hours=2)
    recovered = engine.evaluate_entry(option_entry("b2"), snapshot("100500", clock=clock))
    assert recovered.reasons == (Reason.DAILY_LOSS_LIMIT,)
    assert engine.status()["day_locked"] is True and engine.status()["entries_allowed"] is False


def test_daily_lockout_clears_on_the_next_new_york_day():
    clock = Clock()
    engine = make_engine(clock=clock)
    engine.evaluate_entry(option_entry("seed", requested_qty=1), snapshot(clock=clock))
    engine.release_entry("seed")
    assert not engine.evaluate_entry(option_entry("b1"), snapshot("99000", clock=clock)).approved
    clock.advance(hours=14)  # 00:00 ET Wednesday
    nxt = engine.evaluate_entry(option_entry("b2"), snapshot("99000", clock=clock))
    assert nxt.approved and nxt.detail["day"] == "2026-11-25"
    assert nxt.detail["day_start_equity"] == "99000.00"


def test_new_york_day_boundary_is_not_utc_midnight():
    clock = Clock()
    engine = make_engine(clock=clock)
    engine.evaluate_entry(option_entry("seed", requested_qty=1), snapshot(clock=clock))
    engine.release_entry("seed")
    assert not engine.evaluate_entry(option_entry("b1"), snapshot("99000", clock=clock)).approved
    clock.advance(hours=10)  # 01:00 UTC Wednesday is still 20:00 ET Tuesday
    assert engine.evaluate_entry(option_entry("b2"), snapshot("100000", clock=clock)).reasons == (Reason.DAILY_LOSS_LIMIT,)


def test_worst_case_daily_headroom_reduces_the_size():
    clock = Clock()
    engine = make_engine(clock=clock)
    engine.evaluate_entry(option_entry("seed", requested_qty=1), snapshot(clock=clock))
    engine.release_entry("seed")
    decision = engine.evaluate_entry(option_entry("d1"), snapshot("99400", clock=clock))
    assert decision.qty == 1 and decision.detail["binding_limits"] == ["DAILY_LOSS_LIMIT"]
    assert decision.detail["budgets"]["DAILY_LOSS_LIMIT"] == "150.00"


def test_worst_case_daily_headroom_counts_open_risk():
    clock = Clock()
    engine = make_engine(clock=clock)
    engine.evaluate_entry(option_entry("o1", requested_qty=1), snapshot(clock=clock))  # 120 open
    decision = engine.evaluate_entry(option_entry("d1"), snapshot("99400", clock=clock))
    assert not decision.approved and Reason.DAILY_LOSS_LIMIT in decision.reasons
    assert engine.status()["day_locked"] is False


def test_supplied_day_start_equity_is_used_on_first_sight():
    decision = make_engine().evaluate_entry(
        option_entry(), snapshot("99300", day_start_equity="100000", week_start_equity="100000"))
    assert not decision.approved and Reason.DAILY_LOSS_LIMIT in decision.reasons
    assert decision.detail["day_start_equity"] == "100000.00"


def test_the_higher_baseline_wins_so_losses_are_not_understated():
    engine = make_engine()
    engine.evaluate_entry(option_entry("a", requested_qty=1), snapshot("100000"))
    decision = engine.evaluate_entry(option_entry("b", requested_qty=1), snapshot("99500", day_start_equity="99000"))
    assert decision.detail["day_start_equity"] == "100000.00" and decision.detail["day_loss"] == "500.00"


def test_weekly_breach_engages_the_kill_switch_until_a_person_clears_it():
    clock = Clock()
    engine = make_engine(clock=clock)
    engine.evaluate_entry(option_entry("seed", requested_qty=1), snapshot(clock=clock))
    engine.release_entry("seed")
    clock.advance(days=1)
    breach = engine.evaluate_entry(option_entry("w1"), snapshot("98000", clock=clock))
    assert breach.reasons == (Reason.WEEKLY_LOSS_LIMIT,)
    status = engine.status()
    assert status["kill_switch"]["engaged"] is True and status["kill_switch"]["actor"] == ENGINE_ACTOR
    clock.advance(days=5)  # Monday 2026-11-30
    assert engine.evaluate_entry(option_entry("w2"), snapshot("98000", clock=clock)).reasons == (Reason.KILL_SWITCH,)
    with pytest.raises(ValueError):
        engine.disengage_kill_switch("try", ENGINE_ACTOR)
    engine.disengage_kill_switch("reviewed weekly loss", "nick")
    resumed = engine.evaluate_entry(option_entry("w3"), snapshot("98000", clock=clock))
    assert resumed.approved and resumed.detail["week"] == "2026-W49"


def test_weekly_lockout_without_auto_kill_switch_clears_next_week():
    clock = Clock()
    engine = make_engine(clock=clock, kill_switch_on_weekly_breach=False)
    engine.evaluate_entry(option_entry("seed", requested_qty=1), snapshot(clock=clock))
    engine.release_entry("seed")
    clock.advance(days=1)
    assert engine.evaluate_entry(option_entry("w1"), snapshot("98000", clock=clock)).reasons == (Reason.WEEKLY_LOSS_LIMIT,)
    clock.advance(days=1)
    assert engine.evaluate_entry(option_entry("w2"), snapshot("99000", clock=clock)).reasons == (Reason.WEEKLY_LOSS_LIMIT,)
    assert engine.status()["kill_switch"]["engaged"] is False
    clock.advance(days=5)
    assert engine.evaluate_entry(option_entry("w3"), snapshot("98000", clock=clock)).approved


def test_gross_exposure_limit_caps_notional():
    decision = make_engine(max_gross_exposure_pct="1").evaluate_entry(stock_entry(), snapshot())
    assert decision.qty == 5 and decision.detail["binding_limits"] == ["GROSS_EXPOSURE_LIMIT"]


def test_every_blocking_limit_is_named():
    engine = make_engine(max_gross_exposure_pct="0.1")
    decision = engine.evaluate_entry(option_entry(entry_price="2.60"), snapshot())
    assert set(decision.reasons) == {Reason.TRADE_RISK_LIMIT, Reason.GROSS_EXPOSURE_LIMIT}
    assert len(decision.messages) == 2


def test_custom_limits_apply():
    engine = make_engine(max_trade_risk_pct="0.5", max_open_risk_pct="1.0")
    assert engine.evaluate_entry(option_entry(), snapshot()).qty == 4


# ---------------------------------------------------------------- kill switch
def test_kill_switch_blocks_entries_and_records_history():
    engine = make_engine()
    engine.engage_kill_switch("broker outage", "nick")
    decision = engine.evaluate_entry(option_entry(), snapshot())
    assert decision.reasons == (Reason.KILL_SWITCH,) and "broker outage" in decision.messages[0]
    engine.disengage_kill_switch("broker back", "nick")
    assert engine.evaluate_entry(option_entry(), snapshot()).approved
    history = engine.store.read().kill_switch_history
    assert [h["engaged"] for h in history] == [True, False]


@pytest.mark.parametrize("reason,actor", [("", "nick"), ("why", ""), ("  ", "nick"), (None, "nick")])
def test_kill_switch_changes_need_reason_and_actor(reason, actor):
    with pytest.raises(ValueError):
        make_engine().engage_kill_switch(reason, actor)


def test_kill_switch_survives_a_restart(tmp_path):
    path = tmp_path / "risk.json"
    make_engine(store=FileStateStore(path)).engage_kill_switch("end of week review", "nick")
    restarted = make_engine(store=FileStateStore(path))
    assert restarted.status()["kill_switch"]["engaged"] is True
    assert restarted.evaluate_entry(option_entry(), snapshot()).reasons == (Reason.KILL_SWITCH,)


def test_positions_and_lockouts_survive_a_restart(tmp_path):
    path = tmp_path / "risk.json"
    first = make_engine(store=FileStateStore(path))
    first.evaluate_entry(option_entry("keep"), snapshot())
    first.evaluate_entry(option_entry("lock"), snapshot("99000"))
    second = make_engine(store=FileStateStore(path))
    status = second.status()
    assert status["open_risk"] == "240.00" and status["day_locked"] is True


def test_kill_switch_history_is_bounded():
    engine = make_engine()
    for n in range(260):
        engine.engage_kill_switch(f"r{n}", "nick")
    assert len(engine.store.read().kill_switch_history) == 200


# ---------------------------------------------------------------- protective exits
def _open_position(engine, position_id="held"):
    assert engine.evaluate_entry(option_entry(position_id), snapshot()).approved
    engine.confirm_entry_fill(position_id, 2)


def _assert_exit_approved(decision, qty):
    assert decision.approved and decision.action == Action.EXIT
    assert decision.qty == qty
    assert not set(decision.reasons) & RISK_REASONS


def test_exit_is_approved_with_the_kill_switch_engaged():
    engine = make_engine()
    _open_position(engine)
    engine.engage_kill_switch("halt", "nick")
    _assert_exit_approved(engine.evaluate_exit(ExitRequest("held")), 2)


def test_exit_is_approved_during_a_daily_lockout():
    engine = make_engine()
    _open_position(engine)
    engine.evaluate_entry(option_entry("x"), snapshot("99000"))
    assert engine.status()["day_locked"]
    _assert_exit_approved(engine.evaluate_exit(ExitRequest("held", qty=1)), 1)


def test_exit_is_approved_during_a_weekly_lockout():
    engine = make_engine()
    _open_position(engine)
    engine.evaluate_entry(option_entry("x"), snapshot("97000"))
    assert engine.status()["week_locked"] and engine.status()["kill_switch"]["engaged"]
    _assert_exit_approved(engine.evaluate_exit(ExitRequest("held")), 2)


def test_exit_is_approved_with_aggregate_risk_full():
    engine = make_engine()
    _open_position(engine, "a")
    _open_position(engine, "b")
    assert not engine.evaluate_entry(option_entry("c"), snapshot()).approved
    _assert_exit_approved(engine.evaluate_exit(ExitRequest("a")), 2)


def test_exit_is_approved_when_state_is_corrupt():
    store = InMemoryStateStore()
    engine = make_engine(store=store)
    _open_position(engine)
    store.corrupt("{not json")
    decision = engine.evaluate_exit(ExitRequest("held", qty=2))
    _assert_exit_approved(decision, 2)
    assert decision.detail["state_unavailable"] is True


def test_exit_is_approved_when_state_file_is_corrupt(tmp_path):
    path = tmp_path / "risk.json"
    engine = make_engine(store=FileStateStore(path))
    _open_position(engine)
    path.write_text("garbage", encoding="utf-8")
    _assert_exit_approved(engine.evaluate_exit(ExitRequest("held")), None)
    assert engine.evaluate_entry(option_entry("new"), snapshot()).reasons == (Reason.STATE_UNAVAILABLE,)


def test_exit_for_an_untracked_position_is_approved():
    decision = make_engine().evaluate_exit(ExitRequest("broker-only", qty=3))
    _assert_exit_approved(decision, 3)
    assert decision.detail["untracked"] is True


def test_exit_cannot_sell_more_than_is_held():
    engine = make_engine()
    _open_position(engine)
    decision = engine.evaluate_exit(ExitRequest("held", qty=10))
    _assert_exit_approved(decision, 2)
    assert decision.detail["clamped"] is True


def test_exit_of_a_pending_entry_is_approved():
    engine = make_engine()
    engine.evaluate_entry(option_entry("pending"), snapshot())
    _assert_exit_approved(engine.evaluate_exit(ExitRequest("pending")), 2)


def test_evaluating_an_exit_does_not_change_state():
    engine = make_engine()
    _open_position(engine)
    before = engine.store.read().to_json()
    for _ in range(5):
        engine.evaluate_exit(ExitRequest("held"))
    assert engine.store.read().to_json() == before


@pytest.mark.parametrize("kwargs", [
    {"position_id": ""}, {"position_id": None}, {"position_id": "bad id"},
    {"position_id": "held", "qty": 0}, {"position_id": "held", "qty": -1},
    {"position_id": "held", "qty": "abc"}, {"position_id": "held", "qty": 1.5},
])
def test_malformed_exit_is_rejected_only_as_invalid(kwargs):
    engine = make_engine()
    _open_position(engine)
    engine.engage_kill_switch("halt", "nick")
    decision = engine.evaluate_exit(ExitRequest(**kwargs))
    assert not decision.approved and decision.action == Action.EXIT
    assert decision.reasons == (Reason.INVALID_REQUEST,)


def test_non_request_exit_is_rejected_as_invalid():
    decision = make_engine().evaluate_exit("held")  # type: ignore[arg-type]
    assert decision.reasons == (Reason.INVALID_REQUEST,)


# ---------------------------------------------------------------- state store failures
def test_corrupt_state_fails_entries_closed():
    store = InMemoryStateStore()
    engine = make_engine(store=store)
    store.corrupt("[]")
    assert engine.evaluate_entry(option_entry(), snapshot()).reasons == (Reason.STATE_UNAVAILABLE,)
    status = engine.status()
    assert status["ok"] is False and status["entries_allowed"] is False


@pytest.mark.parametrize("mutate", [
    lambda s: s.update(version=99),
    lambda s: s["kill_switch"].update(engaged="no"),
    lambda s: s.update(positions=[]),
    lambda s: s["positions"]["p"].update(risk_per_unit="-1"),
    lambda s: s["positions"]["p"].update(qty=0),
    lambda s: s["positions"]["p"].update(status="WEIRD"),
    lambda s: s["positions"]["p"].pop("symbol"),
    lambda s: s["positions"].update(other=s["positions"]["p"]),
    lambda s: s["baselines"].update(day={"period": "2026-11-24", "equity": "NaN"}),
    lambda s: s["lockouts"].update(day=5),
])
def test_tampered_state_is_unavailable(mutate):
    engine = make_engine()
    engine.evaluate_entry(option_entry("p"), snapshot())
    data = json.loads(engine.store.read().to_json())
    mutate(data)
    store = InMemoryStateStore(json.dumps(data))
    with pytest.raises(StateUnavailable):
        store.read()
    assert make_engine(store=store).evaluate_entry(option_entry("q"), snapshot()).reasons == (Reason.STATE_UNAVAILABLE,)


def test_missing_state_file_starts_fresh(tmp_path):
    engine = make_engine(store=FileStateStore(tmp_path / "nested" / "risk.json"))
    assert engine.status()["kill_switch"]["engaged"] is False
    assert engine.evaluate_entry(option_entry(), snapshot()).approved
    assert (tmp_path / "nested" / "risk.json").exists()


def test_failed_write_keeps_the_previous_state_and_rejects_the_entry(tmp_path, monkeypatch):
    path = tmp_path / "risk.json"
    engine = make_engine(store=FileStateStore(path))
    engine.evaluate_entry(option_entry("first", requested_qty=1), snapshot())
    before = path.read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("quant_engine.risk.state.os.replace", boom)
    decision = engine.evaluate_entry(option_entry("second", requested_qty=1), snapshot())
    assert decision.reasons == (Reason.STATE_UNAVAILABLE,)
    monkeypatch.undo()
    assert path.read_text(encoding="utf-8") == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["risk.json", "risk.json.lock"]
    assert set(engine.status()["positions"]) == {"first"}


def test_writes_leave_no_temp_files(tmp_path):
    engine = make_engine(store=FileStateStore(tmp_path / "risk.json"))
    for n in range(5):
        engine.evaluate_entry(option_entry(f"e{n}", requested_qty=1), snapshot())
    assert sorted(p.name for p in tmp_path.iterdir()) == ["risk.json", "risk.json.lock"]
