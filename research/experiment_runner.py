"""Controlled experiment runner with trial counting, cost/tax evaluation, and alpha registration."""
from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import uuid4

from alpha.correlation import compute_correlation_report
from alpha.registry import AlphaRegistry, DailyAlphaReturn
from quant_engine.dashboard.domain import Tax
from research.feature_store import FeatureStore
from research.library import ZERO, dec_from_float, dec_str
from research.strategy_catalog import get_strategy
from research.validator import (
    ExecutionCostModel,
    ValidationVerdict,
    simulate_signal_frame,
    walk_forward_and_holdout_validate,
)


@dataclass(frozen=True)
class ExperimentRecord:
    experiment_id: str
    hypothesis_id: str | None
    alpha_id: str
    alpha_version: int
    strategy_key: str
    source_id: str
    dataset_id: str
    dataset_kind: str
    status: str
    selected_params: dict[str, Any]
    trial_count: int
    trials_tested: list[dict[str, object]]
    verdict: ValidationVerdict | None
    max_corr_with_registry: float | None
    compute_seconds: float
    error: str | None
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "hypothesis_id": self.hypothesis_id,
            "alpha_id": self.alpha_id,
            "alpha_version": self.alpha_version,
            "strategy_key": self.strategy_key,
            "source_id": self.source_id,
            "dataset_id": self.dataset_id,
            "dataset_kind": self.dataset_kind,
            "status": self.status,
            "selected_params": dict(self.selected_params),
            "trial_count": self.trial_count,
            "trials_tested": list(self.trials_tested),
            "verdict": self.verdict.to_dict() if self.verdict else None,
            "max_corr_with_registry": round(self.max_corr_with_registry, 4)
            if self.max_corr_with_registry is not None
            else None,
            "compute_seconds": round(self.compute_seconds, 4),
            "error": self.error,
            "rejection_reasons": list(self.verdict.rejection_reasons)
            if self.verdict
            else ([self.error] if self.error else []),
            "created_at": self.created_at,
        }


class ExperimentRunner:
    """Runs controlled parameter sweeps strictly on development data, then evaluates holdout once."""

    def __init__(
        self,
        registry: AlphaRegistry | None = None,
        cost_model: ExecutionCostModel | None = None,
        initial_equity: Decimal = Decimal("100000"),
    ) -> None:
        self.registry = registry or AlphaRegistry()
        self.cost_model = cost_model or ExecutionCostModel()
        self.initial_equity = initial_equity
        self.experiments: list[ExperimentRecord] = []

    def run_controlled_experiment(
        self,
        strategy_key: str,
        store: FeatureStore,
        *,
        param_grid: Sequence[Mapping[str, Any]] | None = None,
        hypothesis_id: str | None = None,
        alpha_id: str | None = None,
        max_trials: int = 10,
        holdout_fraction: float = 0.30,
        purge_days: int = 5,
        min_holdout_trades: int = 30,
        tax_profile: Tax | None = None,
    ) -> ExperimentRecord:
        started = time.monotonic()
        exp_id = f"exp-{uuid4().hex[:12]}"
        created_at = datetime.now(timezone.utc).isoformat()

        strategy = get_strategy(strategy_key)
        meta = strategy.metadata
        aid = alpha_id or strategy_key

        candidates = list(param_grid) if param_grid else [dict(p) for p in meta.param_grid]
        if not candidates:
            candidates = [dict(meta.default_params)]
        candidates = candidates[: max(1, max_trials)]

        # Development slice strictly excludes reserved holdout during parameter selection
        n_days = len(store.dates)
        holdout_len = max(20, int(n_days * holdout_fraction))
        dev_end_idx = n_days - holdout_len
        if dev_end_idx < 35:
            err = f"Insufficient sessions ({n_days}) for controlled dev/holdout split"
            failed = ExperimentRecord(
                experiment_id=exp_id,
                hypothesis_id=hypothesis_id,
                alpha_id=aid,
                alpha_version=0,
                strategy_key=strategy_key,
                source_id=meta.source_id,
                dataset_id=store.bundle.dataset_id,
                dataset_kind=store.bundle.kind,
                status="failed",
                selected_params={},
                trial_count=len(candidates),
                trials_tested=[],
                verdict=None,
                max_corr_with_registry=None,
                compute_seconds=time.monotonic() - started,
                error=err,
                created_at=created_at,
            )
            self.experiments.append(failed)
            return failed

        dev_dates = store.dates[:dev_end_idx]
        dev_train_end = max(25, int(len(dev_dates) * 0.65))
        dev_val_start = min(len(dev_dates) - 6, dev_train_end + purge_days)
        dev_train_store = store.slice_by_dates(dev_dates[0], dev_dates[dev_train_end - 1])
        dev_full_store = store.slice_by_dates(dev_dates[0], dev_dates[-1])
        dev_val_store = store.slice_by_dates(dev_dates[dev_val_start], dev_dates[-1])

        trials_tested: list[dict[str, object]] = []
        best_params: dict[str, Any] | None = None
        best_dev_sharpe = -1e9

        for idx, raw_p in enumerate(candidates, start=1):
            merged_p = {**meta.default_params, **raw_p}
            try:
                fitted = strategy.fit(dev_train_store, merged_p)
                sigs = strategy.generate_signals(dev_full_store, merged_p, fitted)
                val_sigs = {d: sigs[d] for d in dev_val_store.dates if d in sigs}
                dev_sim = simulate_signal_frame(
                    dev_val_store,
                    val_sigs,
                    cost_model=self.cost_model,
                    initial_equity=self.initial_equity,
                    tax_profile=tax_profile,
                )
                dev_sharpe = float(dev_sim.metrics["sharpe_ratio"])
                trials_tested.append(
                    {
                        "trial": idx,
                        "params": merged_p,
                        "status": "ok",
                        "dev_val_sharpe": dev_sim.metrics["sharpe_ratio"],
                        "dev_val_return_pct": dev_sim.metrics["total_return_pct"],
                        "dev_val_trades": dev_sim.metrics["trades"],
                    }
                )
                if dev_sharpe > best_dev_sharpe:
                    best_dev_sharpe = dev_sharpe
                    best_params = merged_p
            except Exception as exc:
                trials_tested.append(
                    {
                        "trial": idx,
                        "params": merged_p,
                        "status": "failed",
                        "error": str(exc),
                    }
                )

        if best_params is None:
            err = "All candidate parameter trials failed during development evaluation"
            failed = ExperimentRecord(
                experiment_id=exp_id,
                hypothesis_id=hypothesis_id,
                alpha_id=aid,
                alpha_version=0,
                strategy_key=strategy_key,
                source_id=meta.source_id,
                dataset_id=store.bundle.dataset_id,
                dataset_kind=store.bundle.kind,
                status="failed",
                selected_params={},
                trial_count=len(candidates),
                trials_tested=trials_tested,
                verdict=None,
                max_corr_with_registry=None,
                compute_seconds=time.monotonic() - started,
                error=err,
                created_at=created_at,
            )
            self.experiments.append(failed)
            return failed

        # Compute full-series causal returns (fit on dev, evaluated across store) for registry/correlation checks
        fitted_best = strategy.fit(dev_full_store, best_params)
        full_signals = strategy.generate_signals(store, best_params, fitted_best)
        full_sim = simulate_signal_frame(
            store,
            full_signals,
            cost_model=self.cost_model,
            initial_equity=self.initial_equity,
            tax_profile=tax_profile,
        )

        # Evaluate correlation against existing alphas already in registry (excluding self)
        existing_returns = {
            other_id: rets
            for other_id, rets in self.registry.all_latest_returns().items()
            if other_id != aid and rets
        }
        max_corr: float | None = None
        if existing_returns:
            combined = {**existing_returns, aid: full_sim.daily_returns}
            corr_rep = compute_correlation_report(combined)
            idx_self = corr_rep.alpha_ids.index(aid)
            corrs = [
                abs(corr_rep.correlation_matrix[idx_self][j])
                for j in range(len(corr_rep.alpha_ids))
                if j != idx_self
            ]
            max_corr = max(corrs) if corrs else 0.0

        verdict = walk_forward_and_holdout_validate(
            strategy,
            store,
            best_params,
            trial_count=len(candidates),
            purge_days=purge_days,
            holdout_fraction=holdout_fraction,
            cost_model=self.cost_model,
            initial_equity=self.initial_equity,
            min_holdout_trades=min_holdout_trades,
            max_observed_corr=max_corr,
            tax_profile=tax_profile,
        )

        deployment_status = "candidate" if verdict.recommended_for_paper else "rejected"
        alpha_def = self.registry.register(
            alpha_id=aid,
            name=meta.name,
            strategy_key=strategy_key,
            source_id=meta.source_id,
            portfolio=meta.portfolio,
            parameters=best_params,
            deployment_status=deployment_status,  # type: ignore[arg-type]
            research_only=True,
            allow_inversion=False,
            metadata={
                "experiment_id": exp_id,
                "dataset_id": store.bundle.dataset_id,
                "dataset_kind": store.bundle.kind,
                "trial_count": len(candidates),
                "holdout_metrics": verdict.holdout_result.metrics,
                "regime_performance": verdict.holdout_result.regime_performance,
                "tax_impact": verdict.holdout_result.tax_impact,
            },
        )
        rows = [
            DailyAlphaReturn(
                alpha_id=alpha_def.alpha_id,
                version=alpha_def.version,
                trade_date=d,
                gross_return=full_sim.daily_gross_returns.get(d, ZERO),
                net_return=full_sim.daily_returns[d],
                turnover=full_sim.daily_turnovers.get(d, ZERO),
                regime=full_sim.regime_by_date.get(d, "chop"),
                is_synthetic=(store.bundle.kind == "synthetic"),
            )
            for d in sorted(full_sim.daily_returns)
        ]
        self.registry.record_daily_returns(alpha_def.alpha_id, alpha_def.version, rows)
        self.registry.record_approval(
            alpha_id=alpha_def.alpha_id,
            version=alpha_def.version,
            validation_status="recommended_for_paper" if verdict.recommended_for_paper else "rejected",
            paper_approved=False,
            approved_by=None,
            held_out_trades=verdict.held_out_trades,
            trial_count=verdict.trial_count,
            rejection_reasons=list(verdict.rejection_reasons),
            reason="Automated controlled research validation; human review required before paper approval.",
        )

        record = ExperimentRecord(
            experiment_id=exp_id,
            hypothesis_id=hypothesis_id,
            alpha_id=alpha_def.alpha_id,
            alpha_version=alpha_def.version,
            strategy_key=strategy_key,
            source_id=meta.source_id,
            dataset_id=store.bundle.dataset_id,
            dataset_kind=store.bundle.kind,
            status="passed" if verdict.recommended_for_paper else "rejected",
            selected_params=best_params,
            trial_count=len(candidates),
            trials_tested=trials_tested,
            verdict=verdict,
            max_corr_with_registry=max_corr,
            compute_seconds=time.monotonic() - started,
            error=None,
            created_at=created_at,
        )
        self.experiments.append(record)
        return record
