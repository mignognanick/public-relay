from decimal import Decimal

import pytest

from quant_engine.risk import Side, size_position
from quant_engine.risk.sizing import risk_per_unit, units_within

D = Decimal


def test_long_option_without_stop_risks_the_whole_premium():
    assert risk_per_unit(Side.LONG, D("1.20"), None, 100) == D("120.00")
    result = size_position(D("100000"), D("0.25"), Side.LONG, D("1.20"), None, 100)
    assert result.trade_budget == D("250")
    assert result.qty == 2
    assert result.planned_risk == D("240.00")


def test_long_stock_with_stop_sizes_by_stop_distance():
    result = size_position(D("100000"), D("0.25"), Side.LONG, D("200"), D("199"), 1)
    assert result.risk_per_unit == D("1")
    assert result.qty == 250


def test_short_with_stop_above_entry():
    result = size_position(D("100000"), D("0.25"), Side.SHORT, D("50"), D("52.50"), 1)
    assert result.risk_per_unit == D("2.50")
    assert result.qty == 100


def test_costs_are_part_of_the_risk():
    result = size_position(D("100000"), D("0.25"), Side.LONG, D("200"), D("199"), 1, D("0.25"))
    assert result.risk_per_unit == D("1.25")
    assert result.qty == 200


def test_quantity_rounds_down_never_up():
    # 250 / 0.83 = 301.2 shares.
    result = size_position(D("100000"), D("0.25"), Side.LONG, D("10"), D("9.17"), 1)
    assert result.qty == 301
    assert result.planned_risk <= D("250")


def test_requested_quantity_only_lowers_the_size():
    assert size_position(D("100000"), D("0.25"), Side.LONG, D("1.20"), None, 100, requested_qty=1).qty == 1
    assert size_position(D("100000"), D("0.25"), Side.LONG, D("1.20"), None, 100, requested_qty=50).qty == 2


def test_budget_smaller_than_one_unit_is_zero():
    assert size_position(D("10000"), D("0.25"), Side.LONG, D("1.20"), None, 100).qty == 0


def test_exact_budget_fits():
    assert size_position(D("100000"), D("0.25"), Side.LONG, D("2.50"), None, 100).qty == 1


@pytest.mark.parametrize("budget", [D("0"), D("-1")])
def test_no_budget_is_zero_units(budget):
    assert units_within(budget, D("1")) == 0


def test_zero_per_unit_is_an_error():
    with pytest.raises(ValueError):
        units_within(D("10"), D("0"))


@pytest.mark.parametrize("side,entry,stop", [
    (Side.LONG, D("10"), D("10")),
    (Side.LONG, D("10"), D("11")),
    (Side.SHORT, D("10"), D("9")),
    (Side.SHORT, D("10"), None),
])
def test_stop_on_the_wrong_side_is_an_error(side, entry, stop):
    with pytest.raises(ValueError):
        risk_per_unit(side, entry, stop, 1)
