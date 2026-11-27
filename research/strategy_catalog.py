"""Versioned catalog of research-only mathematical trading strategies.

Implements the five initial strategies from 151 Trading Strategies
(Kakushadze & Serur, SSRN 3247865 / arXiv:1912.04492), adapted to US equities/ETFs
with strict causality (no look-ahead) and training-only parameter fitting.
"""
from __future__ import annotations

import math
import random
import re
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Literal, Mapping, Protocol, Sequence

from research.feature_store import FeatureStore, MarketBar, TextObservation
from research.library import (
    ACADEMIC_SOURCES,
    AcademicSource,
    canonical_hash,
    hodrick_prescott_filter,
    invert_symmetric_regularized,
    mat_mul,
    mat_transpose,
    mat_vec,
    sample_mean,
    sample_std,
    solve_linear_system,
)

PortfolioBucket = Literal["day", "swing", "long_term"]
SignalFrame = dict[str, dict[str, float]]  # date -> {symbol: target_weight}

STOP_WORDS: frozenset[str] = frozenset(
    {
        "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
        "have", "has", "had", "not", "but", "into", "over", "after", "before", "about",
        "its", "their", "our", "your", "will", "would", "could", "should", "may", "can",
        "all", "any", "more", "most", "some", "such", "than", "then", "when", "where",
        "which", "while", "who", "how", "what", "why", "also", "been", "being", "per",
    }
)
TOKEN_RE = re.compile(r"[a-z]{3,}")


def _simple_stem(token: str) -> str:
    """Lightweight deterministic Porter-style suffix stemmer (Footnote 230)."""
    for suffix in ("ingly", "edly", "ation", "ition", "ment", "ness", "ings", "ing", "ied", "ies", "ed", "ly", "es", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def preprocess_text(text: str) -> frozenset[str]:
    tokens = TOKEN_RE.findall(text.lower())
    return frozenset(_simple_stem(tok) for tok in tokens if tok not in STOP_WORDS)


def _quantiles(sorted_values: Sequence[float], k: int) -> list[float]:
    if not sorted_values or k < 2:
        return []
    n = len(sorted_values)
    cuts: list[float] = []
    for q in range(1, k):
        pos = q * (n - 1) / k
        lo = int(math.floor(pos))
        hi = min(n - 1, lo + 1)
        frac = pos - lo
        cuts.append(sorted_values[lo] * (1.0 - frac) + sorted_values[hi] * frac)
    return cuts


def _assign_quantile(value: float, cutoffs: Sequence[float]) -> int:
    for idx, c in enumerate(cutoffs):
        if value <= c:
            return idx
    return len(cutoffs)


def _stable_softmax(logits: Sequence[float]) -> list[float]:
    if not logits:
        return []
    m = max(logits)
    exps = [math.exp(max(-50.0, min(50.0, x - m))) for x in logits]
    total = sum(exps)
    if total <= 0.0:
        return [1.0 / len(logits)] * len(logits)
    return [e / total for e in exps]


def _bar_closes(bars: Sequence[MarketBar]) -> list[float]:
    return [float(b.close) for b in bars]


def _bar_volumes(bars: Sequence[MarketBar]) -> list[float]:
    return [float(b.volume) for b in bars]


def _resolve_symbol(store: FeatureStore, params: Mapping[str, Any]) -> str:
    requested = str(params.get("symbol", "")).strip().upper()
    if requested and requested in store.symbols:
        return requested
    return store.symbols[0]


@dataclass(frozen=True)
class StrategyMetadata:
    strategy_key: str
    name: str
    version: str
    source_id: str
    portfolio: PortfolioBucket
    research_only: bool
    permitted_regimes: tuple[str, ...]
    default_params: Mapping[str, Any]
    param_grid: tuple[Mapping[str, Any], ...]
    description: str

    @property
    def source(self) -> AcademicSource:
        return ACADEMIC_SOURCES[self.source_id]

    def spec_hash(self, params: Mapping[str, Any] | None = None) -> str:
        effective = dict(self.default_params)
        if params:
            effective.update(params)
        return canonical_hash(
            {
                "strategy_key": self.strategy_key,
                "version": self.version,
                "source_id": self.source_id,
                "parameters": effective,
            }
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_key": self.strategy_key,
            "name": self.name,
            "version": self.version,
            "source_id": self.source_id,
            "section": self.source.section,
            "portfolio": self.portfolio,
            "research_only": self.research_only,
            "permitted_regimes": list(self.permitted_regimes),
            "default_params": dict(self.default_params),
            "param_grid": [dict(p) for p in self.param_grid],
            "description": self.description,
            "academic_source": self.source.to_dict(),
            "default_spec_hash": self.spec_hash(),
        }


class ResearchStrategy(Protocol):
    metadata: StrategyMetadata

    def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
        """Fit normalization, thresholds, or model weights strictly on train_store."""
        ...

    def generate_signals(
        self,
        store: FeatureStore,
        params: Mapping[str, Any],
        fitted_state: Mapping[str, Any],
    ) -> SignalFrame:
        """Generate strictly causal target weights per date using only data <= t."""
        ...


class HPTrendFilterStrategy:
    """Section 8.1: Moving averages on causally Hodrick-Prescott filtered trend S*(t)."""

    metadata = StrategyMetadata(
        strategy_key="hp_trend_filter",
        name="Hodrick-Prescott Trend Filter (Sec 8.1)",
        version="1.0.0",
        source_id="ks_8_1_hp_filter",
        portfolio="swing",
        research_only=True,
        permitted_regimes=("strong_bull", "bull", "bear", "strong_bear"),
        default_params={
            "symbol": "SPY",
            "lamb": 1600.0,
            "short_ma": 5,
            "long_ma": 20,
            "window": 80,
            "allow_short": True,
        },
        param_grid=(
            {"lamb": 400.0, "short_ma": 5, "long_ma": 15, "window": 60},
            {"lamb": 1600.0, "short_ma": 5, "long_ma": 20, "window": 80},
            {"lamb": 6400.0, "short_ma": 10, "long_ma": 30, "window": 100},
        ),
        description=(
            "One-sided causal Hodrick-Prescott trend decomposition S(t)=S*(t)+nu(t) "
            "followed by short/long moving average crossover on S*(t)."
        ),
    )

    def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
        return {"symbol": _resolve_symbol(train_store, params), "fitted_on_sessions": len(train_store.dates)}

    def generate_signals(
        self,
        store: FeatureStore,
        params: Mapping[str, Any],
        fitted_state: Mapping[str, Any],
    ) -> SignalFrame:
        cfg = {**self.metadata.default_params, **params}
        symbol = str(fitted_state.get("symbol") or _resolve_symbol(store, cfg))
        lamb = float(cfg["lamb"])
        short_ma = int(cfg["short_ma"])
        long_ma = int(cfg["long_ma"])
        window = int(cfg["window"])
        allow_short = bool(cfg.get("allow_short", True))
        if short_ma < 1 or long_ma <= short_ma:
            raise ValueError("HP trend filter requires 1 <= short_ma < long_ma")
        if window < long_ma:
            raise ValueError("HP rolling window must be >= long_ma")

        bars = store.bars(symbol)
        closes = _bar_closes(bars)
        out: SignalFrame = {}

        for i, bar in enumerate(bars):
            if i + 1 < long_ma:
                out[bar.day] = {symbol: 0.0}
                continue
            start = max(0, i + 1 - window)
            # Strictly causal slice: closes[start : i + 1] never accesses bars > i.
            trend, _ = hodrick_prescott_filter(closes[start : i + 1], lamb=lamb)
            ma_short = sum(trend[-short_ma:]) / short_ma
            ma_long = sum(trend[-long_ma:]) / long_ma
            if ma_short > ma_long:
                weight = 1.0
            elif ma_short < ma_long and allow_short:
                weight = -1.0
            else:
                weight = 0.0
            out[bar.day] = {symbol: weight}
        return out


class KNNReturnForecastStrategy:
    """Section 3.17: Single-stock KNN return forecasting with train-only scaling and purge gap."""

    metadata = StrategyMetadata(
        strategy_key="knn_return_forecast",
        name="K-Nearest Neighbors Return Forecast (Sec 3.17)",
        version="1.0.0",
        source_id="ks_3_17_knn",
        portfolio="swing",
        research_only=True,
        permitted_regimes=("strong_bull", "bull", "chop", "bear", "strong_bear"),
        default_params={
            "symbol": "SPY",
            "horizon": 5,
            "vol_ma": 5,
            "price_ma_short": 5,
            "price_ma_long": 20,
            "sample_size": 80,
            "k": 7,
            "z1": 0.002,
            "z2": 0.0,
            "use_stationary_ratios": True,
            "linear_weights": False,
            "allow_short": True,
        },
        param_grid=(
            {"horizon": 3, "k": 5, "z1": 0.0015, "z2": 0.0, "linear_weights": False},
            {"horizon": 5, "k": 7, "z1": 0.002, "z2": 0.0, "linear_weights": False},
            {"horizon": 5, "k": 9, "z1": 0.0025, "z2": 0.0005, "linear_weights": True},
        ),
        description=(
            "Forecasts T-day forward returns via Euclidean K-nearest neighbors over normalized "
            "causal price/volume moving averages with strict T-bar realization purge gap."
        ),
    )

    @staticmethod
    def _extract_raw_features(
        closes: Sequence[float],
        volumes: Sequence[float],
        vol_ma: int,
        p_short: int,
        p_long: int,
        use_stationary_ratios: bool,
    ) -> list[tuple[float, float, float] | None]:
        warmup = max(vol_ma, p_short, p_long)
        feats: list[tuple[float, float, float] | None] = []
        for i in range(len(closes)):
            if i + 1 < warmup:
                feats.append(None)
                continue
            v_short = sum(volumes[i + 1 - vol_ma : i + 1]) / vol_ma
            v_long = sum(volumes[i + 1 - p_long : i + 1]) / p_long
            ma_s = sum(closes[i + 1 - p_short : i + 1]) / p_short
            ma_l = sum(closes[i + 1 - p_long : i + 1]) / p_long
            if use_stationary_ratios:
                x1 = v_short / max(v_long, 1e-9)
                x2 = ma_s / max(closes[i], 1e-9)
                x3 = ma_l / max(closes[i], 1e-9)
            else:
                x1, x2, x3 = v_short, ma_s, ma_l
            feats.append((x1, x2, x3))
        return feats

    @staticmethod
    def _normalize(
        vec: tuple[float, float, float],
        mins: Sequence[float],
        maxs: Sequence[float],
    ) -> tuple[float, float, float]:
        out = []
        for val, lo, hi in zip(vec, mins, maxs):
            span = hi - lo
            if span <= 1e-12:
                out.append(0.5)
            else:
                out.append((val - lo) / span)
        return (out[0], out[1], out[2])

    @staticmethod
    def _euclidean_sq(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
        return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2

    def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
        cfg = {**self.metadata.default_params, **params}
        symbol = _resolve_symbol(train_store, cfg)
        horizon = int(cfg["horizon"])
        vol_ma = int(cfg["vol_ma"])
        p_short = int(cfg["price_ma_short"])
        p_long = int(cfg["price_ma_long"])
        stationary = bool(cfg.get("use_stationary_ratios", True))
        linear_weights = bool(cfg.get("linear_weights", False))

        bars = train_store.bars(symbol)
        closes = _bar_closes(bars)
        volumes = _bar_volumes(bars)
        raw_feats = self._extract_raw_features(closes, volumes, vol_ma, p_short, p_long, stationary)

        # Fit min-max bounds strictly on training samples whose forward target Y(t) is inside train_store.
        valid_pairs: list[tuple[int, tuple[float, float, float], float]] = []
        for i in range(len(bars) - horizon):
            feat = raw_feats[i]
            if feat is None:
                continue
            target = closes[i + horizon] / closes[i] - 1.0
            valid_pairs.append((i, feat, target))

        if len(valid_pairs) < 5:
            return {
                "symbol": symbol,
                "mins": [0.0, 0.0, 0.0],
                "maxs": [1.0, 1.0, 1.0],
                "train_pool": [],
                "linear_beta": None,
                "linear_intercept": 0.0,
            }

        mins = [min(p[1][a] for p in valid_pairs) for a in range(3)]
        maxs = [max(p[1][a] for p in valid_pairs) for a in range(3)]
        train_pool = [
            (bars[i].day, self._normalize(feat, mins, maxs), target)
            for i, feat, target in valid_pairs
        ]

        # Optional Eq. (340) linear neighbor weights fit strictly on training samples.
        k = int(cfg["k"]) if cfg.get("k") else max(3, int(math.sqrt(len(train_pool))))
        linear_beta: list[float] | None = None
        linear_intercept = 0.0
        if linear_weights and len(train_pool) > k + horizon + 3:
            reg_x: list[list[float]] = []
            reg_y: list[float] = []
            for idx in range(k + horizon, len(train_pool)):
                _, x_cur, y_cur = train_pool[idx]
                eligible = train_pool[: idx - horizon + 1]
                if len(eligible) < k:
                    continue
                nearest = sorted(eligible, key=lambda item: self._euclidean_sq(x_cur, item[1]))[:k]
                reg_x.append([item[2] for item in nearest] + [1.0])
                reg_y.append(y_cur)
            if len(reg_x) >= k + 2:
                xt = mat_transpose(reg_x)
                gram = mat_mul(xt, reg_x)
                dim = len(gram)
                for d in range(dim):
                    gram[d][d] += 1e-3
                rhs = mat_vec(xt, reg_y)
                coeffs = solve_linear_system(gram, rhs)
                linear_beta = coeffs[:-1]
                linear_intercept = coeffs[-1]

        return {
            "symbol": symbol,
            "mins": mins,
            "maxs": maxs,
            "train_pool": train_pool,
            "linear_beta": linear_beta,
            "linear_intercept": linear_intercept,
        }

    def generate_signals(
        self,
        store: FeatureStore,
        params: Mapping[str, Any],
        fitted_state: Mapping[str, Any],
    ) -> SignalFrame:
        cfg = {**self.metadata.default_params, **params}
        symbol = str(fitted_state.get("symbol") or _resolve_symbol(store, cfg))
        horizon = int(cfg["horizon"])
        vol_ma = int(cfg["vol_ma"])
        p_short = int(cfg["price_ma_short"])
        p_long = int(cfg["price_ma_long"])
        sample_size = int(cfg["sample_size"])
        z1 = float(cfg["z1"])
        z2 = float(cfg["z2"])
        stationary = bool(cfg.get("use_stationary_ratios", True))
        allow_short = bool(cfg.get("allow_short", True))
        if z1 < 0.0 or z2 < 0.0 or z2 > z1:
            raise ValueError("KNN thresholds must satisfy 0 <= z2 <= z1")

        mins = fitted_state["mins"]
        maxs = fitted_state["maxs"]
        base_pool: list[tuple[str, tuple[float, float, float], float]] = list(fitted_state.get("train_pool", ()))
        linear_beta = fitted_state.get("linear_beta")
        linear_intercept = float(fitted_state.get("linear_intercept", 0.0))

        bars = store.bars(symbol)
        closes = _bar_closes(bars)
        volumes = _bar_volumes(bars)
        raw_feats = self._extract_raw_features(closes, volumes, vol_ma, p_short, p_long, stationary)
        norm_feats = [self._normalize(f, mins, maxs) if f is not None else None for f in raw_feats]

        out: SignalFrame = {}
        state = 0.0
        # Maintain realized pool strictly <= t (only samples where t' + horizon <= t).
        realized_eval: dict[str, tuple[tuple[float, float, float], float]] = {}

        for i, bar in enumerate(bars):
            # Add sample from (i - horizon) now that its forward target close[i]/close[i-horizon]-1 is realized.
            if i >= horizon and norm_feats[i - horizon] is not None:
                prev_day = bars[i - horizon].day
                realized_ret = closes[i] / closes[i - horizon] - 1.0
                realized_eval[prev_day] = (norm_feats[i - horizon], realized_ret)  # type: ignore[assignment]

            x_cur = norm_feats[i]
            if x_cur is None:
                out[bar.day] = {symbol: 0.0}
                continue

            # Combine train pool strictly older than current day - horizon with realized eval history.
            cutoff_idx = max(0, i - horizon)
            cutoff_day = bars[cutoff_idx].day if i >= horizon else ""
            pool: list[tuple[str, tuple[float, float, float], float]] = [
                item for item in base_pool if cutoff_day == "" or item[0] <= cutoff_day
            ]
            seen_days = {item[0] for item in pool}
            for d_key, (nf, ret_val) in realized_eval.items():
                if d_key not in seen_days:
                    pool.append((d_key, nf, ret_val))

            pool.sort(key=lambda item: item[0])
            if sample_size > 0 and len(pool) > sample_size:
                pool = pool[-sample_size:]

            k = int(cfg["k"]) if cfg.get("k") else max(3, int(math.sqrt(max(1, len(pool)))))
            if len(pool) < max(3, k):
                out[bar.day] = {symbol: 0.0}
                continue

            nearest = sorted(pool, key=lambda item: self._euclidean_sq(x_cur, item[1]))[:k]
            if linear_beta is not None and len(linear_beta) == len(nearest):
                pred = sum(item[2] * w for item, w in zip(nearest, linear_beta)) + linear_intercept
            else:
                pred = sum(item[2] for item in nearest) / len(nearest)

            # Eq. (341) hysteresis trading rule
            if state == 0.0:
                if pred > z1:
                    state = 1.0
                elif pred < -z1 and allow_short:
                    state = -1.0
            elif state > 0.0:
                if pred < -z1 and allow_short:
                    state = -1.0
                elif pred <= z2:
                    state = 0.0
            else:
                if pred > z1:
                    state = 1.0
                elif pred >= -z2:
                    state = 0.0

            out[bar.day] = {symbol: state}
        return out


class StatArbOptimizationStrategy:
    """Section 3.18 / 3.18.1: Dollar-neutral Sharpe-maximizing statistical arbitrage."""

    metadata = StrategyMetadata(
        strategy_key="stat_arb_optimization",
        name="Statistical Arbitrage Optimization (Sec 3.18)",
        version="1.0.0",
        source_id="ks_3_18_stat_arb",
        portfolio="swing",
        research_only=True,
        permitted_regimes=("chop", "bull", "bear", "high_volatility"),
        default_params={
            "cov_window": 40,
            "signal_lookback": 5,
            "shrinkage": 0.25,
            "ridge": 1e-6,
            "dollar_neutral": True,
            "max_abs_weight": 0.35,
        },
        param_grid=(
            {"cov_window": 30, "signal_lookback": 3, "shrinkage": 0.25, "dollar_neutral": True},
            {"cov_window": 40, "signal_lookback": 5, "shrinkage": 0.25, "dollar_neutral": True},
            {"cov_window": 60, "signal_lookback": 10, "shrinkage": 0.35, "dollar_neutral": True},
        ),
        description=(
            "Cross-sectional mean-reversion expected returns combined with shrinkage-regularized "
            "covariance inversion under Eq. (358) dollar-neutrality and unit gross exposure."
        ),
    )

    def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
        symbols = list(params.get("symbols") or train_store.symbols)
        if len(symbols) < 2:
            raise ValueError("Statistical arbitrage requires at least 2 symbols")
        return {"symbols": symbols}

    @staticmethod
    def _normalize_and_cap(
        raw_weights: Sequence[float],
        dollar_neutral: bool,
        max_abs_weight: float,
    ) -> list[float]:
        w = list(raw_weights)
        n = len(w)
        if n == 0:
            return []
        for _ in range(12):
            if dollar_neutral:
                mean_w = sum(w) / n
                w = [x - mean_w for x in w]
            gross = sum(abs(x) for x in w)
            if gross <= 1e-12:
                return [0.0] * n
            w = [x / gross for x in w]
            if max(abs(x) for x in w) <= max_abs_weight + 1e-9:
                break
            w = [max(-max_abs_weight, min(max_abs_weight, x)) for x in w]
        if dollar_neutral:
            mean_w = sum(w) / n
            w = [x - mean_w for x in w]
        gross = sum(abs(x) for x in w)
        return [x / gross for x in w] if gross > 1e-12 else [0.0] * n

    def generate_signals(
        self,
        store: FeatureStore,
        params: Mapping[str, Any],
        fitted_state: Mapping[str, Any],
    ) -> SignalFrame:
        cfg = {**self.metadata.default_params, **params}
        symbols = list(fitted_state.get("symbols") or store.symbols)
        symbols = [s for s in symbols if s in store.symbols]
        if len(symbols) < 2:
            raise ValueError("Statistical arbitrage requires at least 2 symbols present in FeatureStore")

        cov_window = int(cfg["cov_window"])
        sig_lb = int(cfg["signal_lookback"])
        shrinkage = float(cfg["shrinkage"])
        ridge = float(cfg["ridge"])
        dollar_neutral = bool(cfg.get("dollar_neutral", True))
        max_abs_weight = float(cfg.get("max_abs_weight", 0.35))
        warmup = max(cov_window, sig_lb) + 1

        closes_by_sym = {s: _bar_closes(store.bars(s)) for s in symbols}
        n_sym = len(symbols)
        n_days = len(store.dates)
        # Daily simple returns r[sym_idx][t] = P[t]/P[t-1] - 1
        rets = [
            [0.0] + [closes_by_sym[s][t] / closes_by_sym[s][t - 1] - 1.0 for t in range(1, n_days)]
            for s in symbols
        ]

        out: SignalFrame = {}
        for t, day in enumerate(store.dates):
            if t < warmup:
                out[day] = {s: 0.0 for s in symbols}
                continue

            # Causal expected returns E_i: cross-sectionally demeaned short-horizon reversal.
            cum_rets = [closes_by_sym[s][t] / closes_by_sym[s][t - sig_lb] - 1.0 for s in symbols]
            cs_mean = sum(cum_rets) / n_sym
            expected = [-(r - cs_mean) for r in cum_rets]

            # Trailing sample covariance strictly from [t - cov_window + 1, t]
            win_start = t - cov_window + 1
            win_rets = [rets[idx][win_start : t + 1] for idx in range(n_sym)]
            means = [sample_mean(r) for r in win_rets]
            cov = [[0.0] * n_sym for _ in range(n_sym)]
            denom = max(1, cov_window - 1)
            for i in range(n_sym):
                for j in range(i, n_sym):
                    c_ij = sum((win_rets[i][k] - means[i]) * (win_rets[j][k] - means[j]) for k in range(cov_window)) / denom
                    cov[i][j] = cov[j][i] = c_ij

            # Diagonal shrinkage regularization (Footnote 61)
            c_reg = [
                [
                    (cov[i][i] if cov[i][i] > 1e-10 else 1e-6) + ridge
                    if i == j
                    else (1.0 - shrinkage) * cov[i][j]
                    for j in range(n_sym)
                ]
                for i in range(n_sym)
            ]
            c_inv = invert_symmetric_regularized(c_reg, ridge=ridge)
            u = mat_vec(c_inv, expected)
            if dollar_neutral:
                ones = [1.0] * n_sym
                v = mat_vec(c_inv, ones)
                denom_v = sum(v)
                if abs(denom_v) <= 1e-12:
                    raw_w = [0.0] * n_sym
                else:
                    scale = sum(u) / denom_v
                    raw_w = [u[i] - v[i] * scale for i in range(n_sym)]
            else:
                raw_w = u

            weights = self._normalize_and_cap(raw_w, dollar_neutral=dollar_neutral, max_abs_weight=max_abs_weight)
            out[day] = {symbols[i]: weights[i] for i in range(n_sym)}
        return out


class NNQuantileForecastStrategy:
    """Section 18.2: Feedforward ReLU + Softmax MLP forecasting K normalized return quantiles."""

    metadata = StrategyMetadata(
        strategy_key="nn_quantile_forecast",
        name="Neural Network Quantile Forecast (Sec 18.2)",
        version="1.0.0",
        source_id="ks_18_2_ann_quantile",
        portfolio="swing",
        research_only=True,
        permitted_regimes=("strong_bull", "bull", "chop", "bear", "strong_bear"),
        default_params={
            "symbol": "SPY",
            "norm_window": 20,
            "ema_taus": (3, 5, 10, 20),
            "rsi_taus": (5, 10, 20),
            "n_quantiles": 5,
            "hidden_units": 12,
            "epochs": 30,
            "learning_rate": 0.03,
            "l2_reg": 0.001,
            "trade_outer_quantiles": 1,
            "seed": 17,
            "allow_short": True,
        },
        param_grid=(
            {"n_quantiles": 5, "hidden_units": 8, "epochs": 25, "learning_rate": 0.03},
            {"n_quantiles": 5, "hidden_units": 12, "epochs": 30, "learning_rate": 0.03},
            {"n_quantiles": 3, "hidden_units": 10, "epochs": 35, "learning_rate": 0.025},
        ),
        description=(
            "ReLU hidden layer + stabilized Softmax output MLP over causal standardized-return "
            "EMA, EMSD, and RSI features (Eqs. 521-537) predicting next-bar return quantiles."
        ),
    )

    @staticmethod
    def _build_features_and_rhat(
        closes: Sequence[float],
        norm_window: int,
        ema_taus: Sequence[int],
        rsi_taus: Sequence[int],
    ) -> tuple[list[float | None], list[list[float] | None]]:
        n = len(closes)
        raw_rets = [0.0] + [closes[i] / closes[i - 1] - 1.0 for i in range(1, n)]
        r_hat: list[float | None] = [None] * n
        for t in range(norm_window + 1, n):
            # Eqs. (522)-(525): strictly prior T1 returns [t-T1 .. t-1]
            prior = raw_rets[t - norm_window : t]
            mu = sample_mean(prior)
            sig = max(sample_std(prior, ddof=1), 1e-6)
            r_hat[t] = max(-6.0, min(6.0, (raw_rets[t] - mu) / sig))

        max_tau = max(max(ema_taus), max(rsi_taus))
        features: list[list[float] | None] = [None] * n
        start_idx = norm_window + max_tau + 1

        for t in range(start_idx, n):
            cur_r = r_hat[t]
            if cur_r is None:
                continue
            vec = [cur_r]
            valid = True
            for tau in ema_taus:
                window_vals = r_hat[t - tau : t]
                if any(v is None for v in window_vals):
                    valid = False
                    break
                # Reverse order: lag k=0 is t-1, lag k=tau-1 is t-tau (Eq. 526)
                lam = (tau - 1.0) / (tau + 1.0)
                w_norm = (1.0 - lam) / max(1.0 - (lam ** tau), 1e-12)
                ema_val = 0.0
                for lag in range(tau):
                    ema_val += (lam ** lag) * float(window_vals[tau - 1 - lag])  # type: ignore[arg-type]
                ema_val *= w_norm
                # Eq. (527) weighted variance with positive denominator safeguard
                emsd_scale = (1.0 - lam) / max(lam - (lam ** tau), 1e-9) if tau > 1 else 1.0
                emsd_sq = 0.0
                for lag in range(tau):
                    diff = float(window_vals[tau - 1 - lag]) - ema_val  # type: ignore[arg-type]
                    emsd_sq += (lam ** lag) * (diff * diff)
                emsd_val = math.sqrt(max(0.0, emsd_scale * emsd_sq))
                vec.append(ema_val)
                vec.append(emsd_val)
            if not valid:
                continue
            for tau in rsi_taus:
                window_vals = r_hat[t - tau : t]
                if any(v is None for v in window_vals):
                    valid = False
                    break
                # Eqs. (528)-(529)
                nu_pos = sum(max(float(v), 0.0) for v in window_vals)  # type: ignore[arg-type]
                nu_neg = sum(max(-float(v), 0.0) for v in window_vals)  # type: ignore[arg-type]
                denom = nu_pos + nu_neg
                rsi_val = (nu_pos / denom) if denom > 1e-12 else 0.5
                vec.append(rsi_val)
            if valid:
                features[t] = vec
        return r_hat, features

    @staticmethod
    def _forward(
        x: Sequence[float],
        w1: Sequence[Sequence[float]],
        b1: Sequence[float],
        w2: Sequence[Sequence[float]],
        b2: Sequence[float],
    ) -> tuple[list[float], list[float], list[float]]:
        # Hidden layer: Eq. (533) + Eq. (534) ReLU
        z1 = [sum(w1[h][j] * x[j] for j in range(len(x))) + b1[h] for h in range(len(b1))]
        a1 = [v if v > 0.0 else 0.0 for v in z1]
        # Output layer: Eq. (533) + stabilized Eq. (535) Softmax
        logits = [sum(w2[c][h] * a1[h] for h in range(len(a1))) + b2[c] for c in range(len(b2))]
        probs = _stable_softmax(logits)
        return z1, a1, probs

    def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
        cfg = {**self.metadata.default_params, **params}
        symbol = _resolve_symbol(train_store, cfg)
        norm_window = int(cfg["norm_window"])
        ema_taus = tuple(int(t) for t in cfg["ema_taus"])
        rsi_taus = tuple(int(t) for t in cfg["rsi_taus"])
        k_classes = int(cfg["n_quantiles"])
        hidden = int(cfg["hidden_units"])
        epochs = int(cfg["epochs"])
        lr = float(cfg["learning_rate"])
        l2 = float(cfg["l2_reg"])
        seed = int(cfg["seed"])

        closes = _bar_closes(train_store.bars(symbol))
        r_hat, features = self._build_features_and_rhat(closes, norm_window, ema_taus, rsi_taus)

        # Supervised pairs: features at t -> target R_hat(t+1) strictly within train_store
        x_raw: list[list[float]] = []
        y_vals: list[float] = []
        for t in range(len(closes) - 1):
            if features[t] is not None and r_hat[t + 1] is not None:
                x_raw.append(features[t])  # type: ignore[arg-type]
                y_vals.append(float(r_hat[t + 1]))  # type: ignore[arg-type]

        if len(x_raw) < max(12, k_classes * 2):
            return {"symbol": symbol, "ready": False}

        # Standardize input features strictly on training set
        n_feat = len(x_raw[0])
        feat_means = [sample_mean([row[j] for row in x_raw]) for j in range(n_feat)]
        feat_stds = [max(sample_std([row[j] for row in x_raw], ddof=1), 1e-6) for j in range(n_feat)]
        x_train = [
            [max(-5.0, min(5.0, (row[j] - feat_means[j]) / feat_stds[j])) for j in range(n_feat)]
            for row in x_raw
        ]

        # Quantile cutoffs q_1..q_{K-1} computed strictly on training targets (Eq. 530)
        cutoffs = _quantiles(sorted(y_vals), k_classes)
        y_labels = [_assign_quantile(v, cutoffs) for v in y_vals]

        rng = random.Random(seed)
        scale1 = math.sqrt(2.0 / max(1, n_feat))
        scale2 = math.sqrt(1.0 / max(1, hidden))
        w1 = [[rng.gauss(0.0, scale1) for _ in range(n_feat)] for _ in range(hidden)]
        b1 = [0.0] * hidden
        w2 = [[rng.gauss(0.0, scale2) for _ in range(hidden)] for _ in range(k_classes)]
        b2 = [0.0] * k_classes

        n_samples = len(x_train)
        indices = list(range(n_samples))
        for _ in range(epochs):
            rng.shuffle(indices)
            for idx in indices:
                x = x_train[idx]
                target_c = y_labels[idx]
                z1, a1, probs = self._forward(x, w1, b1, w2, b2)

                # Gradient of cross-entropy loss Eq. (536): dE/dlogits = p - S
                dlogits = [probs[c] - (1.0 if c == target_c else 0.0) for c in range(k_classes)]
                dh = [0.0] * hidden
                for c in range(k_classes):
                    dc = dlogits[c]
                    b2[c] -= lr * dc
                    for h in range(hidden):
                        dh[h] += w2[c][h] * dc
                        w2[c][h] -= lr * (dc * a1[h] + l2 * w2[c][h])

                for h in range(hidden):
                    if z1[h] <= 0.0:
                        continue
                    dz = dh[h]
                    b1[h] -= lr * dz
                    for j in range(n_feat):
                        w1[h][j] -= lr * (dz * x[j] + l2 * w1[h][j])

        return {
            "symbol": symbol,
            "ready": True,
            "feat_means": feat_means,
            "feat_stds": feat_stds,
            "cutoffs": cutoffs,
            "w1": w1,
            "b1": b1,
            "w2": w2,
            "b2": b2,
            "k_classes": k_classes,
        }

    def generate_signals(
        self,
        store: FeatureStore,
        params: Mapping[str, Any],
        fitted_state: Mapping[str, Any],
    ) -> SignalFrame:
        cfg = {**self.metadata.default_params, **params}
        symbol = str(fitted_state.get("symbol") or _resolve_symbol(store, cfg))
        bars = store.bars(symbol)
        if not fitted_state.get("ready"):
            return {b.day: {symbol: 0.0} for b in bars}

        norm_window = int(cfg["norm_window"])
        ema_taus = tuple(int(t) for t in cfg["ema_taus"])
        rsi_taus = tuple(int(t) for t in cfg["rsi_taus"])
        outer = max(1, int(cfg.get("trade_outer_quantiles", 1)))
        allow_short = bool(cfg.get("allow_short", True))

        feat_means = fitted_state["feat_means"]
        feat_stds = fitted_state["feat_stds"]
        w1, b1 = fitted_state["w1"], fitted_state["b1"]
        w2, b2 = fitted_state["w2"], fitted_state["b2"]
        k_classes = int(fitted_state["k_classes"])

        closes = _bar_closes(bars)
        _, features = self._build_features_and_rhat(closes, norm_window, ema_taus, rsi_taus)

        out: SignalFrame = {}
        for i, bar in enumerate(bars):
            raw = features[i]
            if raw is None:
                out[bar.day] = {symbol: 0.0}
                continue
            x = [max(-5.0, min(5.0, (raw[j] - feat_means[j]) / feat_stds[j])) for j in range(len(raw))]
            _, _, probs = self._forward(x, w1, b1, w2, b2)
            best_c = max(range(k_classes), key=lambda c: probs[c])
            # Eq. (537) quantile signal rule
            if best_c >= k_classes - outer:
                weight = 1.0
            elif best_c < outer and allow_short:
                weight = -1.0
            else:
                weight = 0.0
            out[bar.day] = {symbol: weight}
        return out


class BernoulliNBSentimentStrategy:
    """Section 18.3: Bernoulli Naive Bayes sentiment forecasting on timestamped text."""

    metadata = StrategyMetadata(
        strategy_key="bernoulli_nb_sentiment",
        name="Bernoulli Naive Bayes Sentiment (Sec 18.3)",
        version="1.0.0",
        source_id="ks_18_3_bernoulli_nb",
        portfolio="swing",
        research_only=True,
        permitted_regimes=("strong_bull", "bull", "chop", "bear", "strong_bear", "high_volatility"),
        default_params={
            "symbol": "SPY",
            "horizon": 1,
            "n_classes": 2,
            "min_doc_freq": 2,
            "max_vocab": 150,
            "laplace_alpha": 1.0,
            "prob_threshold": 0.55,
            "lookback_hours": 24,
            "allow_short": True,
        },
        param_grid=(
            {"laplace_alpha": 1.0, "prob_threshold": 0.55, "min_doc_freq": 2},
            {"laplace_alpha": 1.0, "prob_threshold": 0.62, "min_doc_freq": 2},
            {"laplace_alpha": 2.0, "prob_threshold": 0.58, "min_doc_freq": 3},
        ),
        description=(
            "Bernoulli Naive Bayes classifier (Eqs. 538-546) over deduplicated, stemmed US equity "
            "news/headlines strictly published <= decision timestamp t."
        ),
    )

    @staticmethod
    def _window_docs(
        docs: Sequence[TextObservation],
        as_of_ts,
        lookback_hours: int,
    ) -> list[frozenset[str]]:
        start_ts = as_of_ts - timedelta(hours=lookback_hours)
        # Deduplicate identical cleaned headlines within the lookback window (Footnote 230)
        seen: set[str] = set()
        token_sets: list[frozenset[str]] = []
        for doc in docs:
            if doc.published_at > as_of_ts:
                break
            if doc.published_at < start_ts:
                continue
            norm_text = " ".join(doc.text.lower().split())
            if norm_text in seen:
                continue
            seen.add(norm_text)
            toks = preprocess_text(norm_text)
            if toks:
                token_sets.append(toks)
        return token_sets

    def fit(self, train_store: FeatureStore, params: Mapping[str, Any]) -> dict[str, Any]:
        cfg = {**self.metadata.default_params, **params}
        symbol = _resolve_symbol(train_store, cfg)
        horizon = int(cfg["horizon"])
        n_classes = int(cfg["n_classes"])
        min_df = int(cfg["min_doc_freq"])
        max_vocab = int(cfg["max_vocab"])
        alpha = float(cfg["laplace_alpha"])
        lookback_hours = int(cfg["lookback_hours"])

        bars = train_store.bars(symbol)
        docs = train_store.texts(symbol)
        closes = _bar_closes(bars)
        if len(bars) <= horizon or not docs:
            return {"symbol": symbol, "ready": False}

        # Collect (token_sets, forward_return) strictly inside training split
        samples: list[tuple[list[frozenset[str]], float]] = []
        for i in range(len(bars) - horizon):
            w_docs = self._window_docs(docs, bars[i].timestamp, lookback_hours)
            if not w_docs:
                continue
            fwd_ret = closes[i + horizon] / closes[i] - 1.0
            samples.append((w_docs, fwd_ret))

        if len(samples) < max(8, n_classes * 3):
            return {"symbol": symbol, "ready": False}

        cutoffs = _quantiles(sorted(s[1] for s in samples), n_classes) if n_classes > 2 else [0.0]

        # Build vocabulary strictly on training documents
        doc_freq: dict[str, int] = {}
        for w_docs, _ in samples:
            for toks in w_docs:
                for tok in toks:
                    doc_freq[tok] = doc_freq.get(tok, 0) + 1
        vocab = sorted(
            (tok for tok, df in doc_freq.items() if df >= min_df),
            key=lambda tok: (-doc_freq[tok], tok),
        )[:max_vocab]
        if not vocab:
            return {"symbol": symbol, "ready": False}

        vocab_index = {w: idx for idx, w in enumerate(vocab)}
        class_sample_counts = [0] * n_classes
        class_doc_counts = [0] * n_classes
        word_class_counts = [[0] * len(vocab) for _ in range(n_classes)]

        for w_docs, fwd_ret in samples:
            c = _assign_quantile(fwd_ret, cutoffs)
            class_sample_counts[c] += 1
            for toks in w_docs:
                class_doc_counts[c] += 1
                for tok in toks:
                    idx = vocab_index.get(tok)
                    if idx is not None:
                        word_class_counts[c][idx] += 1

        total_samples = sum(class_sample_counts)
        log_priors = [
            math.log((class_sample_counts[c] + alpha) / (total_samples + n_classes * alpha))
            for c in range(n_classes)
        ]
        # Bernoulli conditional probabilities P(w_a | C_alpha) with Laplace smoothing
        cond_probs = [
            [
                (word_class_counts[c][a] + alpha) / (class_doc_counts[c] + 2.0 * alpha)
                for a in range(len(vocab))
            ]
            for c in range(n_classes)
        ]

        return {
            "symbol": symbol,
            "ready": True,
            "vocab": vocab,
            "n_classes": n_classes,
            "log_priors": log_priors,
            "cond_probs": cond_probs,
        }

    def generate_signals(
        self,
        store: FeatureStore,
        params: Mapping[str, Any],
        fitted_state: Mapping[str, Any],
    ) -> SignalFrame:
        cfg = {**self.metadata.default_params, **params}
        symbol = str(fitted_state.get("symbol") or _resolve_symbol(store, cfg))
        bars = store.bars(symbol)
        if not fitted_state.get("ready"):
            return {b.day: {symbol: 0.0} for b in bars}

        vocab: Sequence[str] = fitted_state["vocab"]
        n_classes = int(fitted_state["n_classes"])
        log_priors: Sequence[float] = fitted_state["log_priors"]
        cond_probs: Sequence[Sequence[float]] = fitted_state["cond_probs"]
        lookback_hours = int(cfg["lookback_hours"])
        threshold = float(cfg["prob_threshold"])
        allow_short = bool(cfg.get("allow_short", True))

        docs = store.texts(symbol)
        out: SignalFrame = {}
        for bar in bars:
            w_docs = self._window_docs(docs, bar.timestamp, lookback_hours)
            if not w_docs:
                out[bar.day] = {symbol: 0.0}
                continue
            # Eq. (546) evaluated in log-space for numerical stability
            log_scores = list(log_priors)
            for toks in w_docs:
                for c in range(n_classes):
                    row = cond_probs[c]
                    acc = 0.0
                    for a, word in enumerate(vocab):
                        p = row[a]
                        acc += math.log(p) if word in toks else math.log(1.0 - p)
                    log_scores[c] += acc

            probs = _stable_softmax(log_scores)
            best_c = max(range(n_classes), key=lambda c: probs[c])
            if best_c == n_classes - 1 and probs[best_c] >= threshold:
                weight = 1.0
            elif best_c == 0 and probs[best_c] >= threshold and allow_short:
                weight = -1.0
            else:
                weight = 0.0
            out[bar.day] = {symbol: weight}
        return out


STRATEGY_CATALOG: dict[str, ResearchStrategy] = {
    "hp_trend_filter": HPTrendFilterStrategy(),
    "knn_return_forecast": KNNReturnForecastStrategy(),
    "stat_arb_optimization": StatArbOptimizationStrategy(),
    "nn_quantile_forecast": NNQuantileForecastStrategy(),
    "bernoulli_nb_sentiment": BernoulliNBSentimentStrategy(),
}


def get_strategy(strategy_key: str) -> ResearchStrategy:
    if strategy_key not in STRATEGY_CATALOG:
        raise ValueError(f"Unknown research strategy: {strategy_key!r}")
    return STRATEGY_CATALOG[strategy_key]


def list_catalog() -> list[dict[str, object]]:
    return [strat.metadata.to_dict() for strat in STRATEGY_CATALOG.values()]
