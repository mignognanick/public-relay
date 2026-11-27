"""Version-controlled, paper-only alpha registry and immutable return/approval storage."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Never

from research.library import canonical_hash, dec_str

DeploymentStatus = Literal[
    "research",
    "candidate",
    "paper_approved",
    "paper_enabled",
    "rejected",
    "retired",
]
PortfolioBucket = Literal["day", "swing", "long_term"]


def _never(value: Never) -> Never:
    raise RuntimeError(f"Unhandled variant: {value!r}")


@dataclass(frozen=True)
class AlphaDefinition:
    """Immutable versioned alpha definition. Execution is locked to PAPER."""

    alpha_id: str
    version: int
    name: str
    strategy_key: str
    source_id: str
    portfolio: PortfolioBucket
    parameters: Mapping[str, Any]
    spec_hash: str
    deployment_status: DeploymentStatus = "research"
    research_only: bool = True
    allow_inversion: bool = False
    execution: Literal["PAPER"] = "PAPER"
    approved_by: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.execution != "PAPER":
            raise ValueError("Quant Engine V1 alpha definitions are restricted to PAPER execution")
        if self.version < 1:
            raise ValueError("Alpha version must be >= 1")
        if self.deployment_status in ("paper_approved", "paper_enabled") and not self.approved_by:
            raise ValueError("Paper-approved or enabled alphas require an explicit human approver")
        if self.deployment_status == "paper_enabled" and self.research_only:
            raise ValueError("Research-only alphas cannot be marked paper_enabled")

    def to_dict(self) -> dict[str, object]:
        return {
            "alpha_id": self.alpha_id,
            "version": self.version,
            "name": self.name,
            "strategy_key": self.strategy_key,
            "source_id": self.source_id,
            "portfolio": self.portfolio,
            "parameters": dict(self.parameters),
            "spec_hash": self.spec_hash,
            "deployment_status": self.deployment_status,
            "research_only": self.research_only,
            "allow_inversion": self.allow_inversion,
            "execution": self.execution,
            "approved_by": self.approved_by,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class DailyAlphaReturn:
    alpha_id: str
    version: int
    trade_date: str
    gross_return: Decimal
    net_return: Decimal
    turnover: Decimal
    regime: str
    is_synthetic: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "alpha_id": self.alpha_id,
            "version": self.version,
            "trade_date": self.trade_date,
            "gross_return": dec_str(self.gross_return),
            "net_return": dec_str(self.net_return),
            "turnover": dec_str(self.turnover),
            "regime": self.regime,
            "is_synthetic": self.is_synthetic,
        }


class AlphaRegistry:
    """In-memory / serializable registry of versioned alphas and daily return series."""

    def __init__(self) -> None:
        self._versions: dict[tuple[str, int], AlphaDefinition] = {}
        self._returns: dict[tuple[str, int], dict[str, DailyAlphaReturn]] = {}
        self._approvals: list[dict[str, object]] = []
        self._allocations: list[dict[str, object]] = []

    def register(
        self,
        *,
        alpha_id: str,
        name: str,
        strategy_key: str,
        source_id: str,
        portfolio: PortfolioBucket,
        parameters: Mapping[str, Any],
        deployment_status: DeploymentStatus = "research",
        research_only: bool = True,
        allow_inversion: bool = False,
        approved_by: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> AlphaDefinition:
        existing_versions = [v for (aid, v) in self._versions if aid == alpha_id]
        next_ver = (max(existing_versions) + 1) if existing_versions else 1
        spec_hash = canonical_hash(
            {
                "alpha_id": alpha_id,
                "version": next_ver,
                "strategy_key": strategy_key,
                "source_id": source_id,
                "parameters": dict(parameters),
            }
        )
        definition = AlphaDefinition(
            alpha_id=alpha_id,
            version=next_ver,
            name=name,
            strategy_key=strategy_key,
            source_id=source_id,
            portfolio=portfolio,
            parameters=dict(parameters),
            spec_hash=spec_hash,
            deployment_status=deployment_status,
            research_only=research_only,
            allow_inversion=allow_inversion,
            approved_by=approved_by,
            metadata=dict(metadata or {}),
        )
        self._versions[(alpha_id, next_ver)] = definition
        return definition

    def latest(self, alpha_id: str) -> AlphaDefinition | None:
        versions = [v for (aid, v) in self._versions if aid == alpha_id]
        if not versions:
            return None
        return self._versions[(alpha_id, max(versions))]

    def list_latest(self) -> list[AlphaDefinition]:
        ids = sorted({aid for (aid, _) in self._versions})
        out: list[AlphaDefinition] = []
        for aid in ids:
            item = self.latest(aid)
            if item is not None:
                out.append(item)
        return out

    def record_daily_returns(
        self,
        alpha_id: str,
        version: int,
        rows: list[DailyAlphaReturn],
    ) -> None:
        if (alpha_id, version) not in self._versions:
            raise ValueError(f"Unknown alpha version ({alpha_id}, v{version})")
        bucket = self._returns.setdefault((alpha_id, version), {})
        for row in rows:
            bucket[row.trade_date] = row

    def daily_net_returns(self, alpha_id: str, version: int | None = None) -> dict[str, Decimal]:
        target_ver = version
        if target_ver is None:
            latest = self.latest(alpha_id)
            if latest is None:
                return {}
            target_ver = latest.version
        rows = self._returns.get((alpha_id, target_ver), {})
        return {d: rows[d].net_return for d in sorted(rows)}

    def all_latest_returns(self) -> dict[str, dict[str, Decimal]]:
        return {defn.alpha_id: self.daily_net_returns(defn.alpha_id, defn.version) for defn in self.list_latest()}

    def record_approval(
        self,
        *,
        alpha_id: str,
        version: int,
        validation_status: str,
        paper_approved: bool,
        approved_by: str | None,
        held_out_trades: int,
        trial_count: int,
        rejection_reasons: list[str],
        reason: str,
    ) -> dict[str, object]:
        if paper_approved and (not approved_by or held_out_trades < 30):
            raise ValueError("Paper approval requires a human approver and at least 30 held-out trades")
        record = {
            "alpha_id": alpha_id,
            "version": version,
            "execution": "PAPER",
            "validation_status": validation_status,
            "paper_approved": paper_approved,
            "approved_by": approved_by,
            "held_out_trades": held_out_trades,
            "trial_count": trial_count,
            "rejection_reasons": list(rejection_reasons),
            "reason": reason,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        self._approvals.append(record)
        return record

    def record_allocation(self, snapshot: Mapping[str, object]) -> None:
        self._allocations.append(dict(snapshot))

    @property
    def approvals(self) -> list[dict[str, object]]:
        return list(self._approvals)

    @property
    def allocation_history(self) -> list[dict[str, object]]:
        return list(self._allocations)
