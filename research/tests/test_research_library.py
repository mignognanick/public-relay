"""Tests for quantitative research library, 5 mathematical strategies, causality, and orchestrator."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Mapping

import pytest

from quant_engine.dashboard.domain import Tax
from research.experiment_runner import ExperimentRunner
from research.feature_store import (
    FeatureStore,
    MarketBar,
    TextObservation,
    generate_synthetic_us_equity_dataset,
    parse_daily_ohlcv_csv,
)
from research.library import (
    ACADEMIC_SOURCES,
    hodrick_prescott_filter,
)
from research.orchestrator import ResearchBudget, ResearchHypothesis, ResearchOrchestrator
from research.strategy_catalog import STRATEGY_CATALOG, StrategyMetadata, get_strategy
from research.validator import (
    ExecutionCostModel,
    audit_causality,
    simulate_signal_frame,
    walk_forward_and_holdout_validate,
)


def test_academic_sources_verified_and_complete():
    expected_ids = {
        "ks_8_1_hp_filter",
        "ks_3_17_knn",
        "ks_3_18_stat_arb",
        "ks_18_2_ann_quantile",
        "ks_18_3_bernoulli_nb",
        "ks_3_20_alpha_combo",
    }
    assert expected_ids <= set(ACADEMIC_SOURCES)
    for sid in expected_ids:
        src = ACADEMIC_SOURCES[sid]
        assert "ssrn.com/abstract=3247865" in src.primary_url
        assert "arxiv.org/pdf/1912.04492" in src.secondary_url
        assert src.equation_numbers
        assert src.adaptations_for_us_equities


def test_hodrick_prescott_exact_first_order_condition():
    series = [100.0 + 0.5 * i + (3.0 if i % 4 == 0 else -1.5) for i in range(40)]
    lamb = 1600.0
    trend, cycle = hodrick_prescott_filter(series, lamb=lamb)
    assert len(trend) == len(series)
    for s, tr, cy in zip(series, trend, cycle):
        assert abs((tr + cy) - s) < 1e-10

    # Verify (I + lamb * D^T D) trend == series directly
    n = len(series)
    residual = [trend[i] - series[i] for i in range(n)]
    for k in range(n - 2):
        d2 = trend[k] - 2.0 * trend[k + 1] + trend[k + 2]
        residual[k] += lamb * d2
        residual[k + 1] += lamb * (-2.0 * d2)
        residual[k + 2] += lamb * d2
    assert max(abs(r) for r in residual) < 1e-8

    # Zero smoothing reproduces input exactly
    t0, c0 = hodrick_prescott_filter(series, lamb=0.0)
    assert t0 == series and all(x == 0.0 for x in c0)


def test_feature_store_validation_and_point_in_time_isolation():
    store = generate_synthetic_us_equity_dataset(symbols=("SPY", "QQQ"), n_days=40, seed=7)
    assert store.symbols == ("QQQ", "SPY")
    assert len(store.dates) == 40

    cutoff = store.bars("SPY")[19].timestamp
    sliced_bars = store.bars("SPY", as_of=cutoff)
    assert len(sliced_bars) == 20
    assert all(b.timestamp <= cutoff for b in sliced_bars)

    sliced_texts = store.texts("SPY", as_of=cutoff)
    assert all(t.published_at <= cutoff for t in sliced_texts)

    # Reject bad OHLC, duplicates, naive timestamps, and future bars
    spy_bars = list(store.bars("SPY"))
    with pytest.raises(ValueError, match="OHLC"):
        FeatureStore.from_bars({"SPY": [replace(spy_bars[0], high=Decimal("1.00"))] + spy_bars[1:]}, kind="synthetic", source="bad")
    with pytest.raises(ValueError, match="Duplicate"):
        FeatureStore.from_bars({"SPY": [spy_bars[0], spy_bars[0]] + spy_bars[1:]}, kind="synthetic", source="bad")
    with pytest.raises(ValueError, match="timezone"):
        FeatureStore.from_bars(
            {"SPY": [replace(spy_bars[0], timestamp=spy_bars[0].timestamp.replace(tzinfo=None))] + spy_bars[1:]},
            kind="synthetic",
            source="bad",
        )
    with pytest.raises(ValueError, match="Future"):
        FeatureStore.from_bars({"SPY": spy_bars}, kind="synthetic", source="bad", now=spy_bars[5].timestamp)

    # CSV ingestion roundtrip
    csv_lines = ["date,symbol,open,high,low,close,volume"]
    for b in spy_bars:
        csv_lines.append(f"{b.day},SPY,{b.open},{b.high},{b.low},{b.close},{b.volume}")
    parsed = parse_daily_ohlcv_csv("\n".join(csv_lines))
    assert len(parsed["SPY"]) == len(spy_bars)


@pytest.mark.parametrize("strategy_key", list(STRATEGY_CATALOG))
def test_all_five_strategies_pass_causality_and_train_only_fit(strategy_key):
    store = generate_synthetic_us_equity_dataset(n_days=160, seed=11)
    strategy = get_strategy(strategy_key)
    ok, errors = audit_causality(strategy, store, strategy.metadata.default_params)
    assert ok, f"{strategy_key} failed causality audit: {errors}"

    # Perturbing test period bars/texts must not alter training fitted state or signals before test start
    train_end = 90
    train_store = store.slice_prefix(train_end)
    fitted_clean = strategy.fit(train_store, strategy.metadata.default_params)

    mutated_bars = {}
    mutated_texts = {}
    for sym in store.symbols:
        orig = list(store.bars(sym))
        changed = orig[:train_end] + [
            replace(
                b,
                open=b.open * Decimal("2"),
                high=b.high * Decimal("2"),
                low=b.low * Decimal("2"),
                close=b.close * Decimal("2"),
            )
            for b in orig[train_end:]
        ]
        mutated_bars[sym] = changed
        orig_txt = list(store.texts(sym))
        mutated_texts[sym] = [
            TextObservation(sym, t.published_at, "completely mutated future panic crash" if t.day > store.dates[train_end - 1] else t.text)
            for t in orig_txt
        ]
    perturbed_store = FeatureStore.from_bars(
        mutated_bars, kind="synthetic", source="perturbed", texts_by_symbol=mutated_texts
    )
    fitted_after = strategy.fit(perturbed_store.slice_prefix(train_end), strategy.metadata.default_params)
    assert fitted_clean == fitted_after

    sig_clean = strategy.generate_signals(store, strategy.metadata.default_params, fitted_clean)
    sig_pert = strategy.generate_signals(perturbed_store, strategy.metadata.default_params, fitted_clean)
    for d in store.dates[:train_end]:
        assert sig_clean[d] == sig_pert[d]


def test_causality_audit_catches_leaky_strategy():
    class LeakyStrategy:
        metadata = StrategyMetadata(
            strategy_key="leaky_mock",
            name="Leaky Mock",
            version="1.0.0",
            source_id="ks_8_1_hp_filter",
            portfolio="swing",
            research_only=True,
            permitted_regimes=("chop",),
            default_params={},
            param_grid=({},),
            description="Deliberately uses future close",
        )

        def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
            return {}

        def generate_signals(self, store: FeatureStore, params: Mapping[str, Any], fitted_state: Mapping[str, Any]):
            bars = store.bars("SPY")
            final_close = float(bars[-1].close)
            return {b.day: {"SPY": 1.0 if float(b.close) < final_close else -1.0} for b in bars}

    store = generate_synthetic_us_equity_dataset(symbols=("SPY", "QQQ"), n_days=80, seed=5)
    ok, errs = audit_causality(LeakyStrategy(), store, {})
    assert not ok
    assert any("LOOKAHEAD_BIAS_DETECTED" in e for e in errs)


def test_stat_arb_dollar_neutrality_and_singular_covariance_handling():
    # Create collinear/duplicate asset series so raw sample covariance is singular (Footnote 61)
    base = generate_synthetic_us_equity_dataset(symbols=("SPY", "QQQ", "IWM"), n_days=100, seed=3)
    spy_bars = base.bars("SPY")
    dup_bars = [replace(b, symbol="SPY2") for b in spy_bars]
    store = FeatureStore.from_bars(
        {
            "SPY": spy_bars,
            "SPY2": dup_bars,
            "QQQ": base.bars("QQQ"),
            "IWM": base.bars("IWM"),
        },
        kind="synthetic",
        source="collinear_test",
    )
    strat = get_strategy("stat_arb_optimization")
    fitted = strat.fit(store.slice_prefix(60), {"dollar_neutral": True, "cov_window": 30})
    signals = strat.generate_signals(store, {"dollar_neutral": True, "cov_window": 30}, fitted)
    active_days = [w for w in signals.values() if sum(abs(v) for v in w.values()) > 0.5]
    assert len(active_days) > 20
    for w in active_days:
        assert abs(sum(w.values())) < 1e-6
        assert abs(sum(abs(v) for v in w.values()) - 1.0) < 1e-6


def test_bernoulli_nb_ignores_future_headlines_and_handles_missing_text():
    store = generate_synthetic_us_equity_dataset(symbols=("SPY", "QQQ"), n_days=80, seed=9)
    no_text_store = FeatureStore.from_bars(
        {"SPY": store.bars("SPY"), "QQQ": store.bars("QQQ")},
        kind="synthetic",
        source="no_text",
        texts_by_symbol={},
    )
    strat = get_strategy("bernoulli_nb_sentiment")
    fitted = strat.fit(no_text_store, {})
    assert fitted["ready"] is False
    sigs = strat.generate_signals(no_text_store, {}, fitted)
    assert all(v["SPY"] == 0.0 for v in sigs.values())


def test_validator_rejects_synthetic_and_preserves_protective_exits():
    store = generate_synthetic_us_equity_dataset(symbols=("SPY", "QQQ"), n_days=180, seed=19)
    strat = get_strategy("hp_trend_filter")
    verdict = walk_forward_and_holdout_validate(
        strat,
        store,
        strat.metadata.default_params,
        trial_count=5,
        tax_profile=Tax(carryforward=Decimal("35000")),
    )
    assert verdict.causality_passed is True
    assert verdict.paper_approved is False
    assert verdict.recommended_for_paper is False
    assert any("Synthetic" in r for r in verdict.rejection_reasons)
    assert float(verdict.multiple_testing_penalty) > 0.0
    assert "tax_center_projection" in verdict.holdout_result.tax_impact

    # Verify protective exit executes even under severe crash triggering weekly kill switch
    bars = list(store.bars("SPY"))[:40]
    crashed = []
    for idx, b in enumerate(bars):
        if idx < 15:
            p = Decimal("100.00")
        elif idx == 15:
            p = Decimal("80.00")  # -20% gap drop hits stop & weekly kill switch
        else:
            p = Decimal("75.00")
        crashed.append(replace(b, open=p, high=p + Decimal("0.50"), low=p - Decimal("0.50"), close=p))
    crash_store = FeatureStore.from_bars({"SPY": crashed}, kind="synthetic", source="crash")
    always_long = {b.day: {"SPY": 1.0} for b in crashed}
    sim = simulate_signal_frame(crash_store, always_long, cost_model=ExecutionCostModel(stop_pct=Decimal("2.0")))
    assert len(sim.trades) >= 1
    assert sim.trades[0]["reason"] == "stop_exit"
    assert sim.risk_status["kill_switch"]["engaged"] is True
    assert sim.rejected_entries  # Subsequent entries blocked by kill switch after protective exit closed position


def test_orchestrator_enforces_budgets_and_never_auto_promotes():
    store = generate_synthetic_us_equity_dataset(n_days=160, seed=21)
    budget = ResearchBudget(
        max_experiments=2,
        max_trials_per_hypothesis=2,
        max_compute_seconds=30.0,
        max_ai_tokens=100,
        max_ai_cost_usd=Decimal("0.50"),
    )
    orch = ResearchOrchestrator(budget=budget)
    cycle = orch.run_cycle(store, max_steps=5)
    assert cycle["processed"] == 2
    assert cycle["stop_reason"] == "budget_exhausted"
    assert orch.usage.experiments_run == 2
    assert all(e.verdict is None or not e.verdict.paper_approved for e in orch.runner.experiments)

    # AI spend budget fail-closed test
    orch2 = ResearchOrchestrator(budget=ResearchBudget(max_ai_tokens=50, max_ai_cost_usd=Decimal("0.10")))
    res = orch2.run_next(store, advisory_tokens=120, advisory_cost_usd=Decimal("0.25"))
    assert res["status"] == "budget_exhausted"
    assert "budget exhausted" in str(res["reason"]).lower()
