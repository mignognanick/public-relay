"""Research-only VectorBT adapter: causality, costs, holdout isolation, and risk-engine direction."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

pytest.importorskip("vectorbt")

from alpha.registry import AlphaRegistry
from research.feature_store import FeatureStore, MarketBar
from research.vectorbt_adapter import (
    HARD_TRIAL_CAP,
    VectorBTBudget,
    VectorBTCosts,
    assert_daily_equity_etf,
    audit_decision_causality,
    compare_strategies,
    estimated_incremental_monthly_usd,
    load_daily_bars,
    run_experiment,
    validate_against_research_engine,
)
import research.vectorbt_adapter as adapter


def _store(closes: list[float], *, kind: str = "synthetic", symbol: str = "SPY") -> FeatureStore:
    cursor = date(2023, 1, 2)
    previous = Decimal(str(closes[0]))
    bars: list[MarketBar] = []
    for close in closes:
        while cursor.weekday() >= 5:
            cursor += timedelta(days=1)
        price = Decimal(str(close))
        bars.append(
            MarketBar(
                symbol=symbol,
                timestamp=datetime(cursor.year, cursor.month, cursor.day, 21, 0, tzinfo=timezone.utc),
                open=previous,
                high=max(previous, price),
                low=min(previous, price),
                close=price,
                volume=Decimal("1000"),
            )
        )
        previous = price
        cursor += timedelta(days=1)
    return FeatureStore.from_bars({symbol: bars}, kind=kind, source="vectorbt-unit-test")  # type: ignore[arg-type]


def _trend(n: int = 120) -> list[float]:
    up = [100.0 + i * 0.8 for i in range(n // 2)]
    peak = up[-1]
    down = [peak - i * 0.7 for i in range(n - len(up))]
    return up + down


def test_refuses_options_and_intraday_frequency():
    with pytest.raises(ValueError, match="options"):
        assert_daily_equity_etf("option", "1D")
    with pytest.raises(ValueError, match="bar frequency"):
        assert_daily_equity_etf("equity", "5min")


def test_refuses_duplicate_session_bars():
    store = _store([100.0 + i for i in range(12)])
    last = store.bars("SPY")[-1]
    extra = MarketBar(
        symbol="SPY",
        timestamp=last.timestamp + timedelta(minutes=5),
        open=last.open,
        high=last.high,
        low=last.low,
        close=last.close,
        volume=Decimal("1"),
    )
    ragged = FeatureStore.from_bars(
        {"SPY": (*store.bars("SPY"), extra)},
        kind="synthetic",
        source="intraday",
    )
    with pytest.raises(ValueError, match="intraday"):
        run_experiment(ragged, symbol="SPY", strategy="sma_crossover")


def test_signal_prefix_invariance_and_reference_fill():
    store = _store(_trend(90))
    close, opened, _dates = adapter._price_frame(store, "SPY")
    params = {"fast": 3, "slow": 10}
    ok, errors = audit_decision_causality(close, "sma_crossover", params)
    assert ok, errors
    passed = adapter.sma_decision_weights(close, 3, 10)
    manual_fast = close.rolling(3).mean()
    manual_slow = close.rolling(10).mean()
    expected = ((manual_fast > manual_slow) & manual_fast.notna()).astype(float)
    assert (passed.to_numpy() == expected.to_numpy()).all()

    check = validate_against_research_engine(
        store,
        symbol="SPY",
        strategy="sma_crossover",
        params=params,
        costs=VectorBTCosts(),
    )
    assert check["alignment_equity_gap"] < 1e-4
    assert check["position_mismatches"] == 0
    assert check["research_entries_follow_signal"] is True
    assert check["options_execution_modeled"] is False
    assert int(check["research_engine_trades"]) >= 1
    assert int(check["vectorbt_trades"]) >= 1


def test_selection_never_receives_the_holdout_and_synthetic_is_not_approved(monkeypatch):
    closes = _trend(120)
    store = _store(closes)
    n_days = len(store.dates)
    dev_end = n_days - max(20, int(n_days * 0.30))
    seen: list[int] = []
    original = adapter._select_params

    def _spy(*args, **kwargs):
        seen.append(len(args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "_select_params", _spy)
    registry = AlphaRegistry()
    result = run_experiment(
        store,
        symbol="SPY",
        strategy="sma_crossover",
        param_grid=({"fast": 3, "slow": 12}, {"fast": 5, "slow": 15}),
        registry=registry,
        budget=VectorBTBudget(max_trials=2),
    )
    assert seen == [dev_end]
    assert result.trial_count == 2
    assert result.paper_approved is False
    assert result.research_only is True
    assert result.recommended_for_paper is False
    assert result.is_synthetic is True
    assert any("Synthetic" in reason for reason in result.rejection_reasons)
    assert result.walk_forward_folds
    assert all(fold["holdout_used"] is False for fold in result.walk_forward_folds)
    assert all(fold["test_end"] in store.dates[:dev_end] for fold in result.walk_forward_folds)
    assert result.benchmark_metrics is not None
    assert result.regime_performance
    assert result.incremental_cost_usd == "0.000000"
    assert result.to_dict()["options_execution_modeled"] is False

    mutated = closes[:dev_end] + [close * 3.0 for close in closes[dev_end:]]
    other = run_experiment(
        _store(mutated),
        symbol="SPY",
        strategy="sma_crossover",
        param_grid=({"fast": 3, "slow": 12}, {"fast": 5, "slow": 15}),
        budget=VectorBTBudget(max_trials=2),
    )
    assert other.selected_params == result.selected_params

    registered = registry.latest("vbt_sma_crossover_SPY")
    assert registered is not None
    assert registered.deployment_status == "rejected"
    assert registered.execution == "PAPER"
    assert registered.research_only is True
    assert registry.approvals[-1]["paper_approved"] is False
    assert registry.approvals[-1]["approved_by"] is None
    assert all(row.is_synthetic for rows in registry._returns.values() for row in rows.values())


def test_strategy_comparison_does_not_read_the_holdout(monkeypatch):
    store = _store(_trend(100), kind="historical")
    dev_end = len(store.dates) - max(20, int(len(store.dates) * 0.30))
    seen: list[int] = []
    original = adapter._select_params

    def _spy(*args, **kwargs):
        seen.append(len(args[1]))
        return original(*args, **kwargs)

    monkeypatch.setattr(adapter, "_select_params", _spy)
    report = compare_strategies(store, symbol="SPY", asset_class="etf")
    assert report["holdout_used"] is False
    assert report["ranked_on"] == "development_validation_only"
    assert seen
    assert max(seen) <= dev_end
    assert {row["strategy_key"] for row in report["rows"]} == {"sma_crossover", "trailing_momentum"}
    assert all(row["holdout_used"] is False for row in report["rows"])


def test_trial_cap_cost_guard_and_csv_loader(monkeypatch):
    with pytest.raises(ValueError):
        VectorBTBudget(max_trials=HARD_TRIAL_CAP + 1)
    assert estimated_incremental_monthly_usd() == Decimal("0.00")
    monkeypatch.setattr(adapter, "estimated_incremental_monthly_usd", lambda: Decimal("31"))
    store = _store(_trend(90))
    with pytest.raises(ValueError, match="budget"):
        run_experiment(store, symbol="SPY", strategy="trailing_momentum")

    lines = ["date,symbol,open,high,low,close,volume"]
    for i in range(12):
        day = date(2024, 1, 2) + timedelta(days=i)
        price = 50 + i
        lines.append(f"{day.isoformat()},QQQ,{price},{price+1},{price-0.5},{price+0.2},1000")
    loaded = load_daily_bars("\n".join(lines), kind="historical", source="caller-csv")
    assert loaded.symbols == ("QQQ",)
    assert loaded.bundle.kind == "historical"
    assert "SYNTHETIC" not in " ".join(loaded.bundle.warnings)


def test_adapter_source_has_no_vendor_download_or_credential_names():
    source = adapter.__file__
    assert source is not None
    text = open(source, encoding="utf-8").read()
    for banned in (
        "YFData",
        "Binance",
        "CCXT",
        "Alpaca",
        "PUBLIC_SECRET",
        "SUPABASE_SERVICE_KEY",
        "WEBHOOK_PASSPHRASE",
        "CRON_SECRET",
        ".download(",
    ):
        assert banned not in text
