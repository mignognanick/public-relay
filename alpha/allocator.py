"""Portfolio alpha allocation models: V1 (equal risk), V2 (vol/corr constrained), V3 (Kakushadze-Yu).

Safety invariants:
- Never permits unapproved strategy inversion (negative alpha weights blocked unless explicitly approved).
- Handles missing dates, singular covariance matrices, zero-variance alphas, and unstable weights.
- Enforces single-alpha concentration caps and turnover caps relative to previous weights.
- Marks V3 explicitly as research_only=True.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, Mapping, Never, Sequence

from alpha.correlation import CorrelationReport, compute_correlation_report
from research.library import (
    ONE,
    ZERO,
    dec_from_float,
    dec_str,
    mat_vec,
    ols_residuals_no_intercept,
    sample_mean,
)

AllocationModel = Literal["v1", "v2", "v3"]


def _never(value: Never) -> Never:
    raise RuntimeError(f"Unhandled allocation model: {value!r}")


@dataclass(frozen=True)
class AllocationConstraints:
    max_weight: Decimal = Decimal("0.50")
    max_turnover: Decimal = Decimal("0.50")
    min_observations: int = 10
    shrinkage: float = 0.25
    ridge: float = 1e-6
    expected_return_window: int = 20
    risk_aversion: float = 10.0
    allow_inversion_by_alpha: Mapping[str, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.max_weight <= ZERO or self.max_weight > ONE:
            raise ValueError("max_weight must be in (0, 1]")
        if self.max_turnover <= ZERO or self.max_turnover > Decimal("2"):
            raise ValueError("max_turnover must be in (0, 2]")
        if self.min_observations < 3:
            raise ValueError("min_observations must be >= 3")


@dataclass(frozen=True)
class AllocationResult:
    model: AllocationModel
    research_only: bool
    weights: dict[str, Decimal]
    raw_weights: dict[str, Decimal]
    turnover: Decimal
    excluded_alphas: tuple[str, ...]
    inversion_blocked_alphas: tuple[str, ...]
    warnings: tuple[str, ...]
    correlation_report: CorrelationReport
    diagnostics: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "model": self.model,
            "research_only": self.research_only,
            "weights": {k: dec_str(v) for k, v in self.weights.items()},
            "raw_weights": {k: dec_str(v) for k, v in self.raw_weights.items()},
            "turnover": dec_str(self.turnover),
            "excluded_alphas": list(self.excluded_alphas),
            "inversion_blocked_alphas": list(self.inversion_blocked_alphas),
            "warnings": list(self.warnings),
            "diagnostics": dict(self.diagnostics),
        }


def _apply_guardrails(
    alpha_ids: Sequence[str],
    raw: Mapping[str, float],
    constraints: AllocationConstraints,
    previous_weights: Mapping[str, Decimal] | None,
) -> tuple[dict[str, Decimal], Decimal, list[str], list[str]]:
    """Enforce inversion guard, concentration cap, L1 normalization, and turnover limit."""
    warnings: list[str] = []
    inversion_blocked: list[str] = []
    max_w = float(constraints.max_weight)

    w = {}
    for aid in alpha_ids:
        val = float(raw.get(aid, 0.0))
        if not math.isfinite(val):
            val = 0.0
            warnings.append(f"Non-finite raw weight zeroed for {aid}")
        if val < 0.0 and not constraints.allow_inversion_by_alpha.get(aid, False):
            inversion_blocked.append(aid)
            val = 0.0
        w[aid] = val

    active_ids = [aid for aid in alpha_ids if abs(w[aid]) > 1e-12]
    if not active_ids:
        final_zero = {aid: ZERO for aid in alpha_ids}
        turnover = _l1_turnover(alpha_ids, final_zero, previous_weights)
        warnings.append("No eligible non-zero alpha weights after constraints; staying flat")
        return final_zero, turnover, inversion_blocked, warnings

    # Effective per-alpha cap: if N_active * max_w < 1, total gross exposure scales to N_active * max_w (never exceeds max_w).
    cap_total = min(1.0, len(active_ids) * max_w)
    if len(active_ids) * max_w < 1.0 - 1e-9:
        warnings.append(
            f"Concentration cap ({max_w:.2f}) across {len(active_ids)} active alphas limits gross exposure to {cap_total:.2f}"
        )

    # Iterative water-filling projection onto {sum |w_i| = cap_total, |w_i| <= max_w}
    cur = dict(w)
    for _ in range(25):
        gross = sum(abs(cur[a]) for a in alpha_ids)
        if gross <= 1e-12:
            break
        scale = cap_total / gross
        cur = {a: cur[a] * scale for a in alpha_ids}
        over = [a for a in alpha_ids if abs(cur[a]) > max_w + 1e-9]
        if not over:
            break
        fixed_sum = 0.0
        free: list[str] = []
        for a in alpha_ids:
            if abs(cur[a]) >= max_w - 1e-9:
                cur[a] = math.copysign(max_w, cur[a])
                fixed_sum += max_w
            elif abs(cur[a]) > 1e-12:
                free.append(a)
        rem = max(0.0, cap_total - fixed_sum)
        free_gross = sum(abs(cur[a]) for a in free)
        if free_gross <= 1e-12 or rem <= 1e-12:
            for a in free:
                cur[a] = 0.0
            break
        for a in free:
            cur[a] = cur[a] * (rem / free_gross)

    # Enforce turnover cap relative to previous weights if provided
    if previous_weights is not None:
        prev = {a: float(previous_weights.get(a, ZERO)) for a in alpha_ids}
        raw_turn = sum(abs(cur[a] - prev[a]) for a in alpha_ids)
        max_turn = float(constraints.max_turnover)
        if raw_turn > max_turn + 1e-12 and raw_turn > 0.0:
            blend = max_turn / raw_turn
            cur = {a: prev[a] + blend * (cur[a] - prev[a]) for a in alpha_ids}
            warnings.append(
                f"Turnover damped from {raw_turn:.4f} to cap {max_turn:.4f}"
            )

    dec_weights = {a: dec_from_float(cur[a], f"weight_{a}") for a in alpha_ids}
    turnover = _l1_turnover(alpha_ids, dec_weights, previous_weights)
    return dec_weights, turnover, inversion_blocked, warnings


def _l1_turnover(
    alpha_ids: Sequence[str],
    current: Mapping[str, Decimal],
    previous: Mapping[str, Decimal] | None,
) -> Decimal:
    if previous is None:
        return sum((abs(current.get(a, ZERO)) for a in alpha_ids), ZERO)
    return sum((abs(current.get(a, ZERO) - previous.get(a, ZERO)) for a in alpha_ids), ZERO)


def _allocate_v1_equal_risk(
    corr_report: CorrelationReport,
    constraints: AllocationConstraints,
) -> tuple[dict[str, float], list[str], dict[str, object]]:
    """V1: Equal risk allocation (inverse volatility)."""
    excluded = set(corr_report.zero_variance_alphas)
    raw: dict[str, float] = {}
    for aid in corr_report.alpha_ids:
        if aid in excluded:
            raw[aid] = 0.0
            continue
        vol = corr_report.daily_volatilities.get(aid, 0.0)
        raw[aid] = (1.0 / vol) if vol > 1e-12 else 0.0
    total = sum(abs(v) for v in raw.values())
    if total > 1e-12:
        raw = {k: v / total for k, v in raw.items()}
    return raw, sorted(excluded), {"method": "inverse_volatility_equal_risk"}


def _allocate_v2_vol_corr_adjusted(
    alpha_returns: Mapping[str, Mapping[str, Decimal]],
    corr_report: CorrelationReport,
    constraints: AllocationConstraints,
    expected_returns: Mapping[str, Decimal] | None,
) -> tuple[dict[str, float], list[str], dict[str, object]]:
    """V2: Constrained volatility and correlation-adjusted allocation via projected gradient."""
    excluded = set(corr_report.zero_variance_alphas)
    active = [aid for aid in corr_report.alpha_ids if aid not in excluded]
    if not active:
        return {aid: 0.0 for aid in corr_report.alpha_ids}, sorted(excluded), {"method": "constrained_vol_corr"}

    idx_map = {aid: i for i, aid in enumerate(corr_report.alpha_ids)}
    k = len(active)
    cov_sub = [
        [corr_report.regularized_covariance[idx_map[active[i]]][idx_map[active[j]]] for j in range(k)]
        for i in range(k)
    ]

    # Expected returns (non-negative signal tilt when positive, bounded to avoid chasing noise)
    exp_vec: list[float] = []
    for aid in active:
        if expected_returns is not None and aid in expected_returns:
            er = float(expected_returns[aid])
        else:
            series = alpha_returns.get(aid, {})
            ordered = [float(series[d]) for d in sorted(series)][-constraints.expected_return_window :]
            er = sample_mean(ordered) if ordered else 0.0
        exp_vec.append(er)

    # Scale variance matrix to unit trace for well-conditioned step size
    trace = sum(cov_sub[i][i] for i in range(k))
    scale = max(trace / k, 1e-8)
    c_scaled = [[cov_sub[i][j] / scale for j in range(k)] for i in range(k)]
    # Small tilt toward positive expected returns relative to risk aversion
    er_scale = max(math.sqrt(scale), 1e-6)
    e_scaled = [max(-2.0, min(2.0, er / er_scale)) / max(constraints.risk_aversion, 1.0) for er in exp_vec]

    # Initialize from inverse-volatility weights
    inv_vols = [1.0 / max(corr_report.daily_volatilities[aid], 1e-8) for aid in active]
    s_iv = sum(inv_vols)
    w = [v / s_iv for v in inv_vols]

    max_w = float(constraints.max_weight)
    allow_inv = [bool(constraints.allow_inversion_by_alpha.get(aid, False)) for aid in active]

    # Projected gradient descent minimizing 0.5 * w^T C_scaled w - e_scaled^T w
    lr = 0.15
    for _ in range(150):
        cw = mat_vec(c_scaled, w)
        grad = [cw[i] - e_scaled[i] for i in range(k)]
        w_next = [w[i] - lr * grad[i] for i in range(k)]
        # Project box bounds (no unapproved inversion)
        for i in range(k):
            lo = -max_w if allow_inv[i] else 0.0
            w_next[i] = max(lo, min(max_w, w_next[i]))
        gross = sum(abs(x) for x in w_next)
        if gross <= 1e-12:
            w_next = [v / s_iv for v in inv_vols]
        else:
            target = min(1.0, k * max_w)
            w_next = [x * (target / gross) for x in w_next]
        diff = sum(abs(w_next[i] - w[i]) for i in range(k))
        w = w_next
        if diff < 1e-9:
            break

    raw = {aid: 0.0 for aid in corr_report.alpha_ids}
    for aid, val in zip(active, w):
        raw[aid] = val
    return raw, sorted(excluded), {"method": "constrained_vol_corr_pgd", "active_alphas": k}


def _allocate_v3_kakushadze_yu(
    alpha_returns: Mapping[str, Mapping[str, Decimal]],
    corr_report: CorrelationReport,
    constraints: AllocationConstraints,
    expected_returns: Mapping[str, Decimal] | None,
) -> tuple[dict[str, float], list[str], dict[str, object]]:
    """V3: Research-only Kakushadze-Yu (Section 3.20, Steps 1-11) alpha combination.

    Steps 1-11 from 151 Trading Strategies Section 3.20:
    1) Aligned daily alpha returns R_is, i=1..N, s=1..M+1
    2) Serial demean X_is = R_is - mean_s(R_is)
    3) Sample variance sigma_i^2 = (1/M) sum_s X_is^2
    4) Standardized returns Y_is = X_is / sigma_i
    5) Keep first M columns (s=1..M)
    6) Cross-sectional demean Lambda_is = Y_is - (1/N) sum_j Y_js
    7) Keep first M-1 columns
    8) Normalized expected returns E~_i = E_i / sigma_i (Eq. 360 d-day MA if E_i not supplied)
    9) Residuals eps~_i of no-intercept unit-weight OLS regression of E~_i on Lambda_is
       (with finite-N rank cap min(M-1, N-2) so small N never annihilates E~)
    10) Raw weights w_i = eta * eps~_i / sigma_i
    11) Normalize sum |w_i| = 1 (safety guardrails then block unapproved inversions & cap turnover).
    """
    excluded = set(corr_report.zero_variance_alphas)
    active = [aid for aid in corr_report.alpha_ids if aid not in excluded]
    n = len(active)
    if n == 0:
        return {aid: 0.0 for aid in corr_report.alpha_ids}, sorted(excluded), {
            "method": "kakushadze_yu_section_3_20",
            "status": "no_active_alphas",
        }

    # Find common aligned dates across active alphas (most recent first per Section 3.20 Footnote 69)
    date_sets = [set(alpha_returns.get(aid, {}).keys()) for aid in active]
    common_dates = sorted(set.intersection(*date_sets), reverse=True) if date_sets else []
    t_obs = len(common_dates)

    # Need at least 3 aligned observations (M >= 2) and at least 2 alphas for cross-sectional step
    if t_obs < max(3, constraints.min_observations) or n < 2:
        # Safe fallback to inverse-vol-scaled expected return when insufficient history or single alpha
        raw_fb: dict[str, float] = {aid: 0.0 for aid in corr_report.alpha_ids}
        for aid in active:
            series = alpha_returns.get(aid, {})
            vals = [float(series[d]) for d in sorted(series)][-constraints.expected_return_window :]
            er = float(expected_returns[aid]) if (expected_returns and aid in expected_returns) else sample_mean(vals)
            vol = max(corr_report.daily_volatilities[aid], 1e-8)
            raw_fb[aid] = er / (vol * vol)
        gross = sum(abs(v) for v in raw_fb.values())
        if gross > 1e-12:
            raw_fb = {k: v / gross for k, v in raw_fb.items()}
        return raw_fb, sorted(excluded), {
            "method": "kakushadze_yu_section_3_20",
            "fallback": "insufficient_common_history_or_single_alpha",
            "common_observations": t_obs,
        }

    # Step 1: R_is (N x (M+1)), s=0 is most recent
    m = t_obs - 1
    r_mat = [[float(alpha_returns[aid][d]) for d in common_dates] for aid in active]

    # Step 2 & 3: Serial demean X_is and sample variance sigma_i^2 = (1/M) sum X_is^2
    sigmas: list[float] = []
    x_mat: list[list[float]] = []
    valid_indices: list[int] = []
    for i in range(n):
        mu_i = sum(r_mat[i]) / t_obs
        xi = [r_mat[i][s] - mu_i for s in range(t_obs)]
        var_i = sum(v * v for v in xi) / m
        if var_i <= 1e-20:
            excluded.add(active[i])
            continue
        x_mat.append(xi)
        sigmas.append(math.sqrt(var_i))
        valid_indices.append(i)

    active = [active[i] for i in valid_indices]
    r_mat = [r_mat[i] for i in valid_indices]
    n_eff_alphas = len(active)
    if n_eff_alphas == 0:
        return {aid: 0.0 for aid in corr_report.alpha_ids}, sorted(excluded), {
            "method": "kakushadze_yu_section_3_20",
            "status": "all_zero_variance_on_common_dates",
        }

    # Step 4 & 5: Y_is = X_is / sigma_i, keep first M columns
    y_mat = [[x_mat[i][s] / sigmas[i] for s in range(m)] for i in range(n_eff_alphas)]

    # Step 6 & 7: Cross-sectional demean Lambda_is, keep first M-1 columns
    n_cols = max(0, m - 1)
    lam_mat = [[0.0] * n_cols for _ in range(n_eff_alphas)]
    for s in range(n_cols):
        cs_mean = sum(y_mat[i][s] for i in range(n_eff_alphas)) / n_eff_alphas
        for i in range(n_eff_alphas):
            lam_mat[i][s] = y_mat[i][s] - cs_mean

    # Step 8: Normalized expected returns E~_i = E_i / sigma_i (Eq. 360 d-day MA)
    d_win = min(constraints.expected_return_window, t_obs)
    e_tilde: list[float] = []
    for i, aid in enumerate(active):
        if expected_returns is not None and aid in expected_returns:
            er = float(expected_returns[aid])
        else:
            er = sum(r_mat[i][:d_win]) / d_win
        e_tilde.append(er / sigmas[i])

    # Step 9: No-intercept OLS residuals of E~ on Lambda (rank-capped at N - 2 for small N)
    max_rank = max(0, min(n_cols, n_eff_alphas - 2))
    if max_rank > 0:
        eps_tilde, _, used_rank = ols_residuals_no_intercept(
            lam_mat, e_tilde, ridge=constraints.ridge, max_rank=max_rank
        )
    else:
        # When N <= 2, cross-sectional demeaned factor subspace has dimension <= 1; demean E~ safely
        mean_e = sum(e_tilde) / n_eff_alphas
        eps_tilde = [val - mean_e if n_eff_alphas > 1 else val for val in e_tilde]
        used_rank = 0

    # Step 10 & 11: w_i = eta * eps~_i / sigma_i normalized to sum |w_i| = 1
    raw_active = [eps_tilde[i] / sigmas[i] for i in range(n_eff_alphas)]
    gross = sum(abs(v) for v in raw_active)
    if gross <= 1e-12:
        raw_active = [e_tilde[i] / sigmas[i] for i in range(n_eff_alphas)]
        gross = sum(abs(v) for v in raw_active)
    if gross > 1e-12:
        raw_active = [v / gross for v in raw_active]

    raw = {aid: 0.0 for aid in corr_report.alpha_ids}
    for aid, val in zip(active, raw_active):
        raw[aid] = val

    return raw, sorted(excluded), {
        "method": "kakushadze_yu_section_3_20",
        "common_observations": t_obs,
        "M": m,
        "factor_rank_used": used_rank,
        "finite_n_rank_capped": n_cols > max_rank,
    }


def compute_allocation(
    model: AllocationModel,
    alpha_returns: Mapping[str, Mapping[str, Decimal]],
    *,
    constraints: AllocationConstraints | None = None,
    previous_weights: Mapping[str, Decimal] | None = None,
    expected_returns: Mapping[str, Decimal] | None = None,
) -> AllocationResult:
    """Compute portfolio risk/capital weights across alphas using V1, V2, or V3."""
    cfg = constraints or AllocationConstraints()
    corr_report = compute_correlation_report(
        alpha_returns,
        min_overlap=min(5, cfg.min_observations),
        shrinkage=cfg.shrinkage,
        ridge=cfg.ridge,
    )

    match model:
        case "v1":
            raw_floats, excluded, diag = _allocate_v1_equal_risk(corr_report, cfg)
            research_only = False
        case "v2":
            raw_floats, excluded, diag = _allocate_v2_vol_corr_adjusted(
                alpha_returns, corr_report, cfg, expected_returns
            )
            research_only = False
        case "v3":
            raw_floats, excluded, diag = _allocate_v3_kakushadze_yu(
                alpha_returns, corr_report, cfg, expected_returns
            )
            research_only = True
        case _ as unreachable:
            _never(unreachable)

    warnings: list[str] = []
    if research_only:
        warnings.append("V3 Kakushadze-Yu alpha combination is RESEARCH-ONLY; not approved for live execution.")
    if corr_report.is_singular:
        warnings.append("Raw sample covariance is singular or ill-conditioned; shrinkage regularization applied.")
    if corr_report.zero_variance_alphas:
        warnings.append(f"Zero-variance or sparse alphas assigned zero weight: {', '.join(corr_report.zero_variance_alphas)}")

    final_weights, turnover, inv_blocked, guard_warnings = _apply_guardrails(
        corr_report.alpha_ids, raw_floats, cfg, previous_weights
    )
    warnings.extend(guard_warnings)
    if inv_blocked:
        warnings.append(
            f"Unapproved strategy inversion blocked (clamped to 0) for: {', '.join(inv_blocked)}"
        )

    raw_dec = {aid: dec_from_float(raw_floats.get(aid, 0.0), f"raw_{aid}") for aid in corr_report.alpha_ids}
    return AllocationResult(
        model=model,
        research_only=research_only,
        weights=final_weights,
        raw_weights=raw_dec,
        turnover=turnover,
        excluded_alphas=tuple(excluded),
        inversion_blocked_alphas=tuple(inv_blocked),
        warnings=tuple(warnings),
        correlation_report=corr_report,
        diagnostics=diag,
    )
