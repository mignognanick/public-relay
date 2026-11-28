"""Research-only daily equity and ETF adapter around vectorbt 1.2.1.

vectorbt is Apache-2.0 with the Commons Clause: this module uses it for internal
research and does not redistribute it. The adapter never reads brokerage credentials,
never places orders, and never promotes a strategy.

Same-bar execution at the open would look ahead, because the signal uses that bar's
close. Decisions are stored on the close that produced them and filled on the next
session's open. Percent slippage is the only vectorbt cost; per-share commission and
the independent risk engine stay on the existing research path. Intraday option fills
are refused rather than approximated.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal, Mapping, Never, Sequence
from uuid import uuid4

import pandas as pd
import vectorbt as vbt

from alpha.registry import AlphaRegistry, DailyAlphaReturn
from research.feature_store import FeatureStore, parse_daily_ohlcv_csv
from research.library import (
    TRADING_DAYS_PER_YEAR,
    ZERO,
    classify_research_regime,
    compute_return_metrics,
    dec_from_float,
    dec_str,
)
from research.validator import ExecutionCostModel, simulate_signal_frame

AssetClass = Literal["equity", "etf"]
StrategyName = Literal["sma_crossover", "trailing_momentum"]
DatasetKind = Literal["historical", "synthetic"]

VECTORBT_VERSION = vbt.__version__
SOURCE_ID = "vectorbt_1_2_1"
HARD_TRIAL_CAP = 48
OPTIONS_NOT_MODELED = (
    "VectorBT results are daily equity and ETF simulations. They do not reproduce "
    "intraday options execution, spreads, expiration, assignment, or the live relay."
)


def _never(value: Never) -> Never:
    raise RuntimeError(f"Unhandled variant: {value!r}")


@dataclass(frozen=True)
class VectorBTBudget:
    """Local compute cap. Incremental cash cost of this adapter is zero unless a caller sets one."""

    max_trials: int = 8
    max_compute_seconds: float = 60.0
    max_incremental_usd: Decimal = Decimal("30")

    def __post_init__(self) -> None:
        if self.max_trials < 1 or self.max_trials > HARD_TRIAL_CAP:
            raise ValueError(f"max_trials must be in 1..{HARD_TRIAL_CAP}")
        if self.max_compute_seconds <= 0:
            raise ValueError("max_compute_seconds must be > 0")
        if self.max_incremental_usd < ZERO:
            raise ValueError("max_incremental_usd must be >= 0")


@dataclass(frozen=True)
class VectorBTCosts:
    """Bps costs. VectorBT applies the one-way rate as slippage. Commission is research-engine only."""

    spread_bps: Decimal = Decimal("2")
    slippage_bps: Decimal = Decimal("2")
    commission_per_share: Decimal = Decimal("0.005")
    stop_pct: Decimal = Decimal("2")

    @property
    def one_way_rate(self) -> Decimal:
        return (self.spread_bps / Decimal("2") + self.slippage_bps) / Decimal("10000")

    def research_model(self) -> ExecutionCostModel:
        return ExecutionCostModel(
            spread_bps=self.spread_bps,
            slippage_bps=self.slippage_bps,
            commission_per_share=self.commission_per_share,
            stop_pct=self.stop_pct,
        )


DEFAULT_GRIDS: dict[str, tuple[dict[str, int], ...]] = {
    "sma_crossover": (
        {"fast": 5, "slow": 20},
        {"fast": 8, "slow": 21},
        {"fast": 10, "slow": 30},
    ),
    "trailing_momentum": (
        {"lookback": 10},
        {"lookback": 20},
        {"lookback": 40},
    ),
}


def estimated_incremental_monthly_usd() -> Decimal:
    """This adapter runs locally and does not call a paid data or model API."""
    return Decimal("0.00")


def load_daily_bars(
    csv_text: str,
    *,
    kind: DatasetKind,
    source: str,
    default_symbol: str | None = None,
) -> FeatureStore:
    """Load caller-supplied daily bars. This function does not download market data."""
    parsed = parse_daily_ohlcv_csv(csv_text, default_symbol=default_symbol)
    return FeatureStore.from_bars(parsed, kind=kind, source=source)


def assert_daily_equity_etf(asset_class: str, bar_frequency: str) -> None:
    if asset_class not in ("equity", "etf"):
        raise ValueError(
            f"Refusing asset class {asset_class!r}. {OPTIONS_NOT_MODELED}"
        )
    if bar_frequency != "1D":
        raise ValueError(
            f"Refusing bar frequency {bar_frequency!r}. {OPTIONS_NOT_MODELED}"
        )


def _price_frame(store: FeatureStore, symbol: str) -> tuple[pd.Series, pd.Series, list[str]]:
    symbol = symbol.upper()
    if symbol not in store.symbols:
        raise ValueError(f"Symbol {symbol} is not in the dataset")
    bars = store.bars(symbol)
    days = [bar.day for bar in bars]
    if len(days) != len(set(days)):
        raise ValueError(
            "VectorBT adapter refuses intraday bars. Pass one equity or ETF bar per session."
        )
    if days != list(store.dates):
        raise ValueError("Symbol calendar does not match the aligned dataset dates")
    index = pd.to_datetime(days)
    # vectorbt's engine is floating point. Reported money is converted back to Decimal.
    close = pd.Series([float(bar.close) for bar in bars], index=index, dtype="float64")
    opened = pd.Series([float(bar.open) for bar in bars], index=index, dtype="float64")
    return close, opened, days


def sma_decision_weights(close: pd.Series, fast: int, slow: int) -> pd.Series:
    if fast < 2 or slow <= fast:
        raise ValueError("SMA fast window must be >= 2 and strictly shorter than the slow window")
    averages = vbt.MA.run(close, window=[fast, slow]).ma
    fast_ma = averages[fast]
    slow_ma = averages[slow]
    long = (fast_ma > slow_ma) & fast_ma.notna() & slow_ma.notna()
    return long.astype(float)


def momentum_decision_weights(close: pd.Series, lookback: int) -> pd.Series:
    if lookback < 2:
        raise ValueError("Momentum lookback must be >= 2")
    lagged = close.shift(lookback)
    trailing = close / lagged - 1.0
    long = (trailing > 0.0) & trailing.notna()
    return long.astype(float)


def decision_weights(strategy: StrategyName, close: pd.Series, params: Mapping[str, Any]) -> pd.Series:
    match strategy:
        case "sma_crossover":
            return sma_decision_weights(close, int(params["fast"]), int(params["slow"]))
        case "trailing_momentum":
            return momentum_decision_weights(close, int(params["lookback"]))
        case _ as unreachable:
            _never(unreachable)


def signals_from_decisions(
    decisions: pd.Series,
    prior_decision: float = 0.0,
) -> tuple[pd.Series, pd.Series]:
    """Turn close decisions into next-open entries and exits. The slice starts flat."""
    values = [float(value) for value in decisions.tolist()]
    positioned: list[bool] = []
    previous = float(prior_decision)
    for value in values:
        positioned.append(previous > 0.0)
        previous = value
    entries: list[bool] = []
    exits: list[bool] = []
    was_long = False
    for flag in positioned:
        entries.append(flag and not was_long)
        exits.append((not flag) and was_long)
        was_long = flag
    return (
        pd.Series(entries, index=decisions.index, dtype=bool),
        pd.Series(exits, index=decisions.index, dtype=bool),
    )


def simulate_long_flat(
    close: pd.Series,
    opened: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    *,
    init_cash: float,
    slippage: float,
) -> Any:
    return vbt.Portfolio.from_signals(
        close,
        entries,
        exits,
        price=opened,
        fees=0.0,
        slippage=slippage,
        freq="1D",
        init_cash=init_cash,
        direction="longonly",
    )


def reference_equity_path(
    close: pd.Series,
    opened: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    *,
    init_cash: float,
    slippage: float,
) -> list[float]:
    """Next-open, full-notional, long-or-flat path used to audit vectorbt fills.

    This is an audit oracle for the external float engine, not a broker fill model.
    """
    cash = float(init_cash)
    shares = 0.0
    equities: list[float] = []
    for i in range(len(close)):
        if bool(entries.iloc[i]) and shares == 0.0:
            fill = float(opened.iloc[i]) * (1.0 + slippage)
            shares = cash / fill
            cash = 0.0
        elif bool(exits.iloc[i]) and shares > 0.0:
            fill = float(opened.iloc[i]) * (1.0 - slippage)
            cash = shares * fill
            shares = 0.0
        equities.append(cash + shares * float(close.iloc[i]))
    return equities


def _returns_from_equity(init_cash: float, equities: Sequence[float]) -> list[float]:
    previous = float(init_cash)
    out: list[float] = []
    for equity in equities:
        out.append((equity / previous - 1.0) if previous else 0.0)
        previous = equity
    return out


def _float_list(obj: Any) -> list[float]:
    if hasattr(obj, "to_numpy"):
        array = obj.to_numpy()
    else:
        array = obj
    flat = getattr(array, "ravel", lambda: array)()
    return [float(value) for value in flat]


def _trade_count(portfolio: Any) -> int:
    count = portfolio.trades.count()
    if hasattr(count, "item"):
        return int(count.item())
    return int(count)


def _metrics(dates: Sequence[str], returns: Sequence[float], initial_equity: Decimal) -> dict[str, object]:
    daily = {day: dec_from_float(value, day) for day, value in zip(dates, returns, strict=True)}
    return compute_return_metrics(daily, initial_equity=initial_equity)


def _sharpe(metrics: Mapping[str, object]) -> float:
    return float(metrics["sharpe_ratio"])  # type: ignore[arg-type]


def audit_decision_causality(
    close: pd.Series,
    strategy: StrategyName,
    params: Mapping[str, Any],
) -> tuple[bool, tuple[str, ...]]:
    """Prefix invariance: a shorter history must not change earlier decisions."""
    if len(close) < 40:
        return False, ("Insufficient sessions (<40) to audit vectorbt signals",)
    full = decision_weights(strategy, close, params)
    errors: list[str] = []
    for fraction in (0.6, 0.8, 0.9):
        cutoff = min(len(close), max(30, int(len(close) * fraction)))
        prefix = decision_weights(strategy, close.iloc[:cutoff], params)
        for i in range(cutoff):
            if abs(float(full.iloc[i]) - float(prefix.iloc[i])) > 1e-9:
                errors.append(
                    f"LOOKAHEAD_BIAS_DETECTED at index {i}: full={float(full.iloc[i])} "
                    f"prefix({cutoff})={float(prefix.iloc[i])}"
                )
                break
        if errors:
            break
    return (not errors), tuple(errors)


def _cap_grid(strategy: StrategyName, grid: Sequence[Mapping[str, Any]] | None, max_trials: int) -> list[dict[str, Any]]:
    match strategy:
        case "sma_crossover" | "trailing_momentum":
            default = DEFAULT_GRIDS[strategy]
        case _ as unreachable:
            _never(unreachable)
    rows = [dict(item) for item in (grid if grid is not None else default)]
    if not rows:
        raise ValueError("Parameter grid is empty")
    return rows[:max_trials]


def _score_slice(
    strategy: StrategyName,
    params: Mapping[str, Any],
    close: pd.Series,
    opened: pd.Series,
    dates: Sequence[str],
    *,
    start: int,
    end: int,
    prior_decision: float,
    init_cash: float,
    slippage: float,
    initial_equity: Decimal,
) -> tuple[Any, dict[str, object], list[float]]:
    window_decisions = decision_weights(strategy, close, params).iloc[start:end]
    entries, exits = signals_from_decisions(window_decisions, prior_decision=prior_decision)
    portfolio = simulate_long_flat(
        close.iloc[start:end],
        opened.iloc[start:end],
        entries,
        exits,
        init_cash=init_cash,
        slippage=slippage,
    )
    returns = _float_list(portfolio.returns())
    metrics = _metrics(dates[start:end], returns, initial_equity)
    metrics["trades"] = _trade_count(portfolio)
    return portfolio, metrics, returns


def _select_params(
    strategy: StrategyName,
    close_dev: pd.Series,
    open_dev: pd.Series,
    dates_dev: Sequence[str],
    grid: Sequence[Mapping[str, Any]],
    *,
    purge_days: int,
    init_cash: float,
    slippage: float,
    initial_equity: Decimal,
    budget: VectorBTBudget,
    started: float,
) -> tuple[dict[str, Any] | None, list[dict[str, object]], str | None]:
    n_dev = len(close_dev)
    train_end = max(30, int(n_dev * 0.65))
    val_start = min(n_dev - 8, train_end + purge_days)
    if val_start >= n_dev - 5:
        return None, [], "Development validation slice is too short"
    best: dict[str, Any] | None = None
    best_key: tuple[float, tuple[tuple[str, str], ...]] | None = None
    trials: list[dict[str, object]] = []
    for index, params in enumerate(grid, start=1):
        if time.monotonic() - started > budget.max_compute_seconds:
            return best, trials, "Compute time budget exhausted during parameter selection"
        merged = dict(params)
        try:
            decisions = decision_weights(strategy, close_dev, merged)
            prior = float(decisions.iloc[val_start - 1])
            _portfolio, metrics, _returns = _score_slice(
                strategy,
                merged,
                close_dev,
                open_dev,
                dates_dev,
                start=val_start,
                end=n_dev,
                prior_decision=prior,
                init_cash=init_cash,
                slippage=slippage,
                initial_equity=initial_equity,
            )
            sharpe = _sharpe(metrics)
            identity = tuple(sorted((str(key), str(value)) for key, value in merged.items()))
            rank = (sharpe, tuple((-ord(char) for char in str(identity))))
            trials.append(
                {
                    "trial": index,
                    "params": merged,
                    "status": "ok",
                    "segment": "development_validation",
                    "dev_val_sharpe": metrics["sharpe_ratio"],
                    "dev_val_return_pct": metrics["total_return_pct"],
                    "dev_val_trades": metrics["trades"],
                }
            )
            if best_key is None or rank > best_key:
                best_key = rank
                best = merged
        except Exception as exc:
            trials.append({"trial": index, "params": merged, "status": "failed", "error": str(exc)})
    if best is None:
        return None, trials, "All parameter trials failed during development selection"
    return best, trials, None


def _walk_forward(
    strategy: StrategyName,
    params: Mapping[str, Any],
    close_dev: pd.Series,
    open_dev: pd.Series,
    dates_dev: Sequence[str],
    *,
    n_folds: int,
    purge_days: int,
    init_cash: float,
    slippage: float,
    initial_equity: Decimal,
) -> list[dict[str, object]]:
    n_dev = len(close_dev)
    min_train = max(25, int(n_dev * 0.45))
    remaining = n_dev - min_train
    step = max(8, remaining // max(1, n_folds))
    decisions = decision_weights(strategy, close_dev, params)
    folds: list[dict[str, object]] = []
    for fold_idx in range(n_folds):
        train_end = min_train + fold_idx * step
        test_start = train_end + purge_days
        test_end = min(n_dev, test_start + step)
        if test_end - test_start < 6 or train_end >= n_dev:
            break
        prior = float(decisions.iloc[test_start - 1])
        _portfolio, metrics, _returns = _score_slice(
            strategy,
            params,
            close_dev,
            open_dev,
            dates_dev,
            start=test_start,
            end=test_end,
            prior_decision=prior,
            init_cash=init_cash,
            slippage=slippage,
            initial_equity=initial_equity,
        )
        folds.append(
            {
                "fold": fold_idx + 1,
                "train_start": dates_dev[0],
                "train_end": dates_dev[train_end - 1],
                "test_start": dates_dev[test_start],
                "test_end": dates_dev[test_end - 1],
                "purge_days": purge_days,
                "params": dict(params),
                "total_return_pct": metrics["total_return_pct"],
                "sharpe_ratio": metrics["sharpe_ratio"],
                "max_drawdown_pct": metrics["max_drawdown_pct"],
                "trades": metrics["trades"],
                "holdout_used": False,
            }
        )
    return folds


def _regimes_for(close: pd.Series, dates: Sequence[str]) -> dict[str, str]:
    trailing: list[float] = []
    labels: dict[str, str] = {}
    previous: float | None = None
    for day, price in zip(dates, close.tolist(), strict=True):
        labels[day] = classify_research_regime(trailing)
        if previous is not None and previous:
            trailing.append(price / previous - 1.0)
        previous = price
    return labels


def _regime_table(
    dates: Sequence[str],
    returns: Sequence[float],
    labels: Mapping[str, str],
    initial_equity: Decimal,
) -> dict[str, dict[str, object]]:
    grouped: dict[str, dict[str, Decimal]] = {}
    for day, value in zip(dates, returns, strict=True):
        grouped.setdefault(labels.get(day, "chop"), {})[day] = dec_from_float(value, day)
    table: dict[str, dict[str, object]] = {}
    for regime, series in sorted(grouped.items()):
        metrics = compute_return_metrics(series, initial_equity=initial_equity)
        table[regime] = {
            "observations": metrics["observations"],
            "total_return_pct": metrics["total_return_pct"],
            "sharpe_ratio": metrics["sharpe_ratio"],
            "max_drawdown_pct": metrics["max_drawdown_pct"],
        }
    return table


def _buy_hold_metrics(
    close: pd.Series,
    opened: pd.Series,
    dates: Sequence[str],
    *,
    init_cash: float,
    slippage: float,
    initial_equity: Decimal,
) -> dict[str, object]:
    entries = pd.Series(False, index=close.index)
    entries.iloc[0] = True
    exits = pd.Series(False, index=close.index)
    portfolio = simulate_long_flat(
        close,
        opened,
        entries,
        exits,
        init_cash=init_cash,
        slippage=slippage,
    )
    returns = _float_list(portfolio.returns())
    metrics = _metrics(dates, returns, initial_equity)
    price_returns: dict[str, Decimal] = {}
    previous: Decimal | None = None
    for day, price in zip(dates, close.tolist(), strict=True):
        decimal_price = dec_from_float(price, day)
        price_returns[day] = ZERO if previous is None else decimal_price / previous - Decimal(1)
        previous = decimal_price
    metrics["close_to_close_price_return"] = compute_return_metrics(price_returns, initial_equity=initial_equity)
    return metrics


def _deflated_sharpe(raw_sharpe: float, observations: int, trial_count: int) -> tuple[Decimal, Decimal]:
    per_year = max(1.0, observations / TRADING_DAYS_PER_YEAR)
    standard_error = math.sqrt((1.0 + 0.5 * raw_sharpe * raw_sharpe) / per_year)
    penalty = standard_error * math.sqrt(2.0 * math.log(max(1, trial_count))) if trial_count > 1 else 0.0
    return dec_from_float(penalty, "penalty"), dec_from_float(raw_sharpe - penalty, "deflated_sharpe")


def _alignment_gap(
    close: pd.Series,
    opened: pd.Series,
    entries: pd.Series,
    exits: pd.Series,
    portfolio: Any,
    *,
    init_cash: float,
    slippage: float,
) -> float:
    reference = reference_equity_path(
        close, opened, entries, exits, init_cash=init_cash, slippage=slippage
    )
    actual = _float_list(portfolio.value())
    if len(actual) != len(reference):
        return float("inf")
    return max(abs(left - right) for left, right in zip(actual, reference, strict=True))


@dataclass(frozen=True)
class VectorBTExperiment:
    experiment_id: str
    strategy_key: str
    symbol: str
    asset_class: str
    dataset_id: str
    dataset_kind: str
    status: str
    selected_params: dict[str, Any]
    trial_count: int
    trials_tested: list[dict[str, object]]
    walk_forward_folds: list[dict[str, object]]
    holdout_metrics: dict[str, object] | None
    benchmark_metrics: dict[str, object] | None
    regime_performance: dict[str, dict[str, object]]
    deflated_sharpe: str | None
    recommended_for_paper: bool
    paper_approved: bool
    rejection_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    alignment_equity_gap: float | None
    incremental_cost_usd: str
    is_synthetic: bool
    research_only: bool
    created_at: str

    def to_dict(self) -> dict[str, object]:
        return {
            "experiment_id": self.experiment_id,
            "engine": "vectorbt",
            "engine_version": VECTORBT_VERSION,
            "source_id": SOURCE_ID,
            "strategy_key": self.strategy_key,
            "symbol": self.symbol,
            "asset_class": self.asset_class,
            "dataset_id": self.dataset_id,
            "dataset_kind": self.dataset_kind,
            "status": self.status,
            "selected_params": dict(self.selected_params),
            "trial_count": self.trial_count,
            "trials_tested": list(self.trials_tested),
            "walk_forward_folds": list(self.walk_forward_folds),
            "holdout_metrics": self.holdout_metrics,
            "benchmark_metrics": self.benchmark_metrics,
            "regime_performance": self.regime_performance,
            "deflated_sharpe": self.deflated_sharpe,
            "recommended_for_paper": self.recommended_for_paper,
            "paper_approved": self.paper_approved,
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
            "alignment_equity_gap": self.alignment_equity_gap,
            "incremental_cost_usd": self.incremental_cost_usd,
            "is_synthetic": self.is_synthetic,
            "research_only": self.research_only,
            "options_execution_modeled": False,
            "created_at": self.created_at,
        }


def _failed(
    *,
    strategy: str,
    symbol: str,
    asset_class: str,
    store: FeatureStore,
    reason: str,
    started_id: str,
) -> VectorBTExperiment:
    return VectorBTExperiment(
        experiment_id=started_id,
        strategy_key=strategy,
        symbol=symbol,
        asset_class=asset_class,
        dataset_id=store.bundle.dataset_id,
        dataset_kind=store.bundle.kind,
        status="failed",
        selected_params={},
        trial_count=0,
        trials_tested=[],
        walk_forward_folds=[],
        holdout_metrics=None,
        benchmark_metrics=None,
        regime_performance={},
        deflated_sharpe=None,
        recommended_for_paper=False,
        paper_approved=False,
        rejection_reasons=(reason,),
        warnings=store.bundle.warnings,
        alignment_equity_gap=None,
        incremental_cost_usd=dec_str(estimated_incremental_monthly_usd()),
        is_synthetic=store.bundle.kind == "synthetic",
        research_only=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def run_experiment(
    store: FeatureStore,
    *,
    symbol: str,
    strategy: StrategyName,
    asset_class: AssetClass = "etf",
    bar_frequency: str = "1D",
    param_grid: Sequence[Mapping[str, Any]] | None = None,
    costs: VectorBTCosts | None = None,
    budget: VectorBTBudget | None = None,
    initial_equity: Decimal = Decimal("100000"),
    holdout_fraction: float = 0.30,
    purge_days: int = 5,
    n_walk_forward_folds: int = 3,
    min_holdout_trades: int = 30,
    registry: AlphaRegistry | None = None,
) -> VectorBTExperiment:
    """Select parameters on development data, then score the reserved holdout once."""
    assert_daily_equity_etf(asset_class, bar_frequency)
    if estimated_incremental_monthly_usd() > (budget or VectorBTBudget()).max_incremental_usd:
        raise ValueError("Estimated incremental research cost exceeds the configured budget")
    if not 0.10 <= holdout_fraction <= 0.50:
        raise ValueError("holdout_fraction must be between 0.10 and 0.50")
    if purge_days < 1:
        raise ValueError("purge_days must be >= 1")
    cost_spec = costs or VectorBTCosts()
    spend = budget or VectorBTBudget()
    experiment_id = f"vbt-{uuid4().hex[:12]}"
    symbol = symbol.upper()
    close, opened, dates = _price_frame(store, symbol)
    n_days = len(dates)
    if n_days < 80:
        return _failed(
            strategy=strategy,
            symbol=symbol,
            asset_class=asset_class,
            store=store,
            reason=f"Need at least 80 aligned sessions for a reserved holdout, got {n_days}",
            started_id=experiment_id,
        )

    started = time.monotonic()
    holdout_len = max(20, int(n_days * holdout_fraction))
    dev_end = n_days - holdout_len
    holdout_start = min(n_days - 5, dev_end + purge_days)
    if dev_end < 40 or holdout_start >= n_days - 5:
        return _failed(
            strategy=strategy,
            symbol=symbol,
            asset_class=asset_class,
            store=store,
            reason="Development or holdout split is too short",
            started_id=experiment_id,
        )

    grid = _cap_grid(strategy, param_grid, spend.max_trials)
    init_cash = float(initial_equity)
    slippage = float(cost_spec.one_way_rate)
    chosen, trials, error = _select_params(
        strategy,
        close.iloc[:dev_end],
        opened.iloc[:dev_end],
        dates[:dev_end],
        grid,
        purge_days=purge_days,
        init_cash=init_cash,
        slippage=slippage,
        initial_equity=initial_equity,
        budget=spend,
        started=started,
    )
    if chosen is None or error:
        failed = _failed(
            strategy=strategy,
            symbol=symbol,
            asset_class=asset_class,
            store=store,
            reason=error or "Parameter selection failed",
            started_id=experiment_id,
        )
        return VectorBTExperiment(
            experiment_id=failed.experiment_id,
            strategy_key=failed.strategy_key,
            symbol=failed.symbol,
            asset_class=failed.asset_class,
            dataset_id=failed.dataset_id,
            dataset_kind=failed.dataset_kind,
            status="failed",
            selected_params={},
            trial_count=len(trials),
            trials_tested=trials,
            walk_forward_folds=[],
            holdout_metrics=None,
            benchmark_metrics=None,
            regime_performance={},
            deflated_sharpe=None,
            recommended_for_paper=False,
            paper_approved=False,
            rejection_reasons=failed.rejection_reasons,
            warnings=failed.warnings,
            alignment_equity_gap=None,
            incremental_cost_usd=failed.incremental_cost_usd,
            is_synthetic=failed.is_synthetic,
            research_only=True,
            created_at=failed.created_at,
        )

    folds = _walk_forward(
        strategy,
        chosen,
        close.iloc[:dev_end],
        opened.iloc[:dev_end],
        dates[:dev_end],
        n_folds=n_walk_forward_folds,
        purge_days=purge_days,
        init_cash=init_cash,
        slippage=slippage,
        initial_equity=initial_equity,
    )
    full_decisions = decision_weights(strategy, close, chosen)
    prior = float(full_decisions.iloc[holdout_start - 1])
    hold_decisions = full_decisions.iloc[holdout_start:]
    entries, exits = signals_from_decisions(hold_decisions, prior_decision=prior)
    hold_close = close.iloc[holdout_start:]
    hold_open = opened.iloc[holdout_start:]
    portfolio = simulate_long_flat(
        hold_close, hold_open, entries, exits, init_cash=init_cash, slippage=slippage
    )
    hold_dates = dates[holdout_start:]
    hold_returns = _float_list(portfolio.returns())
    hold_metrics = _metrics(hold_dates, hold_returns, initial_equity)
    hold_metrics["trades"] = _trade_count(portfolio)
    gross = simulate_long_flat(
        hold_close, hold_open, entries, exits, init_cash=init_cash, slippage=0.0
    )
    gross_returns = _float_list(gross.returns())
    gap = _alignment_gap(
        hold_close, hold_open, entries, exits, portfolio, init_cash=init_cash, slippage=slippage
    )
    benchmark = _buy_hold_metrics(
        hold_close,
        hold_open,
        hold_dates,
        init_cash=init_cash,
        slippage=slippage,
        initial_equity=initial_equity,
    )
    labels = _regimes_for(close, dates)
    regimes = _regime_table(hold_dates, hold_returns, labels, initial_equity)
    causal_ok, causal_errors = audit_decision_causality(close, strategy, chosen)
    penalty, deflated = _deflated_sharpe(_sharpe(hold_metrics), len(hold_returns), len(trials))
    held_trades = int(hold_metrics["trades"])  # type: ignore[arg-type]

    rejections: list[str] = []
    if not causal_ok:
        rejections.append("Failed causality audit: " + "; ".join(causal_errors))
    if store.bundle.kind == "synthetic":
        rejections.append("Synthetic dataset cannot qualify for a paper-trading recommendation")
    if held_trades < min_holdout_trades:
        rejections.append(
            f"Insufficient held-out trades: {held_trades} < minimum {min_holdout_trades}"
        )
    if float(deflated) <= 0.0:
        rejections.append(
            f"Non-positive trial-discounted holdout Sharpe ({float(deflated):.3f} after {len(trials)} trials)"
        )
    if float(hold_metrics["total_return_pct"]) <= 0.0:  # type: ignore[arg-type]
        rejections.append("Non-positive net holdout return after modeled slippage")
    if float(hold_metrics["max_drawdown_pct"]) > 15.0:  # type: ignore[arg-type]
        rejections.append(f"Holdout max drawdown {hold_metrics['max_drawdown_pct']}% exceeds 15%")
    if gap > max(1e-4, 1e-8 * init_cash):
        rejections.append(f"VectorBT equity path diverged from the next-open reference (gap {gap:.6f})")

    warnings = list(store.bundle.warnings)
    warnings.append(OPTIONS_NOT_MODELED)
    warnings.append(
        "VectorBT uses floating-point fills. Reported metrics are Decimal conversions of those returns."
    )
    warnings.append(
        "Per-share commission and risk-engine position size are not applied inside vectorbt. "
        "Use validate_against_research_engine for that comparison."
    )
    warnings.append("Walk-forward folds are development robustness checks. They are not a second holdout.")
    warnings.append("This result does not approve paper trading and cannot enable live trading.")
    if not folds:
        warnings.append("No walk-forward fold fit inside the development split.")

    recommended = len(rejections) == 0
    result = VectorBTExperiment(
        experiment_id=experiment_id,
        strategy_key=strategy,
        symbol=symbol,
        asset_class=asset_class,
        dataset_id=store.bundle.dataset_id,
        dataset_kind=store.bundle.kind,
        status="passed" if recommended else "rejected",
        selected_params=dict(chosen),
        trial_count=len(trials),
        trials_tested=trials,
        walk_forward_folds=folds,
        holdout_metrics=hold_metrics,
        benchmark_metrics=benchmark,
        regime_performance=regimes,
        deflated_sharpe=dec_str(deflated),
        recommended_for_paper=recommended,
        paper_approved=False,
        rejection_reasons=tuple(rejections),
        warnings=tuple(warnings),
        alignment_equity_gap=gap,
        incremental_cost_usd=dec_str(estimated_incremental_monthly_usd()),
        is_synthetic=store.bundle.kind == "synthetic",
        research_only=True,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    if registry is not None:
        _publish(registry, result, store, hold_dates, hold_returns, gross_returns, labels, entries, exits, penalty)
    return result


def _publish(
    registry: AlphaRegistry,
    result: VectorBTExperiment,
    store: FeatureStore,
    hold_dates: Sequence[str],
    hold_returns: Sequence[float],
    gross_returns: Sequence[float],
    labels: Mapping[str, str],
    entries: pd.Series,
    exits: pd.Series,
    penalty: Decimal,
) -> None:
    definition = registry.register(
        alpha_id=f"vbt_{result.strategy_key}_{result.symbol}",
        name=f"VectorBT {result.strategy_key} {result.symbol}",
        strategy_key=result.strategy_key,
        source_id=SOURCE_ID,
        portfolio="swing",
        parameters=result.selected_params,
        deployment_status="candidate" if result.recommended_for_paper else "rejected",
        research_only=True,
        allow_inversion=False,
        metadata={
            "experiment_id": result.experiment_id,
            "engine": "vectorbt",
            "engine_version": VECTORBT_VERSION,
            "dataset_id": store.bundle.dataset_id,
            "dataset_kind": store.bundle.kind,
            "segment": "holdout",
            "options_execution_modeled": False,
            "paper_approved": False,
            "multiple_testing_penalty": dec_str(penalty),
        },
    )
    entry_flags = [bool(value) for value in entries.tolist()]
    exit_flags = [bool(value) for value in exits.tolist()]
    rows = []
    for index, day in enumerate(hold_dates):
        turnover = Decimal("1") if entry_flags[index] or exit_flags[index] else ZERO
        rows.append(
            DailyAlphaReturn(
                alpha_id=definition.alpha_id,
                version=definition.version,
                trade_date=day,
                gross_return=dec_from_float(gross_returns[index], day),
                net_return=dec_from_float(hold_returns[index], day),
                turnover=turnover,
                regime=labels.get(day, "chop"),
                is_synthetic=result.is_synthetic,
            )
        )
    registry.record_daily_returns(definition.alpha_id, definition.version, rows)
    registry.record_approval(
        alpha_id=definition.alpha_id,
        version=definition.version,
        validation_status="recommended_for_paper" if result.recommended_for_paper else "rejected",
        paper_approved=False,
        approved_by=None,
        held_out_trades=int(result.holdout_metrics["trades"]) if result.holdout_metrics else 0,  # type: ignore[index]
        trial_count=result.trial_count,
        rejection_reasons=list(result.rejection_reasons),
        reason="VectorBT research result. Human review is required before any paper approval.",
    )


def compare_strategies(
    store: FeatureStore,
    *,
    symbol: str,
    strategies: Sequence[StrategyName] = ("sma_crossover", "trailing_momentum"),
    asset_class: AssetClass = "etf",
    costs: VectorBTCosts | None = None,
    budget: VectorBTBudget | None = None,
    initial_equity: Decimal = Decimal("100000"),
    purge_days: int = 5,
) -> dict[str, object]:
    """Rank strategies on the development validation slice. The holdout is not read."""
    assert_daily_equity_etf(asset_class, "1D")
    spend = budget or VectorBTBudget()
    cost_spec = costs or VectorBTCosts()
    close, opened, dates = _price_frame(store, symbol.upper())
    n_days = len(dates)
    dev_end = n_days - max(20, int(n_days * 0.30))
    if dev_end < 40:
        raise ValueError("Not enough development sessions to compare strategies")
    rows: list[dict[str, object]] = []
    started = time.monotonic()
    for strategy in strategies:
        grid = _cap_grid(strategy, None, spend.max_trials)
        chosen, trials, error = _select_params(
            strategy,
            close.iloc[:dev_end],
            opened.iloc[:dev_end],
            dates[:dev_end],
            grid,
            purge_days=purge_days,
            init_cash=float(initial_equity),
            slippage=float(cost_spec.one_way_rate),
            initial_equity=initial_equity,
            budget=spend,
            started=started,
        )
        sharpe = None
        if chosen is not None:
            for trial in trials:
                if trial.get("status") == "ok" and trial.get("params") == chosen:
                    sharpe = trial.get("dev_val_sharpe")
        rows.append(
            {
                "strategy_key": strategy,
                "selected_params": chosen,
                "dev_val_sharpe": sharpe,
                "trial_count": len(trials),
                "error": error,
                "holdout_used": False,
            }
        )
    rows.sort(key=lambda row: float(row["dev_val_sharpe"] or "-999"), reverse=True)
    return {
        "symbol": symbol.upper(),
        "ranked_on": "development_validation_only",
        "holdout_used": False,
        "is_synthetic": store.bundle.kind == "synthetic",
        "rows": rows,
        "note": "This ranking is not evidence. The reserved holdout was not used.",
    }


def validate_against_research_engine(
    store: FeatureStore,
    *,
    symbol: str,
    strategy: StrategyName,
    params: Mapping[str, Any],
    asset_class: AssetClass = "etf",
    costs: VectorBTCosts | None = None,
    initial_equity: Decimal = Decimal("100000"),
) -> dict[str, object]:
    """Compare vectorbt's next-open path with the risk-engine simulator on the same decisions.

    Dollar results are expected to differ: vectorbt is fully invested and charges percent
    slippage only. The research engine sizes through the independent risk engine, adds
    per-share commission, and can stop out. Signal direction is the shared contract.
    """
    assert_daily_equity_etf(asset_class, "1D")
    cost_spec = costs or VectorBTCosts()
    symbol = symbol.upper()
    one = FeatureStore.from_bars(
        {symbol: store.bars(symbol)},
        kind=store.bundle.kind,
        source=store.bundle.source,
    )
    close, opened, dates = _price_frame(one, symbol)
    decisions = decision_weights(strategy, close, params)
    entries, exits = signals_from_decisions(decisions, prior_decision=0.0)
    init_cash = float(initial_equity)
    slippage = float(cost_spec.one_way_rate)
    portfolio = simulate_long_flat(
        close, opened, entries, exits, init_cash=init_cash, slippage=slippage
    )
    gap = _alignment_gap(
        close, opened, entries, exits, portfolio, init_cash=init_cash, slippage=slippage
    )
    assets = _float_list(portfolio.assets())
    mismatches = 0
    for index, shares in enumerate(assets):
        prior = 0.0 if index == 0 else float(decisions.iloc[index - 1])
        if (shares > 1e-8) != (prior > 0.0):
            mismatches += 1
    signals = {day: {symbol: float(weight)} for day, weight in zip(dates, decisions.tolist(), strict=True)}
    research = simulate_signal_frame(
        one,
        signals,
        cost_model=cost_spec.research_model(),
        initial_equity=initial_equity,
    )
    unauthorized: list[str] = []
    for trade in research.trades:
        if trade["direction"] != "long":
            unauthorized.append(str(trade["entry_day"]))
            continue
        entry_day = str(trade["entry_day"])
        position = dates.index(entry_day)
        prior = 0.0 if position == 0 else float(decisions.iloc[position - 1])
        if prior <= 0.0:
            unauthorized.append(entry_day)
    vectorbt_metrics = _metrics(dates, _float_list(portfolio.returns()), initial_equity)
    return {
        "alignment_equity_gap": gap,
        "position_mismatches": mismatches,
        "research_entries_follow_signal": unauthorized == [],
        "unauthorized_entry_days": unauthorized,
        "research_engine_trades": research.metrics["trades"],
        "research_engine_total_return_pct": research.metrics["total_return_pct"],
        "vectorbt_total_return_pct": vectorbt_metrics["total_return_pct"],
        "vectorbt_trades": _trade_count(portfolio),
        "options_execution_modeled": False,
        "differences": [
            "VectorBT is 100% long or flat and applies percent slippage inside the fill.",
            "The research engine sizes with the independent risk engine, charges per-share commission, and can exit at the protective stop.",
            "Dollar P&L is not required to match. The shared check is next-open signal direction.",
            OPTIONS_NOT_MODELED,
        ],
    }
