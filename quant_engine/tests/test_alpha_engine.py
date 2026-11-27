"""Tests for alpha registry, correlation diagnostics, V1/V2/V3 allocators, and ensemble evaluation."""
from __future__ import annotations

import math
import random
from datetime import date, timedelta
from decimal import Decimal

import pytest

from alpha.allocator import AllocationConstraints, compute_allocation
from alpha.correlation import compute_correlation_report
from alpha.ensemble import compare_allocation_models, evaluate_ensemble
from alpha.registry import AlphaDefinition, AlphaRegistry, DailyAlphaReturn


def _make_daily_returns(n_days: int = 80, seed: int = 13) -> dict[str, dict[str, Decimal]]:
    rng = random.Random(seed)
    dates = [(date(2024, 1, 2) + timedelta(days=i)).isoformat() for i in range(n_days)]
    common = [rng.gauss(0.0005, 0.006) for _ in range(n_days)]
    a1 = {d: Decimal(f"{0.0007 + 0.5 * common[i] + rng.gauss(0, 0.004):.6f}") for i, d in enumerate(dates)}
    a2 = {d: Decimal(f"{0.0006 + 0.5 * common[i] + rng.gauss(0, 0.012):.6f}") for i, d in enumerate(dates)}  # higher vol
    a3 = {d: Decimal(f"{0.0008 - 0.3 * common[i] + rng.gauss(0, 0.005):.6f}") for i, d in enumerate(dates)}  # negatively corr
    a4 = {d: Decimal(f"{-0.0012 + rng.gauss(0, 0.006):.6f}") for i, d in enumerate(dates)}  # negative expected return
    return {"alpha_low_vol": a1, "alpha_high_vol": a2, "alpha_hedge": a3, "alpha_negative": a4}


def test_alpha_registry_paper_only_and_versioning():
    reg = AlphaRegistry()
    d1 = reg.register(
        alpha_id="hp_spy",
        name="HP Trend SPY",
        strategy_key="hp_trend_filter",
        source_id="ks_8_1_hp_filter",
        portfolio="swing",
        parameters={"lamb": 1600},
    )
    d2 = reg.register(
        alpha_id="hp_spy",
        name="HP Trend SPY v2",
        strategy_key="hp_trend_filter",
        source_id="ks_8_1_hp_filter",
        portfolio="swing",
        parameters={"lamb": 3200},
    )
    assert d1.version == 1 and d2.version == 2
    assert reg.latest("hp_spy").version == 2

    # Refuse LIVE execution, paper_enabled on research_only, or unapproved paper_approved
    with pytest.raises(ValueError, match="PAPER"):
        AlphaDefinition(
            alpha_id="bad",
            version=1,
            name="Bad",
            strategy_key="hp_trend_filter",
            source_id="ks_8_1_hp_filter",
            portfolio="swing",
            parameters={},
            spec_hash="a" * 64,
            execution="LIVE",  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="human approver"):
        reg.register(
            alpha_id="unapproved",
            name="Unapproved",
            strategy_key="hp_trend_filter",
            source_id="ks_8_1_hp_filter",
            portfolio="swing",
            parameters={},
            deployment_status="paper_approved",
        )
    with pytest.raises(ValueError, match="30 held-out trades"):
        reg.record_approval(
            alpha_id="hp_spy",
            version=2,
            validation_status="paper_approved",
            paper_approved=True,
            approved_by="nick",
            held_out_trades=12,
            trial_count=3,
            rejection_reasons=[],
            reason="Too few trades",
        )


def test_correlation_handles_missing_dates_zero_variance_and_singularity():
    base = _make_daily_returns(60)
    # Add zero-variance alpha, sparse missing-date alpha, and exact duplicate (singular covariance)
    dates = sorted(base["alpha_low_vol"])
    extended = {
        **base,
        "alpha_dup": dict(base["alpha_low_vol"]),
        "alpha_flat": {d: Decimal("0") for d in dates},
        "alpha_sparse": {dates[0]: Decimal("0.01"), dates[1]: Decimal("-0.01")},
    }
    rep = compute_correlation_report(extended, min_overlap=5, shrinkage=0.25)
    assert "alpha_flat" in rep.zero_variance_alphas
    assert "alpha_sparse" in rep.zero_variance_alphas
    assert rep.is_singular is True
    i_low = rep.alpha_ids.index("alpha_low_vol")
    i_dup = rep.alpha_ids.index("alpha_dup")
    assert abs(rep.correlation_matrix[i_low][i_dup] - 1.0) < 1e-6

    # Reject NaN / Inf
    with pytest.raises(ValueError, match="Non-finite"):
        compute_correlation_report({"bad": {"2024-01-02": Decimal("NaN")}})


def test_v1_v2_v3_allocations_and_safety_guardrails():
    rets = _make_daily_returns(80)
    # Add a zero-variance alpha to ensure all 3 models zero it cleanly
    dates = sorted(rets["alpha_low_vol"])
    rets["alpha_zero"] = {d: Decimal("0") for d in dates}

    # V1 Equal Risk: lower-vol alpha receives higher weight than higher-vol alpha
    v1 = compute_allocation("v1", rets, constraints=AllocationConstraints(max_weight=Decimal("0.60")))
    assert v1.research_only is False
    assert v1.weights["alpha_zero"] == Decimal("0")
    assert v1.weights["alpha_low_vol"] > v1.weights["alpha_high_vol"]
    assert abs(sum(v1.weights.values()) - Decimal("1")) < Decimal("1e-5")

    # V2 Vol & Correlation adjusted: penalizes duplicate correlated alpha pair vs hedge
    dup_rets = {
        "a_copy1": rets["alpha_low_vol"],
        "a_copy2": dict(rets["alpha_low_vol"]),
        "a_hedge": rets["alpha_hedge"],
    }
    v2 = compute_allocation("v2", dup_rets, constraints=AllocationConstraints(max_weight=Decimal("0.60")))
    assert v2.research_only is False
    assert v2.weights["a_hedge"] > v2.weights["a_copy1"]
    assert all(w >= Decimal("0") for w in v2.weights.values())
    assert all(w <= Decimal("0.600001") for w in v2.weights.values())

    # V3 Kakushadze-Yu: research-only, blocks unapproved inversion by default
    v3_safe = compute_allocation("v3", rets, constraints=AllocationConstraints(max_weight=Decimal("0.60")))
    assert v3_safe.research_only is True
    assert v3_safe.weights["alpha_zero"] == Decimal("0")
    assert all(w >= Decimal("0") for w in v3_safe.weights.values())
    assert any("RESEARCH-ONLY" in w for w in v3_safe.warnings)

    # When inversion is explicitly approved on an alpha with negative raw weight, negative weight is allowed
    neg_candidates = [k for k, v in v3_safe.raw_weights.items() if v < Decimal("-0.01")]
    if neg_candidates:
        target_inv = neg_candidates[0]
        v3_inv = compute_allocation(
            "v3",
            rets,
            constraints=AllocationConstraints(
                max_weight=Decimal("0.60"),
                allow_inversion_by_alpha={target_inv: True},
            ),
        )
        assert v3_inv.weights[target_inv] < Decimal("0")

    # Turnover cap test
    prev = {aid: (Decimal("1") if aid == "alpha_high_vol" else Decimal("0")) for aid in rets}
    v1_damped = compute_allocation(
        "v1",
        rets,
        constraints=AllocationConstraints(max_weight=Decimal("0.60"), max_turnover=Decimal("0.20")),
        previous_weights=prev,
    )
    assert v1_damped.turnover <= Decimal("0.200001")
    assert any("Turnover damped" in w for w in v1_damped.warnings)


def test_ensemble_causal_comparison_and_contributions():
    rets = _make_daily_returns(90, seed=55)
    dates = sorted(rets["alpha_low_vol"])
    bench = {d: Decimal("0.0004") for d in dates}
    regimes = {d: ("bull" if i % 2 == 0 else "chop") for i, d in enumerate(dates)}

    comparison = compare_allocation_models(
        rets,
        lookback_days=25,
        rebalance_every=5,
        benchmark_returns=bench,
        regime_by_date=regimes,
    )
    assert set(comparison.keys()) == {"v1", "v2", "v3"}
    assert comparison["v1"]["research_only"] is False
    assert comparison["v2"]["research_only"] is False
    assert comparison["v3"]["research_only"] is True

    for model_id, rep in comparison.items():
        assert rep["equal_weight_metrics"] is not None
        assert rep["benchmark_metrics"] is not None
        assert set(rep["individual_metrics"].keys()) == set(rets.keys())
        assert set(rep["strategy_contributions"].keys()) == set(rets.keys())
        assert set(rep["regime_performance"].keys()) == {"bull", "chop"}

    # Causality check on ensemble: modifying returns on the final day must NOT alter earlier ensemble returns
    mutated = {k: dict(v) for k, v in rets.items()}
    last_day = dates[-1]
    for k in mutated:
        mutated[k][last_day] = Decimal("0.25")
    rep_orig = evaluate_ensemble("v2", rets, lookback_days=25, rebalance_every=5)
    rep_mut = evaluate_ensemble("v2", mutated, lookback_days=25, rebalance_every=5)
    for d in dates[:-1]:
        if d in rep_orig.daily_returns:
            assert rep_orig.daily_returns[d] == rep_mut.daily_returns[d]
