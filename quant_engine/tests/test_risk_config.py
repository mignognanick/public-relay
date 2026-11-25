from decimal import Decimal

import pytest

from quant_engine.risk import RiskConfig


def test_defaults_are_the_v1_paper_limits():
    config = RiskConfig()
    assert config.max_trade_risk_pct == Decimal("0.25")
    assert config.daily_loss_limit_pct == Decimal("0.75")
    assert config.weekly_loss_limit_pct == Decimal("2.00")
    assert config.max_open_risk_pct == Decimal("0.50")
    assert config.kill_switch_on_weekly_breach is True
    assert config.timezone == "America/New_York"


def test_env_overrides_and_blank_keeps_default():
    config = RiskConfig.from_env({
        "RISK_MAX_TRADE_RISK_PCT": "0.10",
        "RISK_DAILY_LOSS_LIMIT_PCT": " 0.5 ",
        "RISK_WEEKLY_LOSS_LIMIT_PCT": "",
        "RISK_MAX_OPEN_RISK_PCT": "0.30",
        "RISK_MAX_SNAPSHOT_AGE_SECONDS": "60",
        "RISK_KILL_SWITCH_ON_WEEKLY_BREACH": "false",
    })
    assert config.max_trade_risk_pct == Decimal("0.10")
    assert config.daily_loss_limit_pct == Decimal("0.5")
    assert config.weekly_loss_limit_pct == Decimal("2.00")
    assert config.max_open_risk_pct == Decimal("0.30")
    assert config.max_snapshot_age_seconds == 60
    assert config.kill_switch_on_weekly_breach is False


def test_from_env_with_no_mapping_uses_defaults():
    assert RiskConfig.from_env(None) == RiskConfig()


@pytest.mark.parametrize("value", ["0", "-0.1", "101", "abc", "NaN", "Infinity", True, None])
def test_bad_percent_refuses_to_start(value):
    with pytest.raises(ValueError):
        RiskConfig(max_trade_risk_pct=value)


@pytest.mark.parametrize("env", [
    {"RISK_MAX_SNAPSHOT_AGE_SECONDS": "1.5"},
    {"RISK_MAX_SNAPSHOT_AGE_SECONDS": "-5"},
    {"RISK_MAX_SNAPSHOT_AGE_SECONDS": "0"},
    {"RISK_KILL_SWITCH_ON_WEEKLY_BREACH": "maybe"},
    {"RISK_DAILY_LOSS_LIMIT_PCT": "nan"},
])
def test_bad_env_refuses_to_start(env):
    with pytest.raises(ValueError):
        RiskConfig.from_env(env)


def test_per_trade_risk_cannot_exceed_aggregate():
    with pytest.raises(ValueError, match="max_trade_risk_pct"):
        RiskConfig(max_trade_risk_pct="0.6", max_open_risk_pct="0.5")


def test_daily_limit_cannot_exceed_weekly():
    with pytest.raises(ValueError, match="daily_loss_limit_pct"):
        RiskConfig(daily_loss_limit_pct="3", weekly_loss_limit_pct="2")


def test_unknown_timezone_refuses_to_start():
    with pytest.raises(ValueError, match="timezone"):
        RiskConfig(timezone="Mars/Olympus")


def test_public_view_is_strings_and_has_no_surprises():
    view = RiskConfig().public_view()
    assert view["max_trade_risk_pct"] == "0.25"
    assert view["max_open_risk_pct"] == "0.50"
    assert set(view) == {
        "max_trade_risk_pct", "daily_loss_limit_pct", "weekly_loss_limit_pct", "max_open_risk_pct",
        "max_gross_exposure_pct", "max_snapshot_age_seconds", "kill_switch_on_weekly_breach", "timezone",
    }
