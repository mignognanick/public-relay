"""Validated dashboard state and advisory decisions, independent of execution."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

from quant_engine.risk.config import RiskConfig

Regime = Literal['strong_bull', 'bull', 'chop', 'bear', 'strong_bear', 'high_volatility']
PortfolioID = Literal['day', 'swing', 'long_term']
Stage = Literal['idea', 'specified', 'backtested', 'validated', 'paper', 'archived']
REGIMES = ['strong_bull', 'bull', 'chop', 'bear', 'strong_bear', 'high_volatility']
STAGES = ['idea', 'specified', 'backtested', 'validated', 'paper', 'archived']


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Portfolio(Model):
    id: PortfolioID
    name: str
    mode: Literal['manual', 'hybrid', 'automatic'] = 'hybrid'
    regime: Regime = 'chop'
    override: Regime | None = None
    override_until: datetime | None = None
    paused: StrictBool = True

    @field_validator('override_until')
    @classmethod
    def aware(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError('Override expiry must include timezone')
        return value


class Strategy(Model):
    id: str
    name: str = Field(min_length=1, max_length=100)
    portfolio: PortfolioID
    thesis: str = Field(min_length=1, max_length=4000)
    stage: Stage = 'idea'
    enabled: StrictBool = False
    regimes: list[Regime] = Field(default_factory=lambda: ['chop'], min_length=1)
    evidence: str = Field(default='', max_length=4000)
    held_out_trades: int = Field(default=0, ge=0, le=10000000, strict=True)
    trials: int = Field(default=0, ge=0, le=10000000, strict=True)
    costs_modeled: StrictBool = False
    approved_by: str | None = None


class Tax(Model):
    carryforward: Decimal = Field(default=Decimal('35000'), ge=0, le=Decimal('1000000000'), max_digits=12, decimal_places=2)
    source: Literal['user_stated_unverified'] = 'user_stated_unverified'
    short_term_net: Decimal = Field(default=Decimal('0'), ge=Decimal('-1000000000'), le=Decimal('1000000000'), max_digits=12, decimal_places=2)
    long_term_net: Decimal = Field(default=Decimal('0'), ge=Decimal('-1000000000'), le=Decimal('1000000000'), max_digits=12, decimal_places=2)
    filing: Literal['other', 'married_filing_separately'] = 'other'
    sufficient_income: StrictBool = False


class State(Model):
    execution: Literal['PAPER'] = 'PAPER'
    paused: StrictBool = True
    portfolios: list[Portfolio]
    strategies: list[Strategy] = Field(default_factory=list)
    tax: Tax = Field(default_factory=Tax)

    @model_validator(mode='after')
    def registry_integrity(self):
        if len(self.portfolios) != 3 or {p.id for p in self.portfolios} != {'day', 'swing', 'long_term'}:
            raise ValueError('Exactly one of each portfolio is required')
        if len({s.id for s in self.strategies}) != len(self.strategies):
            raise ValueError('Strategy IDs must be unique')
        for strategy in self.strategies:
            if strategy.enabled and (strategy.stage != 'paper' or not strategy.approved_by):
                raise ValueError('Enabled strategy must have paper approval')
        return self


def initial_state():
    return State(portfolios=[Portfolio(id=i, name=n) for i, n in [
        ('day', 'Day Trading'), ('swing', 'Swing Trading'), ('long_term', 'Long-Term Investing')]])


class Command(Model):
    version: int = Field(ge=0, strict=True)
    action: Literal['global_pause', 'portfolio', 'create_strategy', 'strategy', 'tax']
    target: str = Field(default='', max_length=100)
    reason: str = Field(min_length=3, max_length=500)
    values: dict


def transition(state: State, command: Command, actor: str, now: datetime):
    result = deepcopy(state)
    values = command.values
    if command.action == 'global_pause':
        if set(values) != {'paused'} or type(values['paused']) is not bool:
            raise ValueError('Supply paused as true or false')
        result.paused = values['paused']
    elif command.action == 'portfolio':
        portfolio = next((p for p in result.portfolios if p.id == command.target), None)
        if portfolio is None:
            raise ValueError('Unknown portfolio')
        if not set(values) <= {'mode', 'regime', 'override', 'override_until', 'paused'}:
            raise ValueError('Unsupported portfolio setting')
        updated = Portfolio.model_validate(portfolio.model_dump() | values)
        if updated.override and (not updated.override_until or updated.override_until <= now):
            raise ValueError('Overrides require a future expiry')
        if not updated.override:
            updated.override_until = None
        result.portfolios[result.portfolios.index(portfolio)] = updated
    elif command.action == 'create_strategy':
        if len(result.strategies) >= 200:
            raise ValueError('Registry limit reached')
        if not set(values) <= {'name', 'portfolio', 'thesis', 'regimes'}:
            raise ValueError('New strategies must start as disabled ideas')
        result.strategies.append(Strategy(id=str(uuid4()), **values))
    elif command.action == 'strategy':
        strategy = next((s for s in result.strategies if s.id == command.target), None)
        if strategy is None:
            raise ValueError('Unknown strategy')
        if not set(values) <= {'stage', 'enabled', 'evidence', 'held_out_trades', 'trials', 'costs_modeled'}:
            raise ValueError('Research definitions are immutable; register a new version')
        updated = Strategy.model_validate(strategy.model_dump() | values)
        if strategy.stage == 'archived':
            raise ValueError('Archived research is immutable')
        if updated.stage != strategy.stage and updated.stage != 'archived':
            if STAGES.index(updated.stage) != STAGES.index(strategy.stage) + 1:
                raise ValueError('Advance one research stage at a time')
        if updated.stage in ('backtested', 'validated', 'paper'):
            if not updated.evidence or updated.trials < 1 or not updated.costs_modeled:
                raise ValueError('Record reproducible evidence, trial count and modeled costs')
        if updated.stage in ('validated', 'paper') and updated.held_out_trades < 30:
            raise ValueError('At least 30 held-out trades required')
        if updated.stage == 'paper' and strategy.stage != 'paper':
            updated.approved_by = actor
        if updated.stage == 'archived':
            updated.enabled = False
        if updated.enabled and updated.stage != 'paper':
            raise ValueError('Only human-approved paper strategies can be enabled')
        result.strategies[result.strategies.index(strategy)] = updated
    elif command.action == 'tax':
        result.tax = Tax.model_validate(result.tax.model_dump() | values)
    else:
        raise ValueError('Unknown action')
    return State.model_validate(result.model_dump())


def eligibility(state: State, strategy: Strategy, now: datetime):
    p = next(p for p in state.portfolios if p.id == strategy.portfolio)
    active_override = p.override and p.override_until and p.override_until > now
    regime = p.override if active_override else (None if p.mode == 'automatic' else p.regime)
    reasons = []
    if state.paused or p.paused:
        reasons.append('New entries paused')
    if strategy.stage != 'paper' or not strategy.approved_by:
        reasons.append('Paper approval required')
    if not strategy.enabled:
        reasons.append('Strategy switch off')
    if regime is None:
        reasons.append('Automatic classifier unavailable')
    elif regime not in strategy.regimes:
        reasons.append('Regime outside strategy permissions')
    return {'eligible_for_risk_review': not reasons, 'reasons': reasons, 'effective_regime': regime,
            'execution_active': False, 'worker_acknowledged_version': None,
            'note': 'Advisory only. No worker or broker is connected; independent risk approval still required.'}


def tax_plan(tax: Tax):
    net = tax.short_term_net + tax.long_term_net
    offset = min(tax.carryforward, max(Decimal(0), net))
    losses = max(Decimal(0), tax.carryforward - net)
    cap = Decimal('1500') if tax.filing == 'married_filing_separately' else Decimal('3000')
    deduction = min(losses, cap) if tax.sufficient_income else Decimal(0)
    return {k: str(v) for k, v in {
        'gains_offset': offset, 'remaining_net_gains': max(Decimal(0), net - tax.carryforward),
        'losses_before_deduction': losses, 'illustrative_deduction': deduction,
        'illustrative_next_carryforward': losses - deduction}.items()}


def view(state: State, version: int):
    now = datetime.now(timezone.utc)
    return {'version': version, 'state': state.model_dump(mode='json'),
            'eligibility': {s.id: eligibility(state, s, now) for s in state.strategies},
            'tax_plan': tax_plan(state.tax), 'risk_limits': RiskConfig().public_view(),
            'data_source': 'configuration_and_user_scenarios', 'broker_connected': False,
            'balances': None, 'performance': None, 'worker_acknowledged_version': None}
