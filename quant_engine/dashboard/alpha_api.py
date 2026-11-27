"""Authenticated Alpha Center API for research queue, correlation, allocation, and ensembles.

Strictly paper-only and research-only:
- Never places brokerage orders or modifies production credentials.
- Never auto-promotes or enables strategies; paper permissions change only through /api/commands.
- Preserves independent RiskEngine defaults, manual regime overrides, portfolios, and Tax Center.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from alpha.allocator import AllocationConstraints, AllocationModel, compute_allocation
from alpha.correlation import compute_correlation_report
from alpha.ensemble import compare_allocation_models, evaluate_ensemble
from alpha.registry import AlphaRegistry, DailyAlphaReturn
from quant_engine.dashboard.alpha_store import AlphaStore
from research.feature_store import (
    FeatureStore,
    generate_synthetic_us_equity_dataset,
    parse_daily_ohlcv_csv,
)
from research.library import ACADEMIC_SOURCES, ZERO, dec_from_float, dec_str
from research.orchestrator import (
    ResearchBudget,
    ResearchHypothesis,
    ResearchOrchestrator,
    default_seed_hypotheses,
)
from research.strategy_catalog import STRATEGY_CATALOG, list_catalog
from research.validator import _compute_benchmark_and_regimes


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RunCycleRequest(StrictModel):
    max_steps: int = Field(default=5, ge=1, le=5, strict=True)
    max_trials_per_hypothesis: int = Field(default=2, ge=1, le=10, strict=True)
    max_compute_seconds: float = Field(default=25.0, gt=0.5, le=120.0)
    max_ai_tokens: int = Field(default=50000, ge=0, le=500000, strict=True)
    max_ai_cost_usd: Decimal = Field(default=Decimal("2.00"), ge=0, le=Decimal("100.00"), decimal_places=2)
    dataset_mode: Literal["synthetic_demo", "csv"] = "synthetic_demo"
    csv: str = Field(default="", max_length=3_000_000)
    csv_source: str = Field(default="User-supplied daily OHLCV CSV", max_length=300)
    csv_kind: Literal["historical", "synthetic"] = "historical"


class AllocationPreviewRequest(StrictModel):
    model: AllocationModel
    max_weight: Decimal = Field(default=Decimal("0.50"), gt=0, le=1, decimal_places=2)
    max_turnover: Decimal = Field(default=Decimal("0.50"), gt=0, le=2, decimal_places=2)
    shrinkage: float = Field(default=0.25, ge=0.0, le=1.0)
    reason: str = Field(default="Research allocation preview", min_length=3, max_length=500)


class BudgetUpdateRequest(StrictModel):
    max_experiments: int = Field(default=5, ge=1, le=50, strict=True)
    max_trials_per_hypothesis: int = Field(default=3, ge=1, le=20, strict=True)
    max_compute_seconds: float = Field(default=30.0, gt=1.0, le=300.0)
    max_ai_tokens: int = Field(default=50000, ge=0, le=1_000_000, strict=True)
    max_ai_cost_usd: Decimal = Field(default=Decimal("2.00"), ge=0, le=Decimal("250.00"), decimal_places=2)


def _load_bundle_state(alpha_store: AlphaStore, who: tuple[str, str]) -> dict[str, object]:
    """Assemble persisted Alpha Center state for `who`."""
    saved_budget = alpha_store.get(*who, "budget", "default")
    budget = (
        saved_budget["payload"]
        if saved_budget
        else ResearchBudget().to_dict()
    )
    saved_usage = alpha_store.get(*who, "budget_usage", "default")
    usage = (
        saved_usage["payload"]
        if saved_usage
        else {
            "experiments_run": 0,
            "trials_run": 0,
            "compute_seconds": 0.0,
            "ai_tokens_used": 0,
            "ai_cost_usd": "0.000000",
        }
    )

    hyp_records = alpha_store.list(*who, "hypothesis", limit=100)
    if hyp_records:
        queue = [r["payload"] for r in hyp_records]
    else:
        queue = [h.to_dict() for h in default_seed_hypotheses()]

    alpha_records = [r["payload"] for r in alpha_store.list(*who, "alpha", limit=100)]
    experiments = [r["payload"] for r in alpha_store.list(*who, "experiment", limit=100)]
    allocations = [r["payload"] for r in alpha_store.list(*who, "allocation", limit=50)]
    returns_blob = alpha_store.get(*who, "return_matrix", "latest")
    benchmark_blob = alpha_store.get(*who, "benchmark_returns", "latest")
    regime_blob = alpha_store.get(*who, "regime_by_date", "latest")

    raw_returns: dict[str, dict[str, Decimal]] = {}
    if returns_blob:
        for aid, series in returns_blob["payload"].items():
            raw_returns[aid] = {d: Decimal(str(v)) for d, v in series.items()}

    bench_returns: dict[str, Decimal] | None = None
    if benchmark_blob:
        bench_returns = {d: Decimal(str(v)) for d, v in benchmark_blob["payload"].items()}

    regime_by_date: dict[str, str] | None = None
    if regime_blob:
        regime_by_date = dict(regime_blob["payload"])

    return {
        "budget": budget,
        "usage": usage,
        "queue": queue,
        "alphas": alpha_records,
        "experiments": experiments,
        "allocations": allocations,
        "raw_returns": raw_returns,
        "benchmark_returns": bench_returns,
        "regime_by_date": regime_by_date,
    }


def _build_overview_payload(
    database,
    alpha_store: AlphaStore,
    who: tuple[str, str],
) -> dict[str, object]:
    try:
        state, version = database.read(*who)
    except ValidationError:
        raise HTTPException(503, "Stored configuration is invalid") from None

    bundle = _load_bundle_state(alpha_store, who)
    raw_returns: dict[str, dict[str, Decimal]] = bundle["raw_returns"]  # type: ignore[assignment]
    bench_returns: dict[str, Decimal] | None = bundle["benchmark_returns"]  # type: ignore[assignment]
    regime_by_date: dict[str, str] | None = bundle["regime_by_date"]  # type: ignore[assignment]
    experiments: list[dict[str, object]] = bundle["experiments"]  # type: ignore[assignment]
    allocations: list[dict[str, object]] = bundle["allocations"]  # type: ignore[assignment]

    selected_model: AllocationModel = state.allocation_model
    corr_report = compute_correlation_report(raw_returns) if raw_returns else compute_correlation_report({})

    ensemble_comparison = (
        compare_allocation_models(
            raw_returns,
            lookback_days=30,
            rebalance_every=5,
            benchmark_returns=bench_returns,
            regime_by_date=regime_by_date,
        )
        if raw_returns
        else {}
    )

    active_ensemble = ensemble_comparison.get(selected_model)
    proposed_allocations = {
        m: rep["latest_allocation"] for m, rep in ensemble_comparison.items()
    }
    current_allocation = allocations[0] if allocations else (
        proposed_allocations.get(selected_model) if proposed_allocations else None
    )

    # Aggregate regime performance across registered alphas and selected ensemble
    regime_matrix: dict[str, dict[str, object]] = {}
    for exp in experiments:
        verdict = exp.get("verdict")
        if isinstance(verdict, dict):
            holdout = verdict.get("holdout", {})
            reg_perf = holdout.get("regime_performance", {}) if isinstance(holdout, dict) else {}
            if reg_perf:
                regime_matrix[str(exp.get("alpha_id", exp.get("strategy_key")))] = reg_perf
    if active_ensemble and active_ensemble.get("regime_performance"):
        regime_matrix[f"ensemble_{selected_model}"] = active_ensemble["regime_performance"]

    failed_or_rejected = [
        {
            "experiment_id": e.get("experiment_id"),
            "alpha_id": e.get("alpha_id"),
            "strategy_key": e.get("strategy_key"),
            "status": e.get("status"),
            "dataset_kind": e.get("dataset_kind"),
            "trial_count": e.get("trial_count"),
            "rejection_reasons": e.get("rejection_reasons", []),
            "created_at": e.get("created_at"),
        }
        for e in experiments
        if e.get("status") in ("failed", "rejected")
    ]

    paper_strategies = [
        {
            "id": s.id,
            "name": s.name,
            "portfolio": s.portfolio,
            "stage": s.stage,
            "enabled": s.enabled,
            "regimes": list(s.regimes),
            "approved_by": s.approved_by,
        }
        for s in state.strategies
        if s.stage == "paper" and s.approved_by
    ]

    return {
        "execution": "PAPER",
        "execution_active": False,
        "configuration_version": version,
        "selected_model": selected_model,
        "catalog": list_catalog(),
        "academic_sources": [s.to_dict() for s in ACADEMIC_SOURCES.values()],
        "alphas": bundle["alphas"],
        "queue": bundle["queue"],
        "experiments": experiments,
        "failed_experiments": failed_or_rejected,
        "correlation": corr_report.to_dict(),
        "ensemble_comparison": ensemble_comparison,
        "selected_ensemble": active_ensemble,
        "current_allocation": current_allocation,
        "proposed_allocations": proposed_allocations,
        "allocation_history": allocations,
        "strategy_contributions": (active_ensemble.get("strategy_contributions", {}) if active_ensemble else {}),
        "regime_performance": regime_matrix,
        "budget": bundle["budget"],
        "usage": bundle["usage"],
        "paper_strategies": paper_strategies,
        "notice": (
            "Research & paper-only Alpha Center. No live execution, no automatic promotion. "
            "V3 Kakushadze-Yu is strictly research-only."
        ),
    }


def router(database, identity):
    api = APIRouter(prefix="/api/alpha")
    alpha_store = AlphaStore(database) if database is not None else None

    @api.get("/overview")
    def overview(who=Depends(identity)):
        if alpha_store is None:
            raise HTTPException(503, "Alpha storage unavailable")
        return _build_overview_payload(database, alpha_store, who)

    @api.post("/budget")
    def update_budget(body: BudgetUpdateRequest, who=Depends(identity)):
        if alpha_store is None:
            raise HTTPException(503, "Alpha storage unavailable")
        budget = ResearchBudget(
            max_experiments=body.max_experiments,
            max_trials_per_hypothesis=body.max_trials_per_hypothesis,
            max_compute_seconds=body.max_compute_seconds,
            max_ai_tokens=body.max_ai_tokens,
            max_ai_cost_usd=body.max_ai_cost_usd,
        )
        alpha_store.upsert_local_or_append(*who, "budget", "default", budget.to_dict())
        return {"budget": budget.to_dict()}

    @api.post("/run-cycle")
    def run_cycle(body: RunCycleRequest, who=Depends(identity)):
        if alpha_store is None:
            raise HTTPException(503, "Alpha storage unavailable")
        try:
            state, _ = database.read(*who)
        except ValidationError:
            raise HTTPException(503, "Stored configuration is invalid") from None

        if body.dataset_mode == "csv":
            if not body.csv.strip():
                raise HTTPException(422, "Provide CSV content when dataset_mode is 'csv'")
            try:
                bars_by_sym = parse_daily_ohlcv_csv(body.csv)
                store = FeatureStore.from_bars(
                    bars_by_sym,
                    kind=body.csv_kind,
                    source=body.csv_source,
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
        else:
            store = generate_synthetic_us_equity_dataset(n_days=220, seed=42)

        budget = ResearchBudget(
            max_experiments=body.max_steps,
            max_trials_per_hypothesis=body.max_trials_per_hypothesis,
            max_compute_seconds=body.max_compute_seconds,
            max_ai_tokens=body.max_ai_tokens,
            max_ai_cost_usd=body.max_ai_cost_usd,
        )
        registry = AlphaRegistry()
        orch = ResearchOrchestrator(registry=registry, budget=budget)
        cycle = orch.run_cycle(store, max_steps=body.max_steps, tax_profile=state.tax)

        # Persist hypotheses, alpha definitions, experiments, daily returns, correlation, and allocation
        for hyp in orch.queue:
            alpha_store.upsert_local_or_append(*who, "hypothesis", hyp.hypothesis_id, hyp.to_dict())
        for alpha_def in registry.list_latest():
            alpha_store.upsert_local_or_append(
                *who, "alpha", f"{alpha_def.alpha_id}:v{alpha_def.version}", alpha_def.to_dict()
            )
        for exp in orch.runner.experiments:
            alpha_store.upsert_local_or_append(*who, "experiment", exp.experiment_id, exp.to_dict())

        all_returns = registry.all_latest_returns()
        serialized_returns = {
            aid: {d: dec_str(v) for d, v in series.items()}
            for aid, series in all_returns.items()
            if series
        }
        alpha_store.upsert_local_or_append(*who, "return_matrix", "latest", serialized_returns)

        # Persist benchmark & regime tags from first completed experiment if available
        first_verdict_exp = next((e for e in orch.runner.experiments if e.verdict is not None), None)
        if first_verdict_exp and first_verdict_exp.verdict is not None:
            full_bench, full_regimes = _compute_benchmark_and_regimes(store)
            alpha_store.upsert_local_or_append(
                *who,
                "benchmark_returns",
                "latest",
                {d: dec_str(v) for d, v in full_bench.items()},
            )
            alpha_store.upsert_local_or_append(*who, "regime_by_date", "latest", full_regimes)

        if all_returns:
            corr = compute_correlation_report(all_returns)
            alpha_store.upsert_local_or_append(
                *who,
                "correlation",
                f"corr-{uuid4().hex[:10]}",
                {"as_of_date": store.dates[-1], **corr.to_dict()},
            )
            alloc = compute_allocation(state.allocation_model, all_returns)
            alpha_store.upsert_local_or_append(
                *who,
                "allocation",
                f"alloc-{uuid4().hex[:10]}",
                {
                    "as_of_date": store.dates[-1],
                    "reason": f"Research cycle allocation ({state.allocation_model})",
                    **alloc.to_dict(),
                },
            )

        alpha_store.upsert_local_or_append(*who, "budget", "default", budget.to_dict())
        alpha_store.upsert_local_or_append(*who, "budget_usage", "default", cycle["usage"])
        return {
            "cycle": {
                "stop_reason": cycle["stop_reason"],
                "processed": cycle["processed"],
                "usage": cycle["usage"],
            },
            "overview": _build_overview_payload(database, alpha_store, who),
        }

    @api.post("/preview-allocation")
    def preview_allocation(body: AllocationPreviewRequest, who=Depends(identity)):
        if alpha_store is None:
            raise HTTPException(503, "Alpha storage unavailable")
        bundle = _load_bundle_state(alpha_store, who)
        raw_returns: dict[str, dict[str, Decimal]] = bundle["raw_returns"]  # type: ignore[assignment]
        if not raw_returns:
            raise HTTPException(422, "Run at least one research cycle before previewing alpha allocations")

        prev_allocs = bundle["allocations"]
        prev_weights = None
        if prev_allocs and isinstance(prev_allocs[0], dict) and isinstance(prev_allocs[0].get("weights"), dict):
            prev_weights = {k: Decimal(str(v)) for k, v in prev_allocs[0]["weights"].items()}

        constraints = AllocationConstraints(
            max_weight=body.max_weight,
            max_turnover=body.max_turnover,
            shrinkage=body.shrinkage,
        )
        result = compute_allocation(
            body.model,
            raw_returns,
            constraints=constraints,
            previous_weights=prev_weights,
        )
        alloc_id = f"alloc-{uuid4().hex[:10]}"
        snapshot = {
            "allocation_id": alloc_id,
            "as_of_date": datetime.now(timezone.utc).date().isoformat(),
            "reason": body.reason,
            **result.to_dict(),
        }
        alpha_store.upsert_local_or_append(*who, "allocation", alloc_id, snapshot)
        return {
            "allocation": snapshot,
            "ensemble": evaluate_ensemble(
                body.model,
                raw_returns,
                constraints=constraints,
                lookback_days=30,
                rebalance_every=5,
                benchmark_returns=bundle["benchmark_returns"],  # type: ignore[arg-type]
                regime_by_date=bundle["regime_by_date"],  # type: ignore[arg-type]
            ).to_dict(),
        }

    return api
