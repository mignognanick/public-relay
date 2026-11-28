"""Causality auditing, risk-engine-backed simulation, and walk-forward/holdout validation."""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Mapping, Sequence

from quant_engine.dashboard.domain import Tax, tax_plan
from quant_engine.risk.config import RiskConfig
from quant_engine.risk.engine import RiskEngine
from quant_engine.risk.models import AccountSnapshot, EntryRequest, ExitRequest, Side
from quant_engine.risk.sizing import size_position
from quant_engine.risk.state import InMemoryStateStore
from research.feature_store import FeatureStore
from research.library import (
    HUNDRED,
    ONE,
    TRADING_DAYS_PER_YEAR,
    ZERO,
    classify_research_regime,
    compute_return_metrics,
    dec_from_float,
    dec_str,
    q2,
    sample_mean,
    sample_std,
)
from research.strategy_catalog import ResearchStrategy, SignalFrame


@dataclass(frozen=True)
class ExecutionCostModel:
    """Modeled US equity/ETF transaction costs (all in Decimal)."""

    spread_bps: Decimal = Decimal("2.0")
    slippage_bps: Decimal = Decimal("2.0")
    commission_per_share: Decimal = Decimal("0.005")
    short_borrow_bps_annual: Decimal = Decimal("100.0")
    stop_pct: Decimal = Decimal("2.0")

    @property
    def one_way_rate(self) -> Decimal:
        return (self.spread_bps / Decimal("2") + self.slippage_bps) / Decimal("10000")

    def to_dict(self) -> dict[str, str]:
        return {
            "spread_bps": str(self.spread_bps),
            "slippage_bps": str(self.slippage_bps),
            "commission_per_share": str(self.commission_per_share),
            "short_borrow_bps_annual": str(self.short_borrow_bps_annual),
            "stop_pct": str(self.stop_pct),
        }


@dataclass(frozen=True)
class SimulationResult:
    metrics: dict[str, object]
    daily_returns: dict[str, Decimal]
    daily_gross_returns: dict[str, Decimal]
    daily_turnovers: dict[str, Decimal]
    regime_by_date: dict[str, str]
    regime_performance: dict[str, dict[str, object]]
    trades: list[dict[str, object]]
    rejected_entries: dict[str, int]
    risk_status: dict[str, object]
    tax_impact: dict[str, object]
    benchmark_returns: dict[str, Decimal]

    def to_dict(self) -> dict[str, object]:
        return {
            "metrics": self.metrics,
            "daily_returns": {d: dec_str(v) for d, v in self.daily_returns.items()},
            "regime_performance": self.regime_performance,
            "trades_count": len(self.trades),
            "rejected_entries": dict(self.rejected_entries),
            "risk_status": self.risk_status,
            "tax_impact": self.tax_impact,
        }


@dataclass(frozen=True)
class ValidationVerdict:
    strategy_key: str
    causality_passed: bool
    causality_errors: tuple[str, ...]
    walk_forward_folds: list[dict[str, object]]
    walk_forward_mean_sharpe: Decimal
    holdout_result: SimulationResult
    benchmark_metrics: dict[str, object]
    trial_count: int
    multiple_testing_penalty: Decimal
    deflated_sharpe: Decimal
    held_out_trades: int
    recommended_for_paper: bool
    paper_approved: bool  # Always False; human approval required via dashboard
    rejection_reasons: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "strategy_key": self.strategy_key,
            "causality_passed": self.causality_passed,
            "causality_errors": list(self.causality_errors),
            "walk_forward_folds": self.walk_forward_folds,
            "walk_forward_mean_sharpe": dec_str(self.walk_forward_mean_sharpe),
            "holdout": self.holdout_result.to_dict(),
            "benchmark_metrics": self.benchmark_metrics,
            "trial_count": self.trial_count,
            "multiple_testing_penalty": dec_str(self.multiple_testing_penalty),
            "deflated_sharpe": dec_str(self.deflated_sharpe),
            "held_out_trades": self.held_out_trades,
            "recommended_for_paper": self.recommended_for_paper,
            "paper_approved": self.paper_approved,
            "rejection_reasons": list(self.rejection_reasons),
            "warnings": list(self.warnings),
        }


def _compute_benchmark_and_regimes(store: FeatureStore) -> tuple[dict[str, Decimal], dict[str, str]]:
    """Equal-weight universe buy-and-hold daily returns and causal realized regime tags."""
    dates = store.dates
    bars_by_sym = {s: store.bars(s) for s in store.symbols}
    bench: dict[str, Decimal] = {}
    regimes: dict[str, str] = {}
    trailing: list[float] = []

    for i, d in enumerate(dates):
        if i == 0:
            bench[d] = ZERO
            regimes[d] = "chop"
            continue
        rets = [
            bars_by_sym[s][i].close / bars_by_sym[s][i - 1].close - ONE
            for s in store.symbols
        ]
        avg_r = sum(rets, ZERO) / Decimal(len(rets))
        bench[d] = avg_r
        # Strictly causal regime label using returns realized through i-1 (before day i decision/open)
        regimes[d] = classify_research_regime(trailing)
        trailing.append(float(avg_r))
    return bench, regimes


def simulate_signal_frame(
    store: FeatureStore,
    signals: SignalFrame,
    *,
    cost_model: ExecutionCostModel | None = None,
    initial_equity: Decimal = Decimal("100000"),
    risk_config: RiskConfig | None = None,
    tax_profile: Tax | None = None,
) -> SimulationResult:
    """Causal next-open portfolio simulation enforcing Quant Engine RiskEngine limits.

    Signal decided at close of day t-1 executes at open of day t.
    All entries pass RiskEngine.evaluate_entry + size_position; all protective exits
    pass RiskEngine.evaluate_exit and are never blocked by entry lockouts.
    """
    costs = cost_model or ExecutionCostModel()
    cfg = risk_config or RiskConfig()
    dates = store.dates
    symbols = store.symbols
    bars_by_sym = {s: {b.day: b for b in store.bars(s)} for s in symbols}
    bench_rets, regime_by_date = _compute_benchmark_and_regimes(store)

    if len(dates) < 2:
        empty_metrics = compute_return_metrics({}, initial_equity=initial_equity)
        return SimulationResult(
            metrics=empty_metrics,
            daily_returns={},
            daily_gross_returns={},
            daily_turnovers={},
            regime_by_date=regime_by_date,
            regime_performance={},
            trades=[],
            rejected_entries={},
            risk_status={},
            tax_impact={},
            benchmark_returns=bench_rets,
        )

    first_ts = bars_by_sym[symbols[0]][dates[0]].timestamp
    clock = [first_ts]
    risk = RiskEngine(cfg, InMemoryStateStore(), clock=lambda: clock[0])

    cash = initial_equity
    positions: dict[str, dict[str, Any]] = {}  # sym -> {id, side, qty, entry_price, entry_day, entry_idx, entry_fee}
    daily_returns: dict[str, Decimal] = {}
    daily_gross: dict[str, Decimal] = {}
    daily_turnovers: dict[str, Decimal] = {}
    trades: list[dict[str, object]] = []
    trade_pnls: list[Decimal] = []
    holding_days_list: list[int] = []
    rejects: Counter[str] = Counter()
    total_costs = ZERO

    week_key: tuple[int, int] | None = None
    week_start_equity = initial_equity
    prev_equity = initial_equity
    cost_rate = costs.one_way_rate
    borrow_daily_rate = costs.short_borrow_bps_annual / Decimal("10000") / Decimal(TRADING_DAYS_PER_YEAR)

    for i, d in enumerate(dates):
        day_ts = bars_by_sym[symbols[0]][d].timestamp
        clock[0] = day_ts

        # Mark-to-market equity at open before rebalancing
        def mark_equity(use_open: bool) -> Decimal:
            eq = cash
            for sym, pos in positions.items():
                px = bars_by_sym[sym][d].open if use_open else bars_by_sym[sym][d].close
                sign = ONE if pos["side"] == Side.LONG else -ONE
                eq += sign * (px - pos["entry_price"]) * Decimal(pos["qty"])
            return eq

        day_start_equity = mark_equity(use_open=True)
        iso_week = day_ts.isocalendar()[:2]
        if iso_week != week_key:
            week_key = iso_week
            week_start_equity = day_start_equity

        # Target weights decided strictly at previous close (no same-day look-ahead!)
        target_weights = signals.get(dates[i - 1], {}) if i > 0 else {}
        is_final_day = i == len(dates) - 1
        if is_final_day:
            target_weights = {s: 0.0 for s in symbols}

        day_turnover_notional = ZERO
        day_cost_dollars = ZERO
        day_gross_pnl_before_costs = ZERO

        # 1. Process exits & protective stops FIRST (never blocked by entry restrictions)
        for sym in list(positions.keys()):
            pos = positions[sym]
            bar = bars_by_sym[sym][d]
            target_w = float(target_weights.get(sym, 0.0))
            pos_sign = 1 if pos["side"] == Side.LONG else -1
            want_sign = 1 if target_w > 1e-6 else (-1 if target_w < -1e-6 else 0)

            stop_hit = (
                (pos_sign == 1 and bar.open <= pos["stop"])
                or (pos_sign == -1 and bar.open >= pos["stop"])
            )
            must_close = is_final_day or stop_hit or want_sign != pos_sign

            if must_close:
                raw_exit = bar.open
                exit_px = raw_exit * (ONE - Decimal(pos_sign) * cost_rate)
                reason = "final_flatten" if is_final_day else ("stop_exit" if stop_hit else "signal_exit")
                approval = risk.evaluate_exit(ExitRequest(pos["id"], pos["qty"], reason))
                if not approval.approved:
                    raise RuntimeError(f"Protective exit unexpectedly blocked for {pos['id']}: {approval.reasons}")

                gross_pnl = Decimal(pos_sign) * (raw_exit - pos["entry_price"]) * Decimal(pos["qty"])
                slip_cost = abs(raw_exit - exit_px) * Decimal(pos["qty"])
                exit_comm = costs.commission_per_share * Decimal(pos["qty"])
                hold_days = max(1, i - pos["entry_idx"])
                borrow_cost = (
                    pos["entry_price"] * Decimal(pos["qty"]) * borrow_daily_rate * Decimal(hold_days)
                    if pos_sign < 0
                    else ZERO
                )
                net_pnl = Decimal(pos_sign) * (exit_px - pos["entry_price"]) * Decimal(pos["qty"]) - pos["entry_fee"] - exit_comm - borrow_cost
                cash += Decimal(pos_sign) * (exit_px - pos["entry_price"]) * Decimal(pos["qty"]) - exit_comm - borrow_cost

                day_cost_dollars += slip_cost + exit_comm + borrow_cost
                total_costs += slip_cost + exit_comm + borrow_cost
                day_turnover_notional += raw_exit * Decimal(pos["qty"])
                risk.record_exit_fill(pos["id"], pos["qty"])

                trade_pnls.append(net_pnl)
                holding_days_list.append(hold_days)
                trades.append(
                    {
                        "id": pos["id"],
                        "symbol": sym,
                        "direction": "long" if pos_sign == 1 else "short",
                        "qty": pos["qty"],
                        "entry_day": pos["entry_day"],
                        "exit_day": d,
                        "hold_days": hold_days,
                        "entry_price": dec_str(pos["entry_price"]),
                        "exit_price": dec_str(exit_px),
                        "net_pnl": dec_str(net_pnl),
                        "gross_pnl": dec_str(gross_pnl),
                        "reason": reason,
                    }
                )
                del positions[sym]

        # 2. Process new entries through RiskEngine + size_position
        if not is_final_day:
            open_eq = mark_equity(use_open=True)
            if open_eq > ZERO:
                snapshot = AccountSnapshot(
                    equity=open_eq,
                    as_of=day_ts,
                    day_start_equity=day_start_equity,
                    week_start_equity=week_start_equity,
                )
                for sym in symbols:
                    if sym in positions:
                        continue
                    target_w = float(target_weights.get(sym, 0.0))
                    if abs(target_w) <= 1e-6:
                        continue
                    side = Side.LONG if target_w > 0 else Side.SHORT
                    sign = ONE if side == Side.LONG else -ONE
                    bar = bars_by_sym[sym][d]
                    raw_open = bar.open
                    entry_px = raw_open * (ONE + sign * cost_rate)
                    stop_dist = entry_px * costs.stop_pct / HUNDRED
                    stop_px = entry_px - sign * stop_dist
                    if stop_px <= ZERO:
                        rejects["INVALID_STOP"] += 1
                        continue
                    borrow_one_day = (entry_px * borrow_daily_rate) if side == Side.SHORT else ZERO
                    cost_per_unit = stop_px * cost_rate + Decimal("2") * costs.commission_per_share + borrow_one_day

                    # Desired shares capped by target weight notional AND RiskEngine max_trade_risk_pct
                    desired_notional = open_eq * dec_from_float(abs(target_w), "target_w")
                    weight_qty = int(desired_notional // entry_px)
                    if weight_qty <= 0:
                        continue
                    sized = size_position(
                        open_eq,
                        cfg.max_trade_risk_pct,
                        side,
                        entry_px,
                        stop_px,
                        1,
                        cost_per_unit=cost_per_unit,
                        requested_qty=weight_qty,
                    )
                    if sized.qty <= 0:
                        rejects["ZERO_SIZE_WITHIN_RISK_BUDGET"] += 1
                        continue
                    pos_id = f"{d}-{sym}"
                    decision = risk.evaluate_entry(
                        EntryRequest(
                            request_id=pos_id,
                            strategy=sym,
                            symbol=sym,
                            side=side,
                            entry_price=entry_px,
                            stop_price=stop_px,
                            requested_qty=sized.qty,
                            cost_per_unit=cost_per_unit,
                        ),
                        snapshot,
                    )
                    if decision.approved and decision.qty > 0:
                        risk.confirm_entry_fill(pos_id, decision.qty, entry_px)
                        entry_comm = costs.commission_per_share * Decimal(decision.qty)
                        slip_entry = abs(entry_px - raw_open) * Decimal(decision.qty)
                        cash -= entry_comm
                        day_cost_dollars += entry_comm + slip_entry
                        total_costs += entry_comm + slip_entry
                        day_turnover_notional += raw_open * Decimal(decision.qty)
                        positions[sym] = {
                            "id": pos_id,
                            "side": side,
                            "qty": decision.qty,
                            "entry_price": entry_px,
                            "stop": stop_px,
                            "entry_day": d,
                            "entry_idx": i,
                            "entry_fee": entry_comm,
                        }
                    else:
                        rejects.update(str(r) for r in decision.reasons)

        close_equity = mark_equity(use_open=False)
        day_net_ret = ((close_equity - prev_equity) / prev_equity) if prev_equity > ZERO else ZERO
        day_gross_ret = (
            ((close_equity - prev_equity + day_cost_dollars) / prev_equity) if prev_equity > ZERO else ZERO
        )
        day_turn = (day_turnover_notional / prev_equity) if prev_equity > ZERO else ZERO
        daily_returns[d] = day_net_ret
        daily_gross[d] = day_gross_ret
        daily_turnovers[d] = day_turn
        prev_equity = close_equity
        _ = day_gross_pnl_before_costs

    avg_hold = (sum(holding_days_list) / len(holding_days_list)) if holding_days_list else 5.0
    metrics = compute_return_metrics(
        daily_returns,
        initial_equity=initial_equity,
        trade_pnls=trade_pnls,
        turnovers=list(daily_turnovers.values()),
        total_costs=total_costs,
        holding_days_avg=avg_hold,
    )

    # Regime breakdown across the 6 Quant Engine regimes
    regime_perf: dict[str, dict[str, object]] = {}
    by_reg: dict[str, dict[str, Decimal]] = {}
    for d, r in daily_returns.items():
        by_reg.setdefault(regime_by_date.get(d, "chop"), {})[d] = r
    for reg, series in sorted(by_reg.items()):
        rm = compute_return_metrics(series, initial_equity=initial_equity)
        regime_perf[reg] = {
            "observations": rm["observations"],
            "total_return_pct": rm["total_return_pct"],
            "sharpe_ratio": rm["sharpe_ratio"],
            "max_drawdown_pct": rm["max_drawdown_pct"],
        }

    # Tax Center integration: split realized P&L into short-term (<365d) vs long-term (>=365d)
    st_realized = q2(sum((Decimal(t["net_pnl"]) for t in trades if int(t["hold_days"]) < 365), ZERO))  # type: ignore[arg-type]
    lt_realized = q2(sum((Decimal(t["net_pnl"]) for t in trades if int(t["hold_days"]) >= 365), ZERO))  # type: ignore[arg-type]
    base_tax = tax_profile or Tax()
    scenario_tax = Tax(
        carryforward=base_tax.carryforward,
        short_term_net=q2(base_tax.short_term_net + st_realized),
        long_term_net=q2(base_tax.long_term_net + lt_realized),
        filing=base_tax.filing,
        sufficient_income=base_tax.sufficient_income,
    )
    tax_impact = {
        "short_term_realized": str(st_realized),
        "long_term_realized": str(lt_realized),
        "avg_holding_days": round(avg_hold, 2),
        "tax_center_projection": tax_plan(scenario_tax),
        "note": "Illustrative federal carryforward netting only; no wash-sale or state-tax modeling.",
    }

    return SimulationResult(
        metrics=metrics,
        daily_returns=daily_returns,
        daily_gross_returns=daily_gross,
        daily_turnovers=daily_turnovers,
        regime_by_date=regime_by_date,
        regime_performance=regime_perf,
        trades=trades,
        rejected_entries=dict(rejects),
        risk_status=risk.status(),
        tax_impact=tax_impact,
        benchmark_returns=bench_rets,
    )


def audit_causality(
    strategy: ResearchStrategy,
    store: FeatureStore,
    params: Mapping[str, Any],
    *,
    train_fraction: float = 0.5,
) -> tuple[bool, tuple[str, ...]]:
    """Automated look-ahead / data-leakage audit via prefix invariance.

    Fits the strategy strictly on the training prefix, then compares generated signals
    on the full store vs truncated prefixes at multiple checkpoints. Any divergence
    proves future bars leaked into earlier signals.
    """
    n_days = len(store.dates)
    if n_days < 30:
        return False, ("Insufficient sessions (<30) to run causality audit",)
    train_count = max(15, int(n_days * train_fraction))
    train_store = store.slice_prefix(train_count)
    fitted = strategy.fit(train_store, params)
    full_signals = strategy.generate_signals(store, params, fitted)

    checkpoints = sorted(
        {
            min(n_days - 1, max(train_count + 1, int(n_days * frac)))
            for frac in (0.65, 0.80, 0.92)
        }
    )
    errors: list[str] = []
    for cp in checkpoints:
        prefix_store = store.slice_prefix(cp)
        prefix_signals = strategy.generate_signals(prefix_store, params, fitted)
        for day in prefix_store.dates:
            s_full = full_signals.get(day, {})
            s_pref = prefix_signals.get(day, {})
            if set(s_full.keys()) != set(s_pref.keys()):
                errors.append(f"Symbol key mismatch on {day} at checkpoint {cp}")
                break
            for sym, val_full in s_full.items():
                if abs(float(val_full) - float(s_pref[sym])) > 1e-9:
                    errors.append(
                        f"LOOKAHEAD_BIAS_DETECTED on {day} ({sym}): full={val_full:.6f} vs prefix({cp})={s_pref[sym]:.6f}"
                    )
                    break
            if errors:
                break
        if errors:
            break

    return (len(errors) == 0), tuple(errors)


def walk_forward_and_holdout_validate(
    strategy: ResearchStrategy,
    store: FeatureStore,
    params: Mapping[str, Any],
    *,
    trial_count: int = 1,
    n_walk_forward_folds: int = 3,
    purge_days: int = 5,
    holdout_fraction: float = 0.30,
    cost_model: ExecutionCostModel | None = None,
    initial_equity: Decimal = Decimal("100000"),
    min_holdout_trades: int = 30,
    max_allowed_corr: float = 0.85,
    max_observed_corr: float | None = None,
    tax_profile: Tax | None = None,
) -> ValidationVerdict:
    """Run causality audit, purged walk-forward CV, and independent reserved holdout test."""
    if trial_count < 1:
        raise ValueError("trial_count must be >= 1")
    dates = store.dates
    n_days = len(dates)
    if n_days < 60:
        raise ValueError(f"Validation requires at least 60 trading sessions, got {n_days}")

    causality_ok, causality_errors = audit_causality(strategy, store, params)

    # Reserve independent holdout period strictly at the tail
    holdout_len = max(20, int(n_days * holdout_fraction))
    dev_end_idx = n_days - holdout_len
    if dev_end_idx < 35:
        raise ValueError("Development split too short for walk-forward validation")

    dev_dates = dates[:dev_end_idx]
    holdout_start_idx = min(n_days - 10, dev_end_idx + purge_days)

    # Purged expanding-window walk-forward folds inside development split
    wf_folds: list[dict[str, object]] = []
    wf_sharpes: list[float] = []
    min_train = max(25, int(len(dev_dates) * 0.45))
    rem_dev = len(dev_dates) - min_train
    fold_step = max(8, rem_dev // max(1, n_walk_forward_folds))

    for fold_idx in range(n_walk_forward_folds):
        tr_end = min_train + fold_idx * fold_step
        te_start = tr_end + purge_days
        te_end = min(len(dev_dates), te_start + fold_step)
        if te_end - te_start < 6 or tr_end >= len(dev_dates):
            break
        train_sub = store.slice_by_dates(dev_dates[0], dev_dates[tr_end - 1])
        # Provide lookback context into test slice but score strictly on [te_start : te_end]
        context_start = max(0, te_start - 85)
        eval_sub = store.slice_by_dates(dev_dates[context_start], dev_dates[te_end - 1])
        fitted = strategy.fit(train_sub, params)
        sigs = strategy.generate_signals(eval_sub, params, fitted)
        # Filter signals/store strictly to out-of-sample fold dates for scoring
        oos_store = store.slice_by_dates(dev_dates[te_start], dev_dates[te_end - 1])
        oos_sigs = {d: sigs[d] for d in oos_store.dates if d in sigs}
        fold_sim = simulate_signal_frame(
            oos_store,
            oos_sigs,
            cost_model=cost_model,
            initial_equity=initial_equity,
            tax_profile=tax_profile,
        )
        fold_sharpe = float(fold_sim.metrics["sharpe_ratio"])
        wf_sharpes.append(fold_sharpe)
        wf_folds.append(
            {
                "fold": fold_idx + 1,
                "train_start": dev_dates[0],
                "train_end": dev_dates[tr_end - 1],
                "test_start": dev_dates[te_start],
                "test_end": dev_dates[te_end - 1],
                "purge_days": purge_days,
                "total_return_pct": fold_sim.metrics["total_return_pct"],
                "sharpe_ratio": fold_sim.metrics["sharpe_ratio"],
                "max_drawdown_pct": fold_sim.metrics["max_drawdown_pct"],
                "trades": fold_sim.metrics["trades"],
            }
        )

    wf_mean_sharpe = dec_from_float(sample_mean(wf_sharpes), "wf_sharpe")

    # Independent holdout: fit strictly on development split, evaluate strictly on holdout
    dev_store = store.slice_by_dates(dates[0], dates[dev_end_idx - 1])
    fitted_dev = strategy.fit(dev_store, params)
    # Generate causal signals over full store with dev-only fitted parameters, then score holdout dates
    full_sigs = strategy.generate_signals(store, params, fitted_dev)
    holdout_store = store.slice_by_dates(dates[holdout_start_idx], dates[-1])
    holdout_sigs = {d: full_sigs[d] for d in holdout_store.dates if d in full_sigs}
    holdout_sim = simulate_signal_frame(
        holdout_store,
        holdout_sigs,
        cost_model=cost_model,
        initial_equity=initial_equity,
        tax_profile=tax_profile,
    )
    bench_metrics = compute_return_metrics(holdout_sim.benchmark_returns, initial_equity=initial_equity)

    # Multiple-testing discount (Research Rule 4): approximate expected max noise Sharpe ~ sqrt(2 ln N_trials)
    raw_holdout_sharpe = float(holdout_sim.metrics["sharpe_ratio"])
    n_obs = max(2, len(holdout_sim.daily_returns))
    se_sharpe = math.sqrt((1.0 + 0.5 * raw_holdout_sharpe * raw_holdout_sharpe) / max(1.0, n_obs / TRADING_DAYS_PER_YEAR))
    penalty = se_sharpe * math.sqrt(2.0 * math.log(max(1, trial_count))) if trial_count > 1 else 0.0
    deflated = raw_holdout_sharpe - penalty

    held_out_trades = int(holdout_sim.metrics["trades"])
    rejections: list[str] = []
    warnings: list[str] = list(store.bundle.warnings)

    if not causality_ok:
        rejections.append("Failed causality / look-ahead audit: " + "; ".join(causality_errors))
    if store.bundle.kind == "synthetic":
        rejections.append("Synthetic dataset cannot qualify for paper-trading recommendation (historical data required)")
    if held_out_trades < min_holdout_trades:
        rejections.append(
            f"Insufficient held-out trades: {held_out_trades} < minimum {min_holdout_trades} (AGENTS.md Research Rule 5)"
        )
    if deflated <= 0.0:
        rejections.append(
            f"Negative or non-positive trial-discounted holdout Sharpe ({deflated:.3f} after {trial_count} trials)"
        )
    if float(holdout_sim.metrics["total_return_pct"]) <= 0.0:
        rejections.append("Non-positive net holdout return after modeled transaction costs")
    if float(holdout_sim.metrics["max_drawdown_pct"]) > 15.0:
        rejections.append(f"Holdout max drawdown {holdout_sim.metrics['max_drawdown_pct']}% exceeds 15% research cap")
    if max_observed_corr is not None and abs(max_observed_corr) > max_allowed_corr:
        rejections.append(
            f"Excessive correlation ({max_observed_corr:.2f}) with existing approved alpha (limit {max_allowed_corr:.2f})"
        )

    warnings.append(
        "Validation never auto-promotes or enables paper or live trading. Human approval is required in the dashboard."
    )

    return ValidationVerdict(
        strategy_key=strategy.metadata.strategy_key,
        causality_passed=causality_ok,
        causality_errors=causality_errors,
        walk_forward_folds=wf_folds,
        walk_forward_mean_sharpe=wf_mean_sharpe,
        holdout_result=holdout_sim,
        benchmark_metrics=bench_metrics,
        trial_count=trial_count,
        multiple_testing_penalty=dec_from_float(penalty, "penalty"),
        deflated_sharpe=dec_from_float(deflated, "deflated_sharpe"),
        held_out_trades=held_out_trades,
        recommended_for_paper=(len(rejections) == 0),
        paper_approved=False,
        rejection_reasons=tuple(rejections),
        warnings=tuple(warnings),
    )
