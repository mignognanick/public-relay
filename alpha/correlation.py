"""Pairwise strategy correlation, covariance regularization, and singularity diagnostics."""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping, Sequence

from research.library import (
    TRADING_DAYS_PER_YEAR,
    dec_from_float,
    dec_str,
    jacobi_eigen_symmetric,
    sample_mean,
    sample_std,
)


@dataclass(frozen=True)
class CorrelationReport:
    alpha_ids: tuple[str, ...]
    dates_covered: tuple[str, ...]
    correlation_matrix: list[list[float]]
    raw_covariance: list[list[float]]
    regularized_covariance: list[list[float]]
    daily_volatilities: dict[str, float]
    annualized_volatilities: dict[str, float]
    zero_variance_alphas: tuple[str, ...]
    insufficient_overlap_pairs: tuple[tuple[str, str], ...]
    shrinkage: float
    condition_number: float | None
    is_singular: bool
    effective_independent_alphas: float
    avg_abs_pairwise_correlation: float

    def to_dict(self) -> dict[str, object]:
        return {
            "alpha_ids": list(self.alpha_ids),
            "dates_covered": list(self.dates_covered),
            "observations": len(self.dates_covered),
            "correlation_matrix": [[round(v, 6) for v in row] for row in self.correlation_matrix],
            "daily_volatilities": {k: dec_str(dec_from_float(v, "vol")) for k, v in self.daily_volatilities.items()},
            "annualized_volatilities": {
                k: dec_str(dec_from_float(v, "ann_vol")) for k, v in self.annualized_volatilities.items()
            },
            "zero_variance_alphas": list(self.zero_variance_alphas),
            "insufficient_overlap_pairs": [list(p) for p in self.insufficient_overlap_pairs],
            "shrinkage": self.shrinkage,
            "condition_number": round(self.condition_number, 4) if self.condition_number is not None else None,
            "is_singular": self.is_singular,
            "effective_independent_alphas": round(self.effective_independent_alphas, 4),
            "avg_abs_pairwise_correlation": round(self.avg_abs_pairwise_correlation, 4),
        }


def _clean_float(val: Decimal | float) -> float:
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"Non-finite alpha return encountered: {val!r}")
    return f


def compute_correlation_report(
    alpha_returns: Mapping[str, Mapping[str, Decimal]],
    *,
    min_overlap: int = 5,
    shrinkage: float = 0.25,
    ridge: float = 1e-8,
    alpha_order: Sequence[str] | None = None,
) -> CorrelationReport:
    """Compute pairwise correlation and shrinkage-regularized covariance safely.

    Explicitly handles missing dates, zero-variance alphas, and singular/ill-conditioned
    covariance matrices without raising unhandled linear algebra errors.
    """
    if not (0.0 <= shrinkage <= 1.0):
        raise ValueError("shrinkage must be between 0.0 and 1.0")
    ids = tuple(alpha_order) if alpha_order is not None else tuple(sorted(alpha_returns))
    n = len(ids)
    if n == 0:
        return CorrelationReport(
            alpha_ids=(),
            dates_covered=(),
            correlation_matrix=[],
            raw_covariance=[],
            regularized_covariance=[],
            daily_volatilities={},
            annualized_volatilities={},
            zero_variance_alphas=(),
            insufficient_overlap_pairs=(),
            shrinkage=shrinkage,
            condition_number=None,
            is_singular=False,
            effective_independent_alphas=0.0,
            avg_abs_pairwise_correlation=0.0,
        )

    cleaned: dict[str, dict[str, float]] = {}
    all_dates: set[str] = set()
    for aid in ids:
        series = alpha_returns.get(aid, {})
        cleaned[aid] = {d: _clean_float(v) for d, v in series.items()}
        all_dates.update(cleaned[aid].keys())

    dates_sorted = tuple(sorted(all_dates))
    daily_vols: dict[str, float] = {}
    ann_vols: dict[str, float] = {}
    zero_var: list[str] = []

    for aid in ids:
        vals = [cleaned[aid][d] for d in sorted(cleaned[aid])]
        vol = sample_std(vals, ddof=1) if len(vals) >= 2 else 0.0
        daily_vols[aid] = vol
        ann_vols[aid] = vol * math.sqrt(TRADING_DAYS_PER_YEAR)
        if vol <= 1e-12 or len(vals) < min_overlap:
            zero_var.append(aid)

    corr = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    cov = [[0.0 for _ in range(n)] for _ in range(n)]
    insufficient_pairs: list[tuple[str, str]] = []

    for i in range(n):
        ai = ids[i]
        vi = daily_vols[ai]
        cov[i][i] = vi * vi
        for j in range(i + 1, n):
            aj = ids[j]
            common = sorted(set(cleaned[ai]).intersection(cleaned[aj]))
            if len(common) < min_overlap:
                insufficient_pairs.append((ai, aj))
                corr[i][j] = corr[j][i] = 0.0
                cov[i][j] = cov[j][i] = 0.0
                continue
            xi = [cleaned[ai][d] for d in common]
            xj = [cleaned[aj][d] for d in common]
            si = sample_std(xi, ddof=1)
            sj = sample_std(xj, ddof=1)
            if si <= 1e-12 or sj <= 1e-12:
                corr[i][j] = corr[j][i] = 0.0
                cov[i][j] = cov[j][i] = 0.0
                continue
            mi = sample_mean(xi)
            mj = sample_mean(xj)
            c_ij = sum((xi[k] - mi) * (xj[k] - mj) for k in range(len(common))) / (len(common) - 1)
            rho = max(-1.0, min(1.0, c_ij / (si * sj)))
            corr[i][j] = corr[j][i] = rho
            cov[i][j] = cov[j][i] = c_ij

    # Diagonal shrinkage regularization + ridge ensures SPD even when T <= N+1 or alphas are duplicates.
    reg_cov = [
        [
            cov[i][i] + ridge if i == j else (1.0 - shrinkage) * cov[i][j]
            for j in range(n)
        ]
        for i in range(n)
    ]

    # Eigen diagnostics on raw covariance (singularity) and correlation matrix (effective bets).
    raw_evals, _ = jacobi_eigen_symmetric(cov)
    max_ev = max(raw_evals) if raw_evals else 0.0
    min_ev = min(raw_evals) if raw_evals else 0.0
    is_singular = bool(zero_var) or min_ev <= 1e-12 or (max_ev > 0.0 and max_ev / max(min_ev, 1e-15) > 1e8)
    cond = (max_ev / min_ev) if min_ev > 1e-12 else None

    corr_evals, _ = jacobi_eigen_symmetric(corr)
    pos_evals = [max(0.0, ev) for ev in corr_evals]
    sum_sq = sum(ev * ev for ev in pos_evals)
    n_eff = ((sum(pos_evals) ** 2) / sum_sq) if sum_sq > 1e-12 else 0.0

    off_diag = [abs(corr[i][j]) for i in range(n) for j in range(i + 1, n)]
    avg_abs_corr = (sum(off_diag) / len(off_diag)) if off_diag else 0.0

    return CorrelationReport(
        alpha_ids=ids,
        dates_covered=dates_sorted,
        correlation_matrix=corr,
        raw_covariance=cov,
        regularized_covariance=reg_cov,
        daily_volatilities=daily_vols,
        annualized_volatilities=ann_vols,
        zero_variance_alphas=tuple(zero_var),
        insufficient_overlap_pairs=tuple(insufficient_pairs),
        shrinkage=shrinkage,
        condition_number=cond,
        is_singular=is_singular,
        effective_independent_alphas=n_eff,
        avg_abs_pairwise_correlation=avg_abs_corr,
    )
