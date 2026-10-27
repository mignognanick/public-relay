"""Relay-faithful 0DTE backtest of the signal grid in `strategies.py`.

The simulator tracks two positions. The TradingView position follows the strategy's targets and is
what the Pine port does: it closes on a reversal and enters the other side on the next bar, stops new
entries at the relay's PAPER cutoff, takes at most three entries a day and flattens at the 15:40 bar.
The relay position follows the alerts the way `api/index.py` handles them. An open picks a 0DTE leg
with the same ranking as `choose_leg` and a limit at the mark. A close or opposite alert only flattens.
A resting take-profit (TP_SINGLE, qty 1) or a stop-limit can close the leg early, and the relay then
stays flat until the next TradingView entry. Contract marks come from `pricing.py`.

Run `python research/backtest.py` to write research/results/*.json.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, time
from pathlib import Path

import pricing
import strategies
from strategies import Bar, Target, Variant

RESULTS = Path(__file__).resolve().parent / "results"
SYMBOLS = ("SPY", "QQQ", "IWM", "XSP", "SPX")
SIGNAL_SOURCE = {"SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "XSP": "SPY", "SPX": "SPY"}
WARMUP = set(("2026-09-24", "2026-09-25"))
IN_SAMPLE_END = "2026-10-16"
FLATTEN_AT = time(15, 40)
MAX_ENTRIES = 3
STRIKE_STEP = {"SPX": 5.0}
POINT_SCALE = {"SPX": 10.0}
COST = {"SPY": 0.02, "QQQ": 0.03, "IWM": 0.02, "XSP": 0.05, "SPX": 0.30}
COST_PCT = 0.01
MULTIPLIER = 100
PREMIUM_CAPS = (3.0, 5.0, 8.0, None)
MIN_TRADES_IN_SAMPLE = 30
MIN_PROFIT_FACTOR_OUT = 1.2


def cutoff(symbol: str) -> time:
    return time(15, 30) if symbol in ("SPY", "QQQ") else time(15, 15)


STRESS = {"tp_on_extreme": True, "cost_multiplier": 1.0}


def cost(symbol: str, premium: float) -> float:
    return STRESS["cost_multiplier"] * max(COST[symbol], COST_PCT * premium)


@dataclass(frozen=True)
class ContractCfg:
    band_min: float = 0.50
    band_max: float = 0.60
    itm_points: float = 5.0
    otm_points: float = 0.0
    tp: float | None = 0.20
    stop: float | None = None

    @property
    def name(self) -> str:
        tp = "off" if self.tp is None else f"{round(self.tp * 100)}%"
        stop = "off" if self.stop is None else f"{round(self.stop * 100)}%"
        return f"delta {self.band_min:.2f}-{self.band_max:.2f}, otm {self.otm_points:g}, tp {tp}, stop {stop}"


@dataclass
class Trade:
    day: str
    side: int
    strike: float
    delta: float
    entry_time: str
    limit: float
    entry: float
    exit_time: str = ""
    exit: float = 0.0
    reason: str = ""

    @property
    def pnl(self) -> float:
        return (self.exit - self.entry) * MULTIPLIER


@dataclass
class Leg:
    trade: Trade
    limit: float
    right: str
    tp_limit: float | None
    stop_price: float | None


def choose_strike(symbol: str, day: str, ts: datetime, spot: float, side: int,
                  cfg: ContractCfg) -> tuple[float, float] | None:
    """Mirror of `choose_leg`: band distance, then distance to the band midpoint, then to spot."""
    step = STRIKE_STEP.get(symbol, 1.0)
    scale = POINT_SCALE.get(symbol, 1.0)
    right = "call" if side > 0 else "put"
    itm, otm = cfg.itm_points * scale, cfg.otm_points * scale
    lo, hi = (spot - itm, spot + otm) if side > 0 else (spot - otm, spot + itm)
    mid = (cfg.band_min + cfg.band_max) / 2
    best = None
    k = math.ceil(lo / step) * step
    while k <= hi + 1e-9:
        d = abs(pricing.delta(symbol, day, ts, spot, k, right))
        band_distance = 0.0 if cfg.band_min <= d <= cfg.band_max else min(abs(d - cfg.band_min), abs(d - cfg.band_max))
        key = (round(band_distance, 6), abs(d - mid), abs(k - spot))
        if best is None or key < best[0]:
            best = (key, k, d)
        k += step
    if best is None:
        return None
    return best[1], best[2]


def price_bars(symbol: str) -> dict[str, list[Bar]]:
    source = "SPX" if symbol in ("XSP", "SPX") else symbol
    scale = 0.1 if symbol == "XSP" else 1.0
    rows = {day: [[s, o * scale, h * scale, l * scale, c * scale, v] for s, o, h, l, c, v in r]
            for day, r in pricing.underlying()[source].items()}
    return strategies.bars_from_rows(rows)


def _open(symbol: str, day: str, bar: Bar, side: int, cfg: ContractCfg) -> Leg | None:
    picked = choose_strike(symbol, day, bar.end, bar.c, side, cfg)
    if picked is None:
        return None
    strike, d = picked
    right = "call" if side > 0 else "put"
    limit = pricing.mark(symbol, day, bar.end, bar.c, strike, right)
    if limit < 0.05:
        return None
    limit = round(limit, 2)
    fill = limit + cost(symbol, limit)
    trade = Trade(day, side, strike, round(d, 3), bar.end.strftime("%H:%M"), limit, round(fill, 2))
    tp_limit = None if cfg.tp is None else round(limit * (1 + cfg.tp), 2)
    stop_price = None if cfg.stop is None else round(limit * (1 - cfg.stop), 2)
    return Leg(trade, limit, right, tp_limit, stop_price)


def _close(symbol: str, day: str, leg: Leg, bar: Bar, reason: str) -> Trade:
    m = pricing.mark(symbol, day, bar.end, bar.c, leg.trade.strike, leg.right)
    leg.trade.exit = round(max(0.0, m - cost(symbol, m)), 2)
    leg.trade.exit_time = bar.end.strftime("%H:%M")
    leg.trade.reason = reason
    return leg.trade


def _resting_exit(symbol: str, day: str, leg: Leg, bar: Bar) -> Trade | None:
    """Stop first when a bar touches both; the stop-limit fills at the worse of stop and the bar-close mark."""
    favorable, adverse = (bar.h, bar.l) if leg.right == "call" else (bar.l, bar.h)
    strike = leg.trade.strike
    if leg.stop_price is not None:
        low_mark = pricing.mark(symbol, day, bar.end, adverse, strike, leg.right)
        if low_mark <= leg.stop_price:
            close_mark = pricing.mark(symbol, day, bar.end, bar.c, strike, leg.right)
            px = min(leg.stop_price, close_mark)
            leg.trade.exit = round(max(0.0, px - cost(symbol, px)), 2)
            leg.trade.exit_time = bar.end.strftime("%H:%M")
            leg.trade.reason = "stop"
            return leg.trade
    if leg.tp_limit is not None:
        touch = favorable if STRESS["tp_on_extreme"] else bar.c
        high_mark = pricing.mark(symbol, day, bar.end, touch, strike, leg.right)
        if high_mark >= leg.tp_limit + cost(symbol, leg.tp_limit):
            leg.trade.exit = leg.tp_limit
            leg.trade.exit_time = bar.end.strftime("%H:%M")
            leg.trade.reason = "take_profit"
            return leg.trade
    return None


def simulate_day(symbol: str, day: str, bars: list[Bar], targets: list[Target],
                 cfg: ContractCfg) -> list[Trade]:
    trades: list[Trade] = []
    tv_pos, entries, leg = 0, 0, None
    for bar, target in zip(bars, targets):
        if leg is not None and leg.trade.entry_time != bar.end.strftime("%H:%M"):
            done = _resting_exit(symbol, day, leg, bar)
            if done is not None:
                trades.append(done)
                leg = None
        if bar.end.time() >= FLATTEN_AT:
            if leg is not None:
                trades.append(_close(symbol, day, leg, bar, "flatten"))
            return trades
        if tv_pos != 0 and target is not None and target != tv_pos:
            tv_pos = 0
            if leg is not None:
                trades.append(_close(symbol, day, leg, bar, "signal"))
                leg = None
        elif tv_pos == 0 and target in (1, -1) and bar.end.time() < cutoff(symbol) and entries < MAX_ENTRIES:
            tv_pos = target
            entries += 1
            leg = _open(symbol, day, bar, target, cfg)
    if leg is not None:
        trades.append(_close(symbol, day, leg, bars[-1], "flatten"))
    return trades


def simulate(symbol: str, variant: Variant, cfg: ContractCfg,
             signal_bars: dict[str, list[Bar]], spot_bars: dict[str, list[Bar]],
             targets: dict[str, list[Target]] | None = None) -> list[Trade]:
    targets = targets if targets is not None else strategies.targets_for(variant, signal_bars)
    trades = []
    for day in sorted(spot_bars):
        if day in WARMUP or not pricing.smiles(symbol, day):
            continue
        trades.extend(simulate_day(symbol, day, spot_bars[day], targets[day], cfg))
    return trades


def _percentile(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def metrics(trades: list[Trade], sessions: int) -> dict:
    pnl = [t.pnl for t in trades]
    wins = [p for p in pnl if p > 0]
    losses = [-p for p in pnl if p < 0]
    equity = peak = drawdown = 0.0
    for p in pnl:
        equity += p
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    premiums = [t.entry for t in trades]
    return {
        "trades": len(trades),
        "trades_per_day": round(len(trades) / sessions, 2) if sessions else 0.0,
        "win_rate": round(len(wins) / len(pnl), 3) if pnl else 0.0,
        "avg_pnl": round(sum(pnl) / len(pnl), 2) if pnl else 0.0,
        "total_pnl": round(sum(pnl), 2),
        "profit_factor": round(sum(wins) / sum(losses), 2) if losses else (99.0 if wins else 0.0),
        "max_drawdown": round(drawdown, 2),
        "avg_premium": round(sum(premiums) / len(premiums), 2) if premiums else 0.0,
        "premium_p50": round(_percentile(premiums, 0.5), 2),
        "premium_p90": round(_percentile(premiums, 0.9), 2),
        "premium_p95": round(_percentile(premiums, 0.95), 2),
    }


def apply_cap(trades: list[Trade], cap: float | None) -> list[Trade]:
    """MAX_PREMIUM rejects opens whose limit is above the cap. Applied after the fact because a
    rejected open leaves the relay flat until TradingView's next entry, which the simulation already does."""
    return trades if cap is None else [t for t in trades if t.limit <= cap]


@dataclass
class Result:
    symbol: str
    variant: str
    family: str
    contract: str
    cap: float | None
    sessions_in: int
    sessions_out: int
    in_sample: dict = field(default_factory=dict)
    out_sample: dict = field(default_factory=dict)
    all: dict = field(default_factory=dict)


def evaluate(symbol: str, variant: Variant, cfg: ContractCfg, trades: list[Trade], cap: float | None,
             days_in: int, days_out: int) -> Result:
    kept = apply_cap(trades, cap)
    ins = [t for t in kept if t.day <= IN_SAMPLE_END]
    outs = [t for t in kept if t.day > IN_SAMPLE_END]
    return Result(symbol, variant.name, variant.family, cfg.name, cap, days_in, days_out,
                  metrics(ins, days_in), metrics(outs, days_out), metrics(kept, days_in + days_out))


def contract_grid() -> list[ContractCfg]:
    bands = ((0.30, 0.40, 5.0), (0.40, 0.50, 5.0), (0.50, 0.60, 0.0), (0.60, 0.75, 0.0))
    out = []
    for lo, hi, otm in bands:
        for tp in (None, 0.15, 0.20, 0.30, 0.50):
            for stop in (None, 0.30, 0.50):
                out.append(ContractCfg(lo, hi, 5.0, otm, tp, stop))
    return out


def sessions(symbol: str) -> tuple[int, int]:
    days = [d for d in pricing.underlying()["SPY"] if d not in WARMUP and pricing.smiles(symbol, d)]
    return sum(d <= IN_SAMPLE_END for d in days), sum(d > IN_SAMPLE_END for d in days)


def score(r: Result) -> float:
    """In-sample expectancy per trade, penalised by drawdown relative to total profit."""
    m = r.in_sample
    if m["trades"] < MIN_TRADES_IN_SAMPLE or m["avg_pnl"] <= 0:
        return -1e9
    return m["avg_pnl"] * min(1.0, m["total_pnl"] / max(m["max_drawdown"], 1.0) / 2)


def confirmed(r: Result) -> bool:
    """Ranked on the in-sample sessions, kept only if the held-out sessions agree."""
    o = r.out_sample
    return score(r) > -1e9 and o["avg_pnl"] > 0 and o["profit_factor"] >= MIN_PROFIT_FACTOR_OUT


def run() -> dict:
    RESULTS.mkdir(exist_ok=True)
    default = ContractCfg()
    signal_cache = {sym: price_bars(sym) for sym in ("SPY", "QQQ", "IWM")}
    spot_cache = {sym: price_bars(sym) for sym in SYMBOLS}
    target_cache: dict[tuple[str, str], dict] = {}

    def targets(sym: str, v: Variant) -> dict:
        key = (SIGNAL_SOURCE[sym], v.name)
        if key not in target_cache:
            target_cache[key] = strategies.targets_for(v, signal_cache[SIGNAL_SOURCE[sym]])
        return target_cache[key]

    stage1: list[Result] = []
    for sym in SYMBOLS:
        days_in, days_out = sessions(sym)
        for v in strategies.variants():
            trades = simulate(sym, v, default, signal_cache[SIGNAL_SOURCE[sym]], spot_cache[sym], targets(sym, v))
            for cap in PREMIUM_CAPS:
                stage1.append(evaluate(sym, v, default, trades, cap, days_in, days_out))

    capped = [r for r in stage1 if r.cap == 5.0]
    capped.sort(key=score, reverse=True)
    finalists = []
    for r in capped:
        if score(r) <= -1e9:
            break
        if (r.symbol, r.variant) not in finalists:
            finalists.append((r.symbol, r.variant))
        if len(finalists) == 8:
            break

    by_name = {v.name: v for v in strategies.variants()}
    stage2: list[Result] = []
    trade_log: dict[str, list[dict]] = {}
    for sym, name in finalists:
        v = by_name[name]
        days_in, days_out = sessions(sym)
        for cfg in contract_grid():
            trades = simulate(sym, v, cfg, signal_cache[SIGNAL_SOURCE[sym]], spot_cache[sym], targets(sym, v))
            result = evaluate(sym, v, cfg, trades, 5.0, days_in, days_out)
            stage2.append(result)
            trade_log[f"{sym}|{name}|{cfg.name}"] = [asdict(t) | {"pnl": round(t.pnl, 2)}
                                                    for t in apply_cap(trades, 5.0)]
    stage2.sort(key=score, reverse=True)
    winner = next((r for r in stage2 if confirmed(r)), None)

    robustness = []
    for sym, name in finalists:
        rows = [r for r in stage2 if r.symbol == sym and r.variant == name]
        robustness.append({
            "symbol": sym, "variant": name, "configs": len(rows),
            "in_positive": sum(r.in_sample["avg_pnl"] > 0 for r in rows),
            "out_positive": sum(r.out_sample["avg_pnl"] > 0 for r in rows),
            "both_positive": sum(r.in_sample["avg_pnl"] > 0 and r.out_sample["avg_pnl"] > 0 for r in rows),
            "confirmed": sum(confirmed(r) for r in rows),
        })

    winner_caps, stress = [], []
    if winner is not None:
        v, cfg = by_name[winner.variant], next(c for c in contract_grid() if c.name == winner.contract)
        sym = winner.symbol
        days_in, days_out = sessions(sym)

        def sim() -> list[Trade]:
            return simulate(sym, v, cfg, signal_cache[SIGNAL_SOURCE[sym]], spot_cache[sym], targets(sym, v))

        base = sim()
        winner_caps = [asdict(evaluate(sym, v, cfg, base, cap, days_in, days_out)) for cap in PREMIUM_CAPS]
        for label, patch in (("take-profit fills on bar close only", {"tp_on_extreme": False}),
                             ("double costs", {"cost_multiplier": 2.0}),
                             ("both", {"tp_on_extreme": False, "cost_multiplier": 2.0})):
            saved = dict(STRESS)
            STRESS.update(patch)
            try:
                stress.append({"label": label} | asdict(evaluate(sym, v, cfg, sim(), 5.0, days_in, days_out)))
            finally:
                STRESS.update(saved)
    summary = {
        "winner": asdict(winner) if winner else None,
        "winner_trades": trade_log.get(f"{winner.symbol}|{winner.variant}|{winner.contract}", []) if winner else [],
        "finalists": [{"symbol": s, "variant": n} for s, n in finalists],
        "robustness": robustness,
        "signal_ranking": [asdict(r) | {"score": round(score(r), 2)} for r in capped[:30]],
        "stage2_top": [asdict(r) | {"score": round(score(r), 2)} for r in stage2[:40]],
        "winner_caps": winner_caps,
        "stress": stress,
        "validation": [pricing.validate(s) for s in ("SPY", "QQQ", "IWM")],
    }
    (RESULTS / "stage1.json").write_text(json.dumps([asdict(r) | {"score": round(score(r), 2)} for r in stage1], indent=1))
    (RESULTS / "stage2.json").write_text(json.dumps([asdict(r) | {"score": round(score(r), 2)} for r in stage2], indent=1))
    (RESULTS / "summary.json").write_text(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    out = run()
    w = out["winner"]
    print(json.dumps({"winner": w, "finalists": out["finalists"]}, indent=1))
