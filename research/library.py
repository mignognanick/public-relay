"""Shared academic references, numerical linear algebra, and Decimal performance analytics.

Pure Python 3.12 standard library so both research scripts and serverless dashboard
runtimes share one implementation without adding external runtime dependencies.
All monetary figures (prices, risk, P&L, equity, tax impact) use Decimal.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Never, Sequence

TRADING_DAYS_PER_YEAR = 252
ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")
QUANT_6 = Decimal("0.000001")
QUANT_2 = Decimal("0.01")


def _never(value: Never) -> Never:
    raise RuntimeError(f"Unhandled variant: {value!r}")


@dataclass(frozen=True)
class AcademicSource:
    """Versioned citation record for a published quantitative trading reference."""

    source_id: str
    title: str
    authors: tuple[str, ...]
    year: int
    primary_url: str
    secondary_url: str
    section: str
    equation_numbers: tuple[str, ...]
    formula_summary: str
    verified_notes: str
    adaptations_for_us_equities: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "title": self.title,
            "authors": list(self.authors),
            "year": self.year,
            "primary_url": self.primary_url,
            "secondary_url": self.secondary_url,
            "section": self.section,
            "equation_numbers": list(self.equation_numbers),
            "formula_summary": self.formula_summary,
            "verified_notes": self.verified_notes,
            "adaptations_for_us_equities": list(self.adaptations_for_us_equities),
        }


PRIMARY_PAPER_URL = "https://papers.ssrn.com/abstract=3247865"
SECONDARY_PAPER_URL = "https://arxiv.org/pdf/1912.04492"
PRIMARY_AUTHORS = ("Zura Kakushadze", "Juan Andrés Serur")
PRIMARY_TITLE = "151 Trading Strategies"


ACADEMIC_SOURCES: dict[str, AcademicSource] = {
    "ks_8_1_hp_filter": AcademicSource(
        source_id="ks_8_1_hp_filter",
        title=PRIMARY_TITLE,
        authors=PRIMARY_AUTHORS,
        year=2018,
        primary_url=PRIMARY_PAPER_URL,
        secondary_url=SECONDARY_PAPER_URL,
        section="8.1",
        equation_numbers=("437", "438", "439"),
        formula_summary=(
            "S(t) = S*(t) + nu(t); minimize sum_{t=1}^T [S(t)-S*(t)]^2 + "
            "lambda * sum_{t=2}^{T-1} [S*(t+1)-2S*(t)+S*(t-1)]^2; "
            "buy when MA(T1) > MA(T2) on S*(t), sell when MA(T1) < MA(T2)."
        ),
        verified_notes=(
            "Verified against arXiv:1912.04492 Section 8.1 Eqs. (437)-(439). "
            "First-order condition is the symmetric positive-definite pentadiagonal system "
            "(I + lambda * D^T D) S* = S where D is the (T-2)xT second-difference operator."
        ),
        adaptations_for_us_equities=(
            "Original section targets monthly FX spot rates with lambda = 100 * n^2 (n=12 -> 14400). "
            "Adapted to US equities/ETFs daily closes with configurable lambda (default 1600 for swing horizon).",
            "Strictly one-sided causal HP filter: at each decision date t, solve (I + lambda D^T D) S* = S "
            "using only observations <= t inside a bounded rolling window, preventing two-sided look-ahead leakage.",
            "Positions are entered at the next bar open with modeled spread, slippage, commission, and short borrow.",
        ),
    ),
    "ks_3_17_knn": AcademicSource(
        source_id="ks_3_17_knn",
        title=PRIMARY_TITLE,
        authors=PRIMARY_AUTHORS,
        year=2018,
        primary_url=PRIMARY_PAPER_URL,
        secondary_url=SECONDARY_PAPER_URL,
        section="3.17",
        equation_numbers=("332", "333", "334", "335", "337", "338", "339", "340", "341"),
        formula_summary=(
            "Forward T-day return Y(t) = P(t+T)/P(t) - 1; features X_a(t) min-max normalized "
            "on training data to [0,1]; Euclidean distance D(t,t')^2 = sum_a (X~_a(t) - X~_a(t'))^2; "
            "forecast Y_hat(t) = (1/k) sum_{alpha=1}^k Y(t'_alpha) or linear-weighted Eq. (340); "
            "hysteresis thresholds (z1, z2) for long/short entry and exit."
        ),
        verified_notes=(
            "Verified against arXiv:1912.04492 Section 3.17 Eqs. (332)-(341). "
            "Paper uses reverse-time indexing (t increases into the past); converted to chronological indexing."
        ),
        adaptations_for_us_equities=(
            "Enforces a strict purge gap of T bars: a historical neighbor t' is eligible at decision time t "
            "only if t' + T <= t so its forward return Y(t') is already realized.",
            "Min-max bounds X_a^- and X_a^+ are fit exclusively on the training partition and held fixed out-of-sample.",
            "Supports stationary price-ratio and volume-ratio features alongside raw rolling MAs to avoid non-stationary drift.",
        ),
    ),
    "ks_3_18_stat_arb": AcademicSource(
        source_id="ks_3_18_stat_arb",
        title=PRIMARY_TITLE,
        authors=PRIMARY_AUTHORS,
        year=2018,
        primary_url=PRIMARY_PAPER_URL,
        secondary_url=SECONDARY_PAPER_URL,
        section="3.18 / 3.18.1",
        equation_numbers=("342", "343", "344", "346", "350", "354", "356", "357", "358"),
        formula_summary=(
            "Maximize Sharpe S = (E^T w) / sqrt(w^T C w) subject to sum |w_i| = 1 and dollar-neutrality "
            "sum w_i = 0 via Lagrange multiplier: "
            "w = (1/lambda) * [C^{-1} E - C^{-1} 1 * (1^T C^{-1} E) / (1^T C^{-1} 1)]."
        ),
        verified_notes=(
            "Verified against arXiv:1912.04492 Section 3.18 & 3.18.1 Eqs. (342)-(358). "
            "Footnote 61 explicitly notes sample covariance C_ij is singular if T <= N + 1 and unstable out-of-sample."
        ),
        adaptations_for_us_equities=(
            "Applies diagonal shrinkage regularization C_reg = (1-delta)*C + delta*diag(C) + eps*I "
            "estimated strictly on trailing returns <= t so C_reg is positive-definite even for small T.",
            "Expected returns E_i are generated from causal cross-sectional residual mean-reversion across US equities/ETFs.",
            "Adds configurable per-asset gross weight cap and re-projects to maintain dollar-neutrality and unit L1 norm.",
        ),
    ),
    "ks_18_2_ann_quantile": AcademicSource(
        source_id="ks_18_2_ann_quantile",
        title=PRIMARY_TITLE,
        authors=PRIMARY_AUTHORS,
        year=2018,
        primary_url=PRIMARY_PAPER_URL,
        secondary_url=SECONDARY_PAPER_URL,
        section="18.2",
        equation_numbers=("521", "522", "523", "524", "525", "526", "527", "528", "529", "530", "531", "532", "533", "534", "535", "536", "537"),
        formula_summary=(
            "Rolling standardized returns R_hat(t) = (R(t)-R_bar)/sigma; technical input layer "
            "{R_hat, EMA(lambda_a, tau_a), EMSD(lambda_a, tau_a), RSI(tau'_a')}; "
            "feedforward MLP with ReLU hidden layers, softmax output p_alpha(t) over K return quantiles, "
            "cross-entropy loss E = -sum S_alpha ln(p_alpha); buy if argmax p = K, sell if argmax p = 1."
        ),
        verified_notes=(
            "Verified against arXiv:1912.04492 Section 18.2 Eqs. (521)-(537). "
            "Note on Eq. (535): the published text omits exp() in the printed softmax formula "
            "Y^(L)/sum(Y^(L)); implemented standard numerically stable softmax exp(Y_i-max)/sum exp(Y_j-max) "
            "so probabilities are strictly positive and sum to 1."
        ),
        adaptations_for_us_equities=(
            "Adapted from 15-minute Bitcoin bars to US equity/ETF daily (or intraday) bars with lookbacks "
            "tau_a = (3, 5, 10, 20) and tau'_a' = (5, 10, 20), lambda_a = (tau_a - 1)/(tau_a + 1) per Footnote 223.",
            "Quantile cutoffs q_1..q_{K-1}, input standardization stats, and MLP weights are fit strictly on training data "
            "excluding warmup rows d1 (Footnote 224), trained with deterministic seeded mini-batch SGD + L2 weight decay.",
            "Target at time t is next-bar standardized return R_hat(t+1), keeping features at t strictly causal.",
        ),
    ),
    "ks_18_3_bernoulli_nb": AcademicSource(
        source_id="ks_18_3_bernoulli_nb",
        title=PRIMARY_TITLE,
        authors=PRIMARY_AUTHORS,
        year=2018,
        primary_url=PRIMARY_PAPER_URL,
        secondary_url=SECONDARY_PAPER_URL,
        section="18.3",
        equation_numbers=("538", "539", "540", "541", "542", "543", "544", "545", "546"),
        formula_summary=(
            "Binary document feature vectors X_ia in {0,1} over learned vocabulary V; "
            "C_pred = argmax_{C_alpha} P(C_alpha) * prod_{i=1}^N prod_{a=1}^M "
            "P(w_a|C_alpha)^{X_ia} * (1 - P(w_a|C_alpha))^{1 - X_ia}."
        ),
        verified_notes=(
            "Verified against arXiv:1912.04492 Section 18.3 Eqs. (538)-(546) and Footnote 230 "
            "(deduplication, stop-word removal, suffix stemming)."
        ),
        adaptations_for_us_equities=(
            "Adapted from Bitcoin tweets to timestamped US equity/ETF headlines and news items available strictly <= t.",
            "Vocabulary V, class priors P(C_alpha), and conditional word probabilities P(w_a|C_alpha) are fit exclusively "
            "on training data with Laplace smoothing alpha_smooth > 0 and log-space summation to prevent underflow.",
            "Requires a configurable posterior probability margin above uniform prior before entering; flat when no headlines exist.",
        ),
    ),
    "ks_3_20_alpha_combo": AcademicSource(
        source_id="ks_3_20_alpha_combo",
        title=PRIMARY_TITLE,
        authors=PRIMARY_AUTHORS,
        year=2018,
        primary_url=PRIMARY_PAPER_URL,
        secondary_url=SECONDARY_PAPER_URL,
        section="3.20",
        equation_numbers=("360",),
        formula_summary=(
            "Kakushadze-Yu (2017b) 11-step alpha combo: serially demean R_is -> X_is; "
            "normalize by sample vol sigma_i -> Y_is; keep first M columns; cross-sectionally demean -> Lambda_is; "
            "keep first M-1 columns; regress E~_i = E_i/sigma_i on Lambda_is without intercept -> residuals eps~_i; "
            "set w_i = eta * eps~_i / sigma_i with sum |w_i| = 1."
        ),
        verified_notes=(
            "Verified against arXiv:1912.04492 Section 3.20 Steps 1-11 and Eq. (360)."
        ),
        adaptations_for_us_equities=(
            "Strictly research-only (V3). Adds finite-N rank protection: when number of alphas N <= M, "
            "SVD/ridge truncation caps the factor subspace at min(N-2, M-1) so residuals do not collapse to zero.",
            "Excludes zero-variance or insufficient-history alphas cleanly with weight 0.",
            "Enforces safety guardrails against unapproved strategy inversion (negative weights blocked unless explicitly approved), "
            "single-alpha concentration limits, and turnover limits.",
        ),
    ),
}


def canonical_hash(payload: object) -> str:
    """Deterministic SHA-256 hash of JSON-serializable metadata or specs."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def q6(value: Decimal) -> Decimal:
    return value.quantize(QUANT_6, rounding=ROUND_HALF_UP)


def q2(value: Decimal) -> Decimal:
    return value.quantize(QUANT_2, rounding=ROUND_HALF_UP)


def dec_from_float(value: float, label: str = "value") -> Decimal:
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite, got {value!r}")
    return Decimal(f"{value:.12f}")


def dec_str(value: Decimal) -> str:
    return format(q6(value), "f")


def solve_pentadiagonal_spd(
    d0: Sequence[float],
    d1: Sequence[float],
    d2: Sequence[float],
    rhs: Sequence[float],
) -> list[float]:
    """Solve a symmetric positive-definite pentadiagonal system A x = rhs via LDL^T in O(n).

    d0: main diagonal (length n)
    d1: first off-diagonal (length n-1)
    d2: second off-diagonal (length n-2)
    """
    n = len(rhs)
    if n == 0:
        return []
    if n == 1:
        if d0[0] <= 0.0:
            raise ValueError("Pentadiagonal matrix is not positive definite")
        return [rhs[0] / d0[0]]

    # Banded Cholesky / LDL^T decomposition: A = L D L^T with lower bandwidth 2.
    diag = [0.0] * n
    l1 = [0.0] * (n - 1)
    l2 = [0.0] * max(0, n - 2)

    for i in range(n):
        val = d0[i]
        if i >= 1:
            val -= (l1[i - 1] ** 2) * diag[i - 1]
        if i >= 2:
            val -= (l2[i - 2] ** 2) * diag[i - 2]
        if val <= 1e-15:
            raise ValueError(f"Pentadiagonal pivot non-positive at index {i}: {val}")
        diag[i] = val

        if i + 1 < n:
            s1 = d1[i]
            if i >= 1:
                s1 -= l1[i - 1] * l2[i - 1] * diag[i - 1]
            l1[i] = s1 / diag[i]
        if i + 2 < n:
            l2[i] = d2[i] / diag[i]

    # Forward substitution: L y = rhs
    y = [0.0] * n
    for i in range(n):
        s = rhs[i]
        if i >= 1:
            s -= l1[i - 1] * y[i - 1]
        if i >= 2:
            s -= l2[i - 2] * y[i - 2]
        y[i] = s

    # Diagonal scaling: D z = y
    z = [y[i] / diag[i] for i in range(n)]

    # Backward substitution: L^T x = z
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = z[i]
        if i + 1 < n:
            s -= l1[i] * x[i + 1]
        if i + 2 < n:
            s -= l2[i] * x[i + 2]
        x[i] = s
    return x


def hodrick_prescott_filter(series: Sequence[float], lamb: float) -> tuple[list[float], list[float]]:
    """Exact Hodrick-Prescott decomposition S = S* + nu minimizing Eq. (438).

    Returns (trend S*, cycle nu). When len(series) < 3, trend equals series.
    Callers requiring causal signals MUST pass only observations <= decision time t.
    """
    if lamb < 0.0 or not math.isfinite(lamb):
        raise ValueError("HP smoothing parameter lambda must be finite and >= 0")
    n = len(series)
    if n < 3 or lamb == 0.0:
        trend = [float(x) for x in series]
        return trend, [0.0] * n

    # Construct diagonal bands of A = I + lamb * D^T D where (D x)_k = x_k - 2 x_{k+1} + x_{k+2}
    d0 = [1.0] * n
    d1 = [0.0] * (n - 1)
    d2 = [0.0] * (n - 2)
    for k in range(n - 2):
        # Contribution of (x_k - 2 x_{k+1} + x_{k+2})^2
        d0[k] += lamb * 1.0
        d0[k + 1] += lamb * 4.0
        d0[k + 2] += lamb * 1.0
        d1[k] += lamb * (-2.0)
        d1[k + 1] += lamb * (-2.0)
        d2[k] += lamb * 1.0

    trend = solve_pentadiagonal_spd(d0, d1, d2, [float(x) for x in series])
    cycle = [float(s) - tr for s, tr in zip(series, trend)]
    return trend, cycle


def mat_transpose(a: Sequence[Sequence[float]]) -> list[list[float]]:
    if not a:
        return []
    rows, cols = len(a), len(a[0])
    return [[a[r][c] for r in range(rows)] for c in range(cols)]


def mat_mul(a: Sequence[Sequence[float]], b: Sequence[Sequence[float]]) -> list[list[float]]:
    if not a or not b:
        return []
    m, k, n = len(a), len(a[0]), len(b[0])
    out = [[0.0] * n for _ in range(m)]
    for i in range(m):
        ai = a[i]
        oi = out[i]
        for p in range(k):
            aip = ai[p]
            if aip == 0.0:
                continue
            bp = b[p]
            for j in range(n):
                oi[j] += aip * bp[j]
    return out


def mat_vec(a: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    return [sum(row[j] * v[j] for j in range(len(v))) for row in a]


def solve_linear_system(matrix: Sequence[Sequence[float]], rhs: Sequence[float]) -> list[float]:
    """Solve dense linear system A x = rhs using Gaussian elimination with partial pivoting."""
    n = len(rhs)
    if n == 0:
        return []
    a = [[float(matrix[i][j]) for j in range(n)] + [float(rhs[i])] for i in range(n)]

    for col in range(n):
        pivot_row = max(range(col, n), key=lambda r: abs(a[r][col]))
        pivot = a[pivot_row][col]
        if abs(pivot) < 1e-12:
            raise ValueError("Linear system is singular or numerically rank-deficient")
        if pivot_row != col:
            a[col], a[pivot_row] = a[pivot_row], a[col]
        inv_p = 1.0 / pivot
        for j in range(col, n + 1):
            a[col][j] *= inv_p
        for r in range(n):
            if r == col:
                continue
            factor = a[r][col]
            if factor == 0.0:
                continue
            for j in range(col, n + 1):
                a[r][j] -= factor * a[col][j]

    return [a[i][n] for i in range(n)]


def invert_symmetric_regularized(
    matrix: Sequence[Sequence[float]],
    ridge: float = 1e-8,
) -> list[list[float]]:
    """Invert symmetric matrix with adaptive ridge regularization for numerical stability."""
    n = len(matrix)
    if n == 0:
        return []
    reg = max(0.0, float(ridge))
    for attempt in range(8):
        shifted = [
            [float(matrix[i][j]) + (reg if i == j else 0.0) for j in range(n)]
            for i in range(n)
        ]
        try:
            inv_cols = [
                solve_linear_system(shifted, [1.0 if r == c else 0.0 for r in range(n)])
                for c in range(n)
            ]
            return [[inv_cols[c][r] for c in range(n)] for r in range(n)]
        except ValueError:
            reg = max(1e-6, reg * 10.0) if reg > 0.0 else 1e-6
    raise ValueError("Matrix could not be inverted even after ridge regularization")


def jacobi_eigen_symmetric(
    matrix: Sequence[Sequence[float]],
    max_iter: int = 200,
    tol: float = 1e-11,
) -> tuple[list[float], list[list[float]]]:
    """Eigenvalues and eigenvectors (as columns) of a real symmetric matrix via Jacobi rotations."""
    n = len(matrix)
    if n == 0:
        return [], []
    a = [[0.5 * (float(matrix[i][j]) + float(matrix[j][i])) for j in range(n)] for i in range(n)]
    v = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]

    for _ in range(max_iter * max(1, n)):
        p, q, max_val = 0, 1, 0.0
        for i in range(n):
            for j in range(i + 1, n):
                val = abs(a[i][j])
                if val > max_val:
                    max_val, p, q = val, i, j
        if max_val < tol:
            break
        app, aqq, apq = a[p][p], a[q][q], a[p][q]
        if abs(app - aqq) < 1e-15:
            theta = math.pi / 4.0
        else:
            theta = 0.5 * math.atan2(2.0 * apq, aqq - app)
        c = math.cos(theta)
        s = math.sin(theta)
        for i in range(n):
            if i != p and i != q:
                aip, aiq = a[i][p], a[i][q]
                a[i][p] = a[p][i] = c * aip - s * aiq
                a[i][q] = a[q][i] = s * aip + c * aiq
        a[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        a[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        a[p][q] = a[q][p] = 0.0
        for i in range(n):
            vip, viq = v[i][p], v[i][q]
            v[i][p] = c * vip - s * viq
            v[i][q] = s * vip + c * viq

    evals = [a[i][i] for i in range(n)]
    order = sorted(range(n), key=lambda idx: evals[idx], reverse=True)
    sorted_evals = [evals[i] for i in order]
    sorted_evecs = [[v[r][c] for c in order] for r in range(n)]
    return sorted_evals, sorted_evecs


def ols_residuals_no_intercept(
    x_matrix: Sequence[Sequence[float]],
    y: Sequence[float],
    ridge: float = 1e-6,
    max_rank: int | None = None,
) -> tuple[list[float], list[float], int]:
    """Compute OLS / ridge-stabilized residuals y - X beta without intercept.

    If max_rank is provided and cols(X) > max_rank, projects onto top-k right singular
    directions via Gram eigendecomposition so small-N regressions never annihilate y.
    Returns (residuals, fitted, effective_rank).
    """
    n = len(y)
    if n == 0:
        return [], [], 0
    k = len(x_matrix[0]) if x_matrix and x_matrix[0] else 0
    if k == 0:
        return [float(val) for val in y], [0.0] * n, 0

    xt = mat_transpose(x_matrix)
    gram = mat_mul(xt, x_matrix)

    if max_rank is not None and k > max_rank:
        keep = max(0, max_rank)
        if keep == 0:
            return [float(val) for val in y], [0.0] * n, 0
        evals, evecs = jacobi_eigen_symmetric(gram)
        active = [idx for idx in range(min(keep, len(evals))) if evals[idx] > 1e-10]
        if not active:
            return [float(val) for val in y], [0.0] * n, 0
        basis = [[evecs[r][idx] for idx in active] for r in range(k)]
        x_reduced = mat_mul(x_matrix, basis)
        return ols_residuals_no_intercept(x_reduced, y, ridge=ridge, max_rank=None)

    xty = mat_vec(xt, y)
    gram_reg = [
        [gram[i][j] + (ridge if i == j else 0.0) for j in range(k)]
        for i in range(k)
    ]
    beta = solve_linear_system(gram_reg, xty)
    fitted = mat_vec(x_matrix, beta)
    residuals = [float(y[i]) - fitted[i] for i in range(n)]
    evals, _ = jacobi_eigen_symmetric(gram)
    rank = sum(1 for ev in evals if ev > 1e-9)
    return residuals, fitted, rank


def sample_mean(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def sample_variance(values: Sequence[float], ddof: int = 1) -> float:
    n = len(values)
    if n <= ddof:
        return 0.0
    mu = sum(values) / n
    return sum((x - mu) ** 2 for x in values) / (n - ddof)


def sample_std(values: Sequence[float], ddof: int = 1) -> float:
    return math.sqrt(max(0.0, sample_variance(values, ddof=ddof)))


def compute_return_metrics(
    daily_returns: Mapping[str, Decimal],
    initial_equity: Decimal = Decimal("100000"),
    trade_pnls: Sequence[Decimal] = (),
    turnovers: Sequence[Decimal] = (),
    total_costs: Decimal = ZERO,
    short_term_tax_rate: Decimal = Decimal("0.32"),
    long_term_tax_rate: Decimal = Decimal("0.15"),
    holding_days_avg: float = 5.0,
) -> dict[str, object]:
    """Compute standardized risk/return and illustrative tax-drag metrics in Decimal."""
    dates = sorted(daily_returns)
    equity = initial_equity
    peak = initial_equity
    max_dd = ZERO
    curve: list[dict[str, str]] = []
    float_rets: list[float] = []

    for d in dates:
        r = daily_returns[d]
        float_rets.append(float(r))
        equity = equity * (ONE + r)
        if equity > peak:
            peak = equity
        if peak > ZERO:
            dd = (peak - equity) / peak
            if dd > max_dd:
                max_dd = dd
        curve.append({"date": d, "equity": dec_str(equity), "return": dec_str(r)})

    n_days = len(dates)
    total_return = (equity / initial_equity - ONE) if initial_equity > ZERO else ZERO
    net_pnl = equity - initial_equity

    mean_r = sample_mean(float_rets)
    vol_daily = sample_std(float_rets, ddof=1)
    ann_vol = vol_daily * math.sqrt(TRADING_DAYS_PER_YEAR)
    ann_return = (
        (float(ONE + total_return) ** (TRADING_DAYS_PER_YEAR / n_days) - 1.0)
        if n_days > 0 and (ONE + total_return) > ZERO
        else -1.0
    )
    sharpe = (mean_r / vol_daily * math.sqrt(TRADING_DAYS_PER_YEAR)) if vol_daily > 1e-12 else 0.0
    downside = [r for r in float_rets if r < 0.0]
    downside_dev = math.sqrt(sum(r * r for r in downside) / max(1, len(float_rets)))
    sortino = (mean_r / downside_dev * math.sqrt(TRADING_DAYS_PER_YEAR)) if downside_dev > 1e-12 else 0.0
    calmar = (ann_return / float(max_dd)) if max_dd > Decimal("1e-9") else 0.0

    wins = [p for p in trade_pnls if p > ZERO]
    losses = [-p for p in trade_pnls if p < ZERO]
    win_rate = (Decimal(len(wins)) / Decimal(len(trade_pnls)) * HUNDRED) if trade_pnls else None
    profit_factor = (sum(wins, ZERO) / sum(losses, ZERO)) if losses else None
    expectancy = (sum(trade_pnls, ZERO) / Decimal(len(trade_pnls))) if trade_pnls else None
    avg_turnover = (sum(turnovers, ZERO) / Decimal(len(turnovers))) if turnovers else ZERO

    tax_character = "long_term" if holding_days_avg >= 365.0 else "short_term"
    applicable_rate = long_term_tax_rate if tax_character == "long_term" else short_term_tax_rate
    illustrative_tax_drag = max(ZERO, net_pnl) * applicable_rate
    after_tax_net_pnl = net_pnl - illustrative_tax_drag

    return {
        "observations": n_days,
        "initial_equity": dec_str(initial_equity),
        "ending_equity": dec_str(equity),
        "net_pnl": dec_str(net_pnl),
        "total_return_pct": dec_str(total_return * HUNDRED),
        "annualized_return_pct": dec_str(dec_from_float(ann_return * 100.0, "ann_return")),
        "annualized_volatility_pct": dec_str(dec_from_float(ann_vol * 100.0, "ann_vol")),
        "sharpe_ratio": dec_str(dec_from_float(sharpe, "sharpe")),
        "sortino_ratio": dec_str(dec_from_float(sortino, "sortino")),
        "calmar_ratio": dec_str(dec_from_float(calmar, "calmar")),
        "max_drawdown_pct": dec_str(max_dd * HUNDRED),
        "trades": len(trade_pnls),
        "win_rate_pct": dec_str(win_rate) if win_rate is not None else None,
        "profit_factor": dec_str(profit_factor) if profit_factor is not None else None,
        "expectancy": dec_str(expectancy) if expectancy is not None else None,
        "avg_daily_turnover": dec_str(avg_turnover),
        "total_transaction_costs": dec_str(total_costs),
        "tax_character": tax_character,
        "illustrative_tax_drag": dec_str(illustrative_tax_drag),
        "after_tax_net_pnl": dec_str(after_tax_net_pnl),
        "equity_curve": curve,
    }


def classify_research_regime(
    trailing_returns: Sequence[float],
) -> str:
    """Research-only realized return/volatility regime tag matching Quant Engine's 6 regimes.

    Strictly causal: callers must pass only returns realized at or before decision time t.
    Never overrides manual dashboard regime controls.
    """
    if len(trailing_returns) < 5:
        return "chop"
    window = trailing_returns[-20:] if len(trailing_returns) >= 20 else trailing_returns
    cum = 1.0
    for r in window:
        cum *= 1.0 + r
    cum_ret = cum - 1.0
    ann_vol = sample_std(window, ddof=1) * math.sqrt(TRADING_DAYS_PER_YEAR)
    if ann_vol >= 0.30:
        return "high_volatility"
    if cum_ret >= 0.05:
        return "strong_bull"
    if cum_ret >= 0.015:
        return "bull"
    if cum_ret <= -0.05:
        return "strong_bear"
    if cum_ret <= -0.015:
        return "bear"
    return "chop"
