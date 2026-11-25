from datetime import date
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)


class Parameters(Model):
    opening_minutes: Literal[5, 15, 30] = 15
    ema_period: int = Field(default=20, ge=5, le=50, strict=True)
    stop_pct: Decimal = Field(default=Decimal('0.30'), ge=Decimal('0.05'), le=Decimal('2'), decimal_places=2)
    target_r: Decimal = Field(default=Decimal('2'), ge=Decimal('.5'), le=Decimal('5'), decimal_places=2)
    max_entries: int = Field(default=3, ge=1, le=5, strict=True)
    # Each side pays half the spread plus slippage and a per-share commission.
    spread_bps: Decimal = Field(default=Decimal('1'), ge=0, le=100, decimal_places=2)
    slippage_bps: Decimal = Field(default=Decimal('1'), ge=0, le=100, decimal_places=2)
    commission_per_share: Decimal = Field(default=Decimal('.005'), ge=0, le=1, decimal_places=4)
    short_borrow_bps_annual: Decimal = Field(default=Decimal('100'), ge=0, le=10000, decimal_places=2)
    initial_equity: Decimal = Field(default=Decimal('100000'), ge=1000, le=10000000, decimal_places=2)


class RunRequest(Model):
    request_id: UUID
    dataset_id: str = Field(pattern=r'^[a-f0-9]{64}$')
    strategy_id: str = Field(min_length=1, max_length=100)
    template: Literal['orb_bull', 'orb_bear', 'vwap_bull', 'vwap_bear']
    split_date: date
    parameters: Parameters = Field(default_factory=Parameters)


class Upload(Model):
    symbol: Literal['SPY', 'QQQ']
    source: str = Field(min_length=3, max_length=300)
    kind: Literal['historical', 'synthetic'] = 'historical'
    csv: str = Field(min_length=1, max_length=3000000)


class AlpacaImport(Model):
    symbol: Literal['SPY', 'QQQ']
    start: date
    end: date
