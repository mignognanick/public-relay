import pathlib
import sys
from datetime import datetime

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import backtest  # noqa: E402
import pricing  # noqa: E402
from strategies import Bar  # noqa: E402

DAY = "2026-10-20"


def fake_mark(symbol, day, ts, spot, strike, right):
    intrinsic = max(0.0, spot - strike) if right == "call" else max(0.0, strike - spot)
    return intrinsic + 1.0


def fake_delta(symbol, day, ts, spot, strike, right):
    d = min(0.95, max(0.05, 0.5 + 0.03 * (spot - strike)))
    return d if right == "call" else d - 1.0


@pytest.fixture(autouse=True)
def flat_smile(monkeypatch):
    monkeypatch.setattr(pricing, "mark", fake_mark)
    monkeypatch.setattr(pricing, "delta", fake_delta)
    monkeypatch.setattr(pricing, "smiles", lambda symbol, day: ((None, None),))


def bar(hhmm, c, h=None, l=None):
    hour, minute = map(int, hhmm.split(":"))
    end = datetime(2026, 10, 20, hour, minute)
    return Bar(DAY, end, c, h if h is not None else c, l if l is not None else c, c, 1)


def run(bars, targets, **cfg):
    return backtest.simulate_day("SPY", DAY, bars, targets, backtest.ContractCfg(**cfg))


def test_choose_strike_picks_in_band_itm_call():
    strike, d = backtest.choose_strike("SPY", DAY, datetime(2026, 10, 20, 10), 100.0, 1, backtest.ContractCfg())
    assert strike == 98.0
    assert 0.50 <= d <= 0.60


def test_choose_strike_reaches_otm_only_with_otm_window():
    cfg = backtest.ContractCfg(band_min=0.30, band_max=0.40, otm_points=5.0)
    strike, d = backtest.choose_strike("SPY", DAY, datetime(2026, 10, 20, 10), 100.0, 1, cfg)
    assert strike > 100.0 and 0.30 <= d <= 0.40
    no_otm = backtest.ContractCfg(band_min=0.30, band_max=0.40)
    strike, _ = backtest.choose_strike("SPY", DAY, datetime(2026, 10, 20, 10), 100.0, 1, no_otm)
    assert strike == 100.0


def test_take_profit_fills_at_limit_on_a_later_bar():
    trades = run([bar("10:00", 100), bar("10:05", 101, h=101), bar("10:10", 100.5)], [1, 1, 1], tp=0.20)
    assert len(trades) == 1
    t = trades[0]
    assert t.reason == "take_profit"
    assert t.exit == round(t.limit * 1.2, 2)
    assert t.exit_time == "10:05"


def test_take_profit_needs_half_spread_through_the_limit():
    trades = run([bar("10:00", 100), bar("10:05", 100.6), bar("15:40", 100.6)], [1, 1, 1], tp=0.20)
    assert trades[0].reason == "flatten"


def test_stop_beats_take_profit_in_same_bar():
    trades = run([bar("10:00", 100), bar("10:05", 100, h=103, l=98), bar("15:40", 100)], [1, 1, 1],
                 tp=0.20, stop=0.30)
    assert trades[0].reason == "stop"
    assert trades[0].exit < trades[0].entry


def test_reversal_closes_then_enters_other_side_next_bar():
    bars = [bar("10:00", 100), bar("10:05", 99), bar("10:10", 98), bar("15:40", 98)]
    trades = run(bars, [1, -1, -1, -1], tp=None)
    assert [(t.side, t.reason, t.exit_time) for t in trades] == [(1, "signal", "10:05"), (-1, "flatten", "15:40")]


def test_flatten_at_1540_and_no_entries_after_cutoff():
    bars = [bar("15:30", 100), bar("15:35", 100), bar("15:40", 100)]
    assert run(bars, [1, 1, 1], tp=None) == []
    assert backtest.simulate_day("IWM", DAY, [bar("15:20", 100), bar("15:40", 100)], [1, 1],
                                 backtest.ContractCfg(tp=None)) == []


def test_three_entries_per_day():
    bars = [bar(f"10:{m:02d}", 100) for m in range(0, 50, 5)] + [bar("15:40", 100)]
    targets = [1, 0, 1, 0, 1, 0, 1, 0, 1, 0, 0]
    assert len(run(bars, targets, tp=None)) == 3


def test_relay_stays_flat_after_take_profit_until_next_entry():
    bars = [bar("10:00", 100), bar("10:05", 101), bar("10:10", 101), bar("10:15", 101), bar("15:40", 101)]
    trades = run(bars, [1, 1, 1, 1, 1], tp=0.20)
    assert len(trades) == 1 and trades[0].reason == "take_profit"


def test_premium_cap_removes_opens_above_the_limit():
    trades = run([bar("10:00", 100), bar("15:40", 100)], [1, 1], tp=None)
    assert backtest.apply_cap(trades, 5.0) == trades
    assert backtest.apply_cap(trades, 1.0) == []


def test_metrics_drawdown_and_profit_factor():
    a = backtest.Trade(DAY, 1, 100, 0.5, "10:00", 1.0, 1.0, "10:05", 1.5, "signal")
    b = backtest.Trade(DAY, 1, 100, 0.5, "10:10", 1.0, 1.0, "10:15", 0.5, "signal")
    m = backtest.metrics([a, b, b], 1)
    assert m["total_pnl"] == -50.0
    assert m["max_drawdown"] == 100.0
    assert m["profit_factor"] == 0.5
