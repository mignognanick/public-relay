"""Causal walk-forward ensemble evaluation, contribution attribution, and benchmark comparison."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from alpha.allocator import AllocationConstraints, AllocationModel, AllocationResult, compute_allocation
from alpha.correlation import CorrelationReport, compute_correlation_report
from research.library import (
    HUNDRED,
    ONE,
    ZERO,
    compute_return_metrics,
    dec_from_float,
    dec_str,
)


@dataclass(frozen=True)
class EnsembleReport:
    model: AllocationModel
    research_only: bool
    ensemble_metrics: dict[str, object]
    equal_weight_metrics: dict[str, object]
    benchmark_metrics: dict[str, object] | None
    individual_metrics: dict[str, dict[str, object]]
    strategy_contributions: dict[str, dict[str, str]]
    regime_performance: dict[str, dict[str, object]]
    latest_allocation: AllocationResult
    correlation_report: CorrelationReport
    daily_returns: dict[str, Decimal]

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "research_only": self.research_only,
            "ensemble_metrics": self.ensemble_metrics,
            "equal_weight_metrics": self.equal_weight_metrics,
            "benchmark_metrics": self.benchmark_metrics,
            "individual_metrics": self.individual_metrics,
            "strategy_contributions": self.strategy_contributions,
            "regime_performance": self.regime_performance,
            "latest_allocation": self.latest_allocation.to_dict(),
            "correlation": self.correlation_report.to_dict(),
            "daily_returns": {d: dec_str(v) for d, v in self.daily_returns.items()},
        }


def evaluate_ensemble(
    model: AllocationModel,
    alpha_returns: Mapping[str, Mapping[str, Decimal]],
    *,
    constraints: AllocationConstraints | None = None,
    lookback_days: int = 40,
    rebalance_every: int = 5,
    cost_bps_per_turnover: Decimal = Decimal("2.0"),
    benchmark_returns: Mapping[str, Decimal] | None = None,
    regime_by_date: Mapping[str, str] | None = None,
    initial_equity: Decimal = Decimal("100000"),
) -> EnsembleReport:
    """Run strictly causal walk-forward ensemble combination and compare against benchmarks.

    Weights applied on day t are estimated strictly from trailing returns on days < t.
    """
    cfg = constraints or AllocationConstraints()
    alpha_ids = sorted(alpha_returns)
    all_dates = sorted({d for series in alpha_returns.values() for d in series})
    if not alpha_ids or len(all_dates) < max(5, cfg.min_observations + 2):
        empty_alloc = compute_allocation(model, alpha_returns, constraints=cfg)
        empty_metrics = compute_return_metrics({}, initial_equity=initial_equity)
        return EnsembleReport(
            model=model,
            research_only=(model == "v3"),
            ensemble_metrics=empty_metrics,
            equal_weight_metrics=empty_metrics,
            benchmark_metrics=None,
            individual_metrics={},
            strategy_contributions={},
            regime_performance={},
            latest_allocation=empty_alloc,
            correlation_report=empty_alloc.correlation_report,
            daily_returns={},
        )

    start_idx = min(max(cfg.min_observations, lookback_days), len(all_dates) - 2)
    eval_dates = all_dates[start_idx:]

    ens_daily: dict[str, Decimal] = {}
    eqw_daily: dict[str, Decimal] = {}
    turnovers: list[Decimal] = []
    total_cost_rate_sum = ZERO
    contrib_ret: dict[str, Decimal] = {aid: ZERO for aid in alpha_ids}
    contrib_sq: dict[str, Decimal] = {aid: ZERO for aid in alpha_ids}

    prev_weights: dict[str, Decimal] | None = None
    current_weights: dict[str, Decimal] = {aid: ZERO for aid in alpha_ids}
    latest_alloc: AllocationResult | None = None
    cost_rate = cost_bps_per_turnover / Decimal("10000")

    for step, d in enumerate(eval_dates):
        global_idx = start_idx + step
        if step % max(1, rebalance_every) == 0 or prev_weights is None:
            hist_start = max(0, global_idx - lookback_days)
            hist_dates = set(all_dates[hist_start:global_idx])  # strictly < d
            trailing = {
                aid: {dt: val for dt, val in alpha_returns[aid].items() if dt in hist_dates}
                for aid in alpha_ids
            }
            alloc = compute_allocation(
                model,
                trailing,
                constraints=cfg,
                previous_weights=prev_weights,
            )
            current_weights = alloc.weights
            turnover = alloc.turnover
            prev_weights = current_weights
            latest_alloc = alloc
        else:
            turnover = ZERO

        # Portfolio return on day d using weights decided strictly before day d
        gross_r = ZERO
        for aid in alpha_ids:
            r_a = alpha_returns[aid].get(d, ZERO)
            w_a = current_weights.get(aid, ZERO)
            c_a = w_a * r_a
            gross_r += c_a
            contrib_ret[aid] += c_a
            contrib_sq[aid] += c_a * c_a

        day_cost = turnover * cost_rate
        total_cost_rate_sum += day_cost
        ens_daily[d] = gross_r - day_cost
        turnovers.append(turnover)

        # Equal-weight benchmark (1/N across available alphas on day d)
        avail = [alpha_returns[aid][d] for aid in alpha_ids if d in alpha_returns[aid]]
        eqw_daily[d] = (sum(avail, ZERO) / Decimal(len(avail))) if avail else ZERO

    # Compute final latest allocation using all available history for forward proposal
    full_trailing = {
        aid: {dt: val for dt, val in alpha_returns[aid].items() if dt in set(all_dates[-lookback_days:])}
        for aid in alpha_ids
    }
    latest_alloc = compute_allocation(
        model,
        full_trailing,
        constraints=cfg,
        previous_weights=prev_weights,
    )

    total_costs_dollars = initial_equity * total_cost_rate_sum
    ens_metrics = compute_return_metrics(
        ens_daily,
        initial_equity=initial_equity,
        turnovers=turnovers,
        total_costs=total_costs_dollars,
    )
    eqw_metrics = compute_return_metrics(eqw_daily, initial_equity=initial_equity)

    bench_metrics = None
    if benchmark_returns:
        aligned_bench = {d: benchmark_returns[d] for d in eval_dates if d in benchmark_returns}
        if aligned_bench:
            bench_metrics = compute_return_metrics(aligned_bench, initial_equity=initial_equity)

    indiv_metrics = {
        aid: compute_return_metrics(
            {d: alpha_returns[aid][d] for d in eval_dates if d in alpha_returns[aid]},
            initial_equity=initial_equity,
        )
        for aid in alpha_ids
    }

    total_contrib = sum(contrib_ret.values(), ZERO)
    total_abs_contrib = sum((abs(v) for v in contrib_ret.values()), ZERO)
    contributions: dict[str, dict[str, str]] = {}
    for aid in alpha_ids:
        share = (contrib_ret[aid] / total_abs_contrib * HUNDRED) if total_abs_contrib > ZERO else ZERO
        contributions[aid] = {
            "cumulative_return_contribution_pct": dec_str(contrib_ret[aid] * HUNDRED),
            "dollar_contribution": dec_str(contrib_ret[aid] * initial_equity),
            "relative_share_pct": dec_str(share),
            "current_weight": dec_str(latest_alloc.weights.get(aid, ZERO)),
        }
    _ = total_contrib

    # Regime breakdown of ensemble daily returns
    regime_perf: dict[str, dict[str, object]] = {}
    if regime_by_date:
        by_reg: dict[str, dict[str, Decimal]] = {}
        for d, r in ens_daily.items():
            reg = regime_by_date.get(d, "chop")
            by_reg.setdefault(reg, {})[d] = r
        for reg, series in sorted(by_reg.items()):
            m = compute_return_metrics(series, initial_equity=initial_equity)
            regime_perf[reg] = {
                "observations": m["observations"],
                "total_return_pct": m["total_return_pct"],
                "sharpe_ratio": m["sharpe_ratio"],
                "max_drawdown_pct": m["max_drawdown_pct"],
            }

    full_corr = compute_correlation_report(
        {aid: {d: alpha_returns[aid][d] for d in eval_dates if d in alpha_returns[aid]} for aid in alpha_ids},
        min_overlap=min(5, cfg.min_observations),
        shrinkage=cfg.shrinkage,
        ridge=cfg.ridge,
    )

    return EnsembleReport(
        model=model,
        research_only=(model == "v3"),
        ensemble_metrics=ens_metrics,
        equal_weight_metrics=eqw_metrics,
        benchmark_metrics=bench_metrics,
        individual_metrics=indiv_metrics,
        strategy_contributions=contributions,
        regime_performance=regime_perf,
        latest_allocation=latest_alloc,
        correlation_report=full_corr,
        daily_returns=ens_daily,
    )


def compare_allocation_models(
    alpha_returns: Mapping[str, Mapping[str, Decimal]],
    *,
    constraints: AllocationConstraints | None = None,
    lookback_days: int = 40,
    rebalance_every: int = 5,
    benchmark_returns: Mapping[str, Decimal] | None = None,
    regime_by_date: Mapping[str, str] | None = None,
) -> dict[str, dict[str, object]]:
    """Run V1, V2, and V3 side-by-side on identical out-of-sample dates and costs."""
    models: Sequence[AllocationModel] = ("v1", "v2", "v3")
    return {
        m: evaluate_ensemble(
            m,
            alpha_returns,
            constraints=constraints,
            lookback_days=lookback_days,
            rebalance_every=rebalance_every,
            benchmark_returns=benchmark_returns,
            regime_by_date=regime_by_date,
        ).to_dict()
        for m in models
    }
