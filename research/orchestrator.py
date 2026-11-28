"""Continuous AI research orchestrator with strict compute/AI usage budgets and paper-only governance.

Implements the 7-step autonomous research loop:
1. Select a research hypothesis from the queue.
2. Verify the original formula against the academic source catalog.
3. Implement/bind and smoke-test the strategy (including look-ahead audit).
4. Run controlled parameter experiments strictly on development data.
5. Conduct purged walk-forward and independent holdout validation.
6. Evaluate transaction costs, portfolio correlations, and tax impact.
7. Submit a paper-trading recommendation OR reject the hypothesis with reasons.

Hard safety guarantees:
- Never promotes to live trading and never auto-enables paper trading.
- Never places brokerage orders or modifies production credentials.
- Fails closed when compute, experiment, trial, or AI usage budgets are exhausted.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Sequence

from alpha.registry import AlphaRegistry
from quant_engine.dashboard.domain import Tax
from research.experiment_runner import ExperimentRecord, ExperimentRunner
from research.feature_store import FeatureStore
from research.library import ACADEMIC_SOURCES, dec_str
from research.strategy_catalog import STRATEGY_CATALOG, get_strategy
from research.validator import ExecutionCostModel, audit_causality

HypothesisStatus = Literal["queued", "running", "recommended_for_paper", "rejected", "archived"]


@dataclass(frozen=True)
class ResearchBudget:
    """Configurable guardrails for autonomous research compute and AI usage."""

    max_experiments: int = 5
    max_trials_per_hypothesis: int = 6
    max_compute_seconds: float = 30.0
    max_ai_tokens: int = 50_000
    max_ai_cost_usd: Decimal = Decimal("2.00")

    def __post_init__(self) -> None:
        if self.max_experiments < 1:
            raise ValueError("max_experiments must be >= 1")
        if self.max_trials_per_hypothesis < 1:
            raise ValueError("max_trials_per_hypothesis must be >= 1")
        if self.max_compute_seconds <= 0:
            raise ValueError("max_compute_seconds must be > 0")
        if self.max_ai_tokens < 0:
            raise ValueError("max_ai_tokens must be >= 0")
        if self.max_ai_cost_usd < Decimal("0"):
            raise ValueError("max_ai_cost_usd must be >= 0")

    def to_dict(self) -> dict[str, object]:
        return {
            "max_experiments": self.max_experiments,
            "max_trials_per_hypothesis": self.max_trials_per_hypothesis,
            "max_compute_seconds": self.max_compute_seconds,
            "max_ai_tokens": self.max_ai_tokens,
            "max_ai_cost_usd": str(self.max_ai_cost_usd),
        }


@dataclass
class BudgetUsage:
    experiments_run: int = 0
    trials_run: int = 0
    compute_seconds: float = 0.0
    ai_tokens_used: int = 0
    ai_cost_usd: Decimal = Decimal("0.00")

    def exhausted_reason(self, budget: ResearchBudget) -> str | None:
        if self.experiments_run >= budget.max_experiments:
            return f"Experiment budget exhausted ({self.experiments_run}/{budget.max_experiments})"
        if self.compute_seconds >= budget.max_compute_seconds:
            return f"Compute time budget exhausted ({self.compute_seconds:.2f}s/{budget.max_compute_seconds:.2f}s)"
        if self.ai_tokens_used > budget.max_ai_tokens:
            return f"AI token budget exhausted ({self.ai_tokens_used}/{budget.max_ai_tokens})"
        if self.ai_cost_usd > budget.max_ai_cost_usd:
            return f"AI spend budget exhausted (${self.ai_cost_usd}/${budget.max_ai_cost_usd})"
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "experiments_run": self.experiments_run,
            "trials_run": self.trials_run,
            "compute_seconds": round(self.compute_seconds, 4),
            "ai_tokens_used": self.ai_tokens_used,
            "ai_cost_usd": dec_str(self.ai_cost_usd),
        }


@dataclass
class ResearchHypothesis:
    hypothesis_id: str
    strategy_key: str
    source_id: str
    title: str
    thesis: str
    portfolio: Literal["day", "swing", "long_term"]
    param_grid: list[dict[str, Any]] = field(default_factory=list)
    status: HypothesisStatus = "queued"
    formula_verified: bool = False
    causality_verified: bool = False
    experiment_id: str | None = None
    rejection_reasons: list[str] = field(default_factory=list)
    step_log: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, object]:
        return {
            "hypothesis_id": self.hypothesis_id,
            "strategy_key": self.strategy_key,
            "source_id": self.source_id,
            "title": self.title,
            "thesis": self.thesis,
            "portfolio": self.portfolio,
            "param_grid": list(self.param_grid),
            "status": self.status,
            "formula_verified": self.formula_verified,
            "causality_verified": self.causality_verified,
            "experiment_id": self.experiment_id,
            "rejection_reasons": list(self.rejection_reasons),
            "step_log": list(self.step_log),
            "created_at": self.created_at,
        }


def default_seed_hypotheses() -> list[ResearchHypothesis]:
    """Build initial queued research hypotheses for the five 151 Trading Strategies modules."""
    out: list[ResearchHypothesis] = []
    for key, strat in STRATEGY_CATALOG.items():
        meta = strat.metadata
        out.append(
            ResearchHypothesis(
                hypothesis_id=f"hyp-{key}",
                strategy_key=key,
                source_id=meta.source_id,
                title=meta.name,
                thesis=meta.description,
                portfolio=meta.portfolio,
                param_grid=[dict(p) for p in meta.param_grid],
            )
        )
    return out


class ResearchOrchestrator:
    """Autonomous paper-only research loop obeying strict verification and usage budgets."""

    def __init__(
        self,
        *,
        registry: AlphaRegistry | None = None,
        runner: ExperimentRunner | None = None,
        budget: ResearchBudget | None = None,
        hypotheses: Sequence[ResearchHypothesis] | None = None,
        cost_model: ExecutionCostModel | None = None,
    ) -> None:
        self.registry = registry or (runner.registry if runner else AlphaRegistry())
        self.runner = runner or ExperimentRunner(registry=self.registry, cost_model=cost_model)
        self.budget = budget or ResearchBudget()
        self.usage = BudgetUsage()
        self.queue: list[ResearchHypothesis] = list(hypotheses) if hypotheses is not None else default_seed_hypotheses()
        self.recommendations: list[dict[str, object]] = []
        self.budget_events: list[dict[str, object]] = []

    def enqueue(self, hypothesis: ResearchHypothesis) -> None:
        if any(h.hypothesis_id == hypothesis.hypothesis_id for h in self.queue):
            raise ValueError(f"Duplicate hypothesis_id: {hypothesis.hypothesis_id}")
        self.queue.append(hypothesis)

    def _charge_ai_advisory(self, tokens: int, cost_usd: Decimal, note: str) -> str | None:
        self.usage.ai_tokens_used += tokens
        self.usage.ai_cost_usd += cost_usd
        self.budget_events.append(
            {
                "note": note,
                "tokens": tokens,
                "cost_usd": str(cost_usd),
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        return self.usage.exhausted_reason(self.budget)

    def _verify_formula(self, hyp: ResearchHypothesis) -> tuple[bool, str]:
        source = ACADEMIC_SOURCES.get(hyp.source_id)
        if source is None:
            return False, f"Academic source {hyp.source_id!r} not found in verified library"
        if not source.equation_numbers or not source.formula_summary or not source.adaptations_for_us_equities:
            return False, f"Incomplete formula verification metadata for {hyp.source_id!r}"
        return True, f"Verified Section {source.section} Eqs. ({', '.join(source.equation_numbers)})"

    def run_next(
        self,
        store: FeatureStore,
        *,
        min_holdout_trades: int = 30,
        tax_profile: Tax | None = None,
        advisory_tokens: int = 0,
        advisory_cost_usd: Decimal = Decimal("0.00"),
    ) -> dict[str, object]:
        """Execute the full 7-step research pipeline on the next queued hypothesis."""
        stopped_reason = self.usage.exhausted_reason(self.budget)
        if stopped_reason:
            return {"status": "budget_exhausted", "reason": stopped_reason, "usage": self.usage.to_dict()}

        # Step 1: Select next queued hypothesis
        hyp = next((h for h in self.queue if h.status == "queued"), None)
        if hyp is None:
            return {"status": "queue_empty", "usage": self.usage.to_dict()}

        hyp.status = "running"
        hyp.step_log.append("1. Selected hypothesis from research queue")
        t0 = time.monotonic()

        if advisory_tokens > 0 or advisory_cost_usd > Decimal("0"):
            budget_hit = self._charge_ai_advisory(advisory_tokens, advisory_cost_usd, f"hypothesis:{hyp.hypothesis_id}")
            if budget_hit:
                hyp.status = "rejected"
                hyp.rejection_reasons.append(budget_hit)
                return {"status": "budget_exhausted", "hypothesis": hyp.to_dict(), "reason": budget_hit}

        # Step 2: Verify original formula against academic source catalog
        formula_ok, formula_msg = self._verify_formula(hyp)
        hyp.formula_verified = formula_ok
        hyp.step_log.append(f"2. Formula verification: {formula_msg}")
        if not formula_ok:
            hyp.status = "rejected"
            hyp.rejection_reasons.append(formula_msg)
            self.usage.compute_seconds += time.monotonic() - t0
            return {"status": "rejected", "hypothesis": hyp.to_dict(), "usage": self.usage.to_dict()}

        # Step 3: Bind strategy module & run smoke causality audit
        try:
            strategy = get_strategy(hyp.strategy_key)
        except ValueError as exc:
            hyp.status = "rejected"
            hyp.rejection_reasons.append(str(exc))
            hyp.step_log.append(f"3. Strategy binding failed: {exc}")
            self.usage.compute_seconds += time.monotonic() - t0
            return {"status": "rejected", "hypothesis": hyp.to_dict(), "usage": self.usage.to_dict()}

        causality_ok, causality_errs = audit_causality(strategy, store, strategy.metadata.default_params)
        hyp.causality_verified = causality_ok
        hyp.step_log.append(
            "3. Strategy bound & causality audit passed"
            if causality_ok
            else f"3. Causality audit FAILED: {'; '.join(causality_errs)}"
        )
        if not causality_ok:
            hyp.status = "rejected"
            hyp.rejection_reasons.extend(causality_errs)
            self.usage.compute_seconds += time.monotonic() - t0
            return {"status": "rejected", "hypothesis": hyp.to_dict(), "usage": self.usage.to_dict()}

        # Steps 4, 5, 6: Controlled parameter experiments, walk-forward & holdout, costs/correlations/taxes
        hyp.step_log.append("4-6. Running controlled dev trials, walk-forward/holdout validation, cost/corr/tax analysis")
        record: ExperimentRecord = self.runner.run_controlled_experiment(
            hyp.strategy_key,
            store,
            param_grid=hyp.param_grid or None,
            hypothesis_id=hyp.hypothesis_id,
            max_trials=self.budget.max_trials_per_hypothesis,
            min_holdout_trades=min_holdout_trades,
            tax_profile=tax_profile,
        )
        elapsed = time.monotonic() - t0
        self.usage.experiments_run += 1
        self.usage.trials_run += record.trial_count
        self.usage.compute_seconds += elapsed
        hyp.experiment_id = record.experiment_id

        # Step 7: Submit paper-trading recommendation OR reject hypothesis (never auto-promote/enable)
        if record.verdict and record.verdict.recommended_for_paper:
            hyp.status = "recommended_for_paper"
            hyp.step_log.append("7. Submitted paper-trading recommendation (awaiting human approval)")
            recommendation = {
                "hypothesis_id": hyp.hypothesis_id,
                "experiment_id": record.experiment_id,
                "alpha_id": record.alpha_id,
                "alpha_version": record.alpha_version,
                "execution": "PAPER",
                "auto_promoted": False,
                "requires_human_approval": True,
                "holdout_metrics": record.verdict.holdout_result.metrics,
                "tax_impact": record.verdict.holdout_result.tax_impact,
                "max_corr_with_registry": record.max_corr_with_registry,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            self.recommendations.append(recommendation)
        else:
            hyp.status = "rejected"
            reasons = list(record.verdict.rejection_reasons) if record.verdict else [record.error or "Validation failed"]
            hyp.rejection_reasons.extend(reasons)
            hyp.step_log.append(f"7. Hypothesis rejected ({len(reasons)} reasons)")

        return {
            "status": hyp.status,
            "hypothesis": hyp.to_dict(),
            "experiment": record.to_dict(),
            "usage": self.usage.to_dict(),
        }

    def run_cycle(
        self,
        store: FeatureStore,
        *,
        max_steps: int | None = None,
        min_holdout_trades: int = 30,
        tax_profile: Tax | None = None,
    ) -> dict[str, object]:
        """Run queued hypotheses until queue is drained, max_steps reached, or budget exhausted."""
        limit = max_steps if max_steps is not None else self.budget.max_experiments
        outcomes: list[dict[str, object]] = []
        stop_reason = "completed"
        for _ in range(max(1, limit)):
            res = self.run_next(
                store,
                min_holdout_trades=min_holdout_trades,
                tax_profile=tax_profile,
            )
            if res["status"] in ("budget_exhausted", "queue_empty"):
                stop_reason = str(res["status"])
                break
            outcomes.append(res)
        return {
            "stop_reason": stop_reason,
            "processed": len(outcomes),
            "outcomes": outcomes,
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "recommendations": list(self.recommendations),
        }

    def snapshot(self) -> dict[str, object]:
        return {
            "budget": self.budget.to_dict(),
            "usage": self.usage.to_dict(),
            "queue": [h.to_dict() for h in self.queue],
            "experiments": [e.to_dict() for e in self.runner.experiments],
            "recommendations": list(self.recommendations),
            "failed_or_rejected": [
                e.to_dict() for e in self.runner.experiments if e.status in ("failed", "rejected")
            ],
        }
