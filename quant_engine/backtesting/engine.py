"""Causal five-minute underlying-share simulator; all money uses Decimal."""
import hashlib
import json
from collections import Counter
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from quant_engine.backtesting.data import parse_csv
from quant_engine.backtesting.models import Parameters
from quant_engine.risk.config import RiskConfig
from quant_engine.risk.engine import RiskEngine
from quant_engine.risk.models import AccountSnapshot, EntryRequest, ExitRequest, Side
from quant_engine.risk.state import InMemoryStateStore
from quant_engine.risk.sizing import size_position

ENGINE_VERSION = 'underlying-v1.0.0'
TEMPLATES = {
    'orb_bull': 'Bullish Opening Range Breakout (ORB)',
    'orb_bear': 'Bearish Opening Range Breakout (ORB)',
    'vwap_bull': 'Bullish VWAP Pullback',
    'vwap_bear': 'Bearish VWAP Pullback',
}
RULES = {
    'orb': 'After the opening range completes, a closed bar crosses above its high (bull) or below its low (bear). Enter at the next bar open.',
    'vwap': 'After EMA warmup each session, previous close is on the trend side of previous VWAP; current bar touches VWAP and closes back on the trend side, with rising/falling EMA and close beyond EMA. Enter at the next bar open.',
    'exits': 'Fixed percentage stop from simulated entry, target at target_r times that distance. Stop first if both touched; gap stops at worse open; target gaps receive no price improvement. Flatten at final session bar open.',
    'timing': 'No entries in final 30 minutes. One position at a time, no re-entry on an exit bar. EMA and typical-price volume-weighted VWAP reset each session. No regime filter; test both directions independently.',
}


def implementation_hash():
    root = Path(__file__).parents[1]
    paths = [Path(__file__), Path(__file__).with_name('data.py'), Path(__file__).with_name('models.py')]
    paths += sorted((root / 'risk').glob('*.py'))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(str(path.relative_to(root)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def signals(bars, template, params):
    direction = 1 if template.endswith('bull') else -1
    n = params.opening_minutes // 5
    alpha = Decimal(2) / (params.ema_period + 1)
    out, numerator, denominator, ema, previous_vwap = [], Decimal(0), Decimal(0), None, None
    for i, b in enumerate(bars):
        previous_ema = ema
        ema = b.c if ema is None else alpha*b.c+(1-alpha)*ema
        numerator += (b.h+b.l+b.c)/3*b.volume
        denominator += b.volume
        vwap = numerator/denominator if denominator else None
        signal = False
        if template.startswith('orb') and i >= n:
            level = max(x.h for x in bars[:n]) if direction == 1 else min(x.l for x in bars[:n])
            signal = b.c > level >= bars[i-1].c if direction == 1 else b.c < level <= bars[i-1].c
        elif template.startswith('vwap') and i >= params.ema_period and vwap is not None and previous_vwap is not None:
            if direction == 1:
                signal = bars[i-1].c > previous_vwap and b.l <= vwap < b.c and b.c > ema > previous_ema
            else:
                signal = bars[i-1].c < previous_vwap and b.h >= vwap > b.c and b.c < ema < previous_ema
        out.append(bool(signal))
        previous_vwap = vwap
    return out


def dec(value):
    return format(value.quantize(Decimal('.000001')), 'f')


def simulate(bars, template, params):
    sessions = {}
    for b in bars:
        sessions.setdefault(b.day, []).append(b)
    store = InMemoryStateStore()
    clock = [bars[0].start]
    risk_config = RiskConfig()
    risk = RiskEngine(risk_config, store, clock=lambda: clock[0])
    equity = params.initial_equity
    peak, drawdown = equity, Decimal(0)
    trades, curve, rejects = [], [], Counter()
    exposed = 0
    direction = 1 if template.endswith('bull') else -1
    cost_rate = (params.spread_bps/2+params.slippage_bps)/10000
    week_key, week_start = None, equity
    for day, day_bars in sessions.items():
        day_start, entries, position = equity, 0, None
        current_week = day_bars[0].start.isocalendar()[:2]
        if current_week != week_key:
            week_key, week_start = current_week, equity
        decisions = signals(day_bars, template, params)
        for i, b in enumerate(day_bars):
            clock[0] = b.start
            closing_bar = i == len(day_bars)-1
            had_position = position is not None
            if position is None and not closing_bar and i > 0 and decisions[i-1] and entries < params.max_entries:
                if b.start >= b.session_close-timedelta(minutes=30):
                    rejects['SESSION_CUTOFF'] += 1
                elif equity <= 0:
                    rejects['INSOLVENT'] += 1
                else:
                    entry = b.o*(1+direction*cost_rate)
                    distance = entry*params.stop_pct/100
                    stop, target = entry-direction*distance, entry+direction*distance*params.target_r
                    position_id = f'{day}-{i}'
                    borrow_reserve = entry*params.short_borrow_bps_annual/10000/252 if direction < 0 else Decimal(0)
                    cost_per_unit = stop*cost_rate+2*params.commission_per_share+borrow_reserve
                    size = size_position(equity, risk_config.max_trade_risk_pct,
                                         Side.LONG if direction == 1 else Side.SHORT,
                                         entry, stop, 1, cost_per_unit)
                    decision = risk.evaluate_entry(
                        EntryRequest(position_id, template, 'UNDERLYING', 'LONG' if direction == 1 else 'SHORT',
                                     entry, stop, requested_qty=max(1, size.qty), cost_per_unit=cost_per_unit),
                        AccountSnapshot(equity, b.start, day_start, week_start))
                    if decision.approved:
                        risk.confirm_entry_fill(position_id, decision.qty, entry)
                        position = {'id': position_id, 'entry_time': b.start.isoformat(), 'signal_time': (day_bars[i-1].start+timedelta(minutes=5)).isoformat(),
                                    'signal_bar_start': day_bars[i-1].start.isoformat(),
                                    'entry': entry, 'stop': stop, 'target': target, 'qty': decision.qty, 'entry_fee': decision.qty*params.commission_per_share,
                                    'planned_risk': decision.planned_risk, 'borrow': borrow_reserve*decision.qty}
                        equity -= position['entry_fee']
                        entries += 1
                    else:
                        rejects.update(str(r) for r in decision.reasons)
            if position is not None:
                p = position
                if not closing_bar:
                    exposed += 1
                raw_exit, reason = None, None
                if closing_bar:
                    raw_exit, reason = b.o, 'session_flatten'
                elif (direction == 1 and b.o <= p['stop']) or (direction == -1 and b.o >= p['stop']):
                    raw_exit, reason = b.o, 'gap_stop'
                elif (direction == 1 and b.l <= p['stop']) or (direction == -1 and b.h >= p['stop']):
                    raw_exit, reason = p['stop'], 'stop'
                elif (direction == 1 and b.h >= p['target']) or (direction == -1 and b.l <= p['target']):
                    raw_exit, reason = p['target'], 'target'
                if raw_exit is not None:
                    exit_price = raw_exit*(1-direction*cost_rate)
                    approval = risk.evaluate_exit(ExitRequest(p['id'], p['qty'], reason))
                    if not approval.approved:
                        raise RuntimeError('Protective exit unexpectedly rejected')
                    gross = direction*(exit_price-p['entry'])*p['qty']
                    fees = p['entry_fee']+p['qty']*params.commission_per_share+p['borrow']
                    equity += gross-p['qty']*params.commission_per_share-p['borrow']
                    risk.record_exit_fill(p['id'], p['qty'])
                    trades.append({**{k: dec(v) if isinstance(v, Decimal) else v for k, v in p.items()},
                                   'exit_bar_start': b.start.isoformat(), 'exit_price': dec(exit_price),
                                   'reason': reason, 'direction': 'long' if direction == 1 else 'short',
                                   'net_pnl': dec(gross-fees), 'fees_and_borrow': dec(fees)})
                    position = None
            marked = equity if position is None else equity+direction*(b.c-position['entry'])*position['qty']-position['qty']*params.commission_per_share-position['borrow']
            peak = max(peak, marked)
            drawdown = max(drawdown, (peak-marked)/peak*100)
            # A position stopped within this bar is not allowed to re-enter until a later bar.
            if had_position and position is None:
                decisions[i] = False
        curve.append({'date': day, 'equity': dec(equity)})
    pnls = [Decimal(t['net_pnl']) for t in trades]
    wins, losses = sum((p for p in pnls if p > 0), Decimal(0)), -sum((p for p in pnls if p < 0), Decimal(0))
    benchmark_return = (bars[-1].o/bars[0].o-1)*100
    return {'metrics': {'net_return_pct': dec((equity/params.initial_equity-1)*100),
                        'net_pnl': dec(equity-params.initial_equity), 'ending_equity': dec(equity),
                        'max_bar_close_drawdown_pct': dec(drawdown), 'trades': len(trades),
                        'win_rate_pct': dec(Decimal(sum(p > 0 for p in pnls))/len(pnls)*100) if pnls else None,
                        'expectancy': dec(sum(pnls)/len(pnls)) if pnls else None,
                        'profit_factor': dec(wins/losses) if losses else None,
                        'profit_factor_note': 'No losing trades / undefined' if not losses else None,
                        'exposure_pct': dec(Decimal(exposed)/len(bars)*100),
                        'benchmark_price_return_pct': dec(benchmark_return)},
            'trades': trades, 'equity_curve': curve, 'rejected_entries': dict(rejects),
            'risk_status': risk.status(), 'sessions': len(sessions)}


def run_backtest(dataset, request):
    if request.template not in TEMPLATES:
        raise ValueError('Unsupported strategy template')
    bars = parse_csv(dataset['csv'])
    split = request.split_date.isoformat()
    development = [b for b in bars if b.day <= split]
    holdout = [b for b in bars if b.day > split]
    if min(len({b.day for b in development}), len({b.day for b in holdout})) < 2:
        raise ValueError('Each split must contain at least two complete sessions')
    result = {'engine_version': ENGINE_VERSION, 'implementation_sha256': implementation_hash(),
              'template': request.template, 'rules': RULES, 'parameters': request.parameters.model_dump(mode='json'),
              'dataset': dataset['meta'], 'split_date': split,
              'development': simulate(development, request.template, request.parameters),
              'holdout': simulate(holdout, request.template, request.parameters),
              'risk_limits': RiskConfig().public_view(), 'execution_active': False,
              'warnings': ['Independent starting equity and risk state in each split; results are not additive.',
                           'Held-out results stop being unseen after inspection. Retuning requires a new reserved period.',
                           'Bar-close drawdown can understate intrabar loss. Stop gaps can exceed planned risk.',
                           'Bearish runs simulate short shares with assumed borrow availability; they are not put-option returns.',
                           'Benchmark is unlevered buy-and-hold price return, excluding dividends and costs.',
                           'No automatic strategy promotion. No regime classifier or parameter search.']}
    if dataset['meta']['kind'] == 'synthetic':
        result['warnings'].insert(0, 'SYNTHETIC DEMO: fabricated data; not evidence of profitability.')
    result['minimum_trade_count_met'] = dataset['meta']['kind'] == 'historical' and result['holdout']['metrics']['trades'] >= 30
    result['paper_approved'] = False
    return result
