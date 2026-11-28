"""Point-in-time causal feature store for US equities and ETFs.

Guarantees that any query at timestamp t can only see OHLCV bars and sentiment
documents whose timestamps are <= t, preventing look-ahead bias and data leakage.
"""
from __future__ import annotations

import csv
import io
import math
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal, Mapping, Sequence

from quant_engine.backtesting.data import parse_csv as parse_intraday_csv
from research.library import canonical_hash, dec_from_float, q2, q6

DatasetKind = Literal["historical", "synthetic"]


@dataclass(frozen=True)
class MarketBar:
    """Single OHLCV bar with explicit timezone-aware bar-close availability timestamp."""

    symbol: str
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    @property
    def day(self) -> str:
        return self.timestamp.date().isoformat()


@dataclass(frozen=True)
class TextObservation:
    """Timestamped headline or sentiment text strictly available at `published_at`."""

    symbol: str
    published_at: datetime
    text: str
    source: str = "research_feed"

    @property
    def day(self) -> str:
        return self.published_at.date().isoformat()


@dataclass(frozen=True)
class DatasetBundle:
    """Immutable, content-hashed multi-symbol US equity/ETF dataset."""

    dataset_id: str
    kind: DatasetKind
    source: str
    symbols: tuple[str, ...]
    bars_by_symbol: Mapping[str, tuple[MarketBar, ...]]
    texts_by_symbol: Mapping[str, tuple[TextObservation, ...]]
    dates: tuple[str, ...]
    warnings: tuple[str, ...]

    def meta(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset_id,
            "kind": self.kind,
            "source": self.source,
            "symbols": list(self.symbols),
            "start": self.dates[0] if self.dates else None,
            "end": self.dates[-1] if self.dates else None,
            "sessions": len(self.dates),
            "bar_counts": {s: len(self.bars_by_symbol[s]) for s in self.symbols},
            "text_counts": {s: len(self.texts_by_symbol.get(s, ())) for s in self.symbols},
            "warnings": list(self.warnings),
        }


def _parse_decimal(raw: str, label: str) -> Decimal:
    try:
        val = Decimal(raw.strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"Invalid decimal for {label}: {raw!r}") from exc
    if not val.is_finite():
        raise ValueError(f"Non-finite decimal for {label}: {raw!r}")
    return val


def _parse_timestamp(raw: str) -> datetime:
    text = raw.strip()
    if len(text) == 10:
        # Daily date string YYYY-MM-DD -> US market close 16:00 UTC-5 / 21:00 UTC
        d = date.fromisoformat(text)
        return datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc)
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"Timestamp must include explicit timezone: {raw!r}")
    return dt


def validate_bars(bars: Sequence[MarketBar], now: datetime | None = None) -> tuple[MarketBar, ...]:
    """Validate strict ordering, OHLC invariants, positive prices, and no future timestamps."""
    if not bars:
        raise ValueError("Bar series must not be empty")
    ordered = tuple(bars)
    seen_ts: set[datetime] = set()
    prev_ts: datetime | None = None
    for b in ordered:
        if b.timestamp.tzinfo is None or b.timestamp.utcoffset() is None:
            raise ValueError(f"Bar timestamp for {b.symbol} must be timezone-aware")
        if now is not None and b.timestamp > now:
            raise ValueError(f"Future bar rejected for {b.symbol} at {b.timestamp.isoformat()}")
        if b.timestamp in seen_ts:
            raise ValueError(f"Duplicate timestamp for {b.symbol}: {b.timestamp.isoformat()}")
        if prev_ts is not None and b.timestamp <= prev_ts:
            raise ValueError(f"Bars for {b.symbol} must be strictly ascending in time")
        if min(b.open, b.high, b.low, b.close) <= 0:
            raise ValueError(f"Prices for {b.symbol} must be strictly positive")
        if b.high < max(b.open, b.close, b.low) or b.low > min(b.open, b.close, b.high):
            raise ValueError(f"Invalid OHLC bounds for {b.symbol} at {b.timestamp.isoformat()}")
        if b.volume < 0:
            raise ValueError(f"Negative volume rejected for {b.symbol}")
        seen_ts.add(b.timestamp)
        prev_ts = b.timestamp
    return ordered


def parse_daily_ohlcv_csv(
    csv_text: str,
    default_symbol: str | None = None,
    now: datetime | None = None,
) -> dict[str, tuple[MarketBar, ...]]:
    """Parse daily OHLCV CSV with columns (symbol optional): date/timestamp,open,high,low,close,volume."""
    cleaned = csv_text.lstrip("\ufeff").strip()
    if not cleaned:
        raise ValueError("CSV input is empty")
    reader = csv.DictReader(io.StringIO(cleaned))
    if not reader.fieldnames:
        raise ValueError("CSV must contain a header row")
    headers = {h.strip().lower(): h for h in reader.fieldnames}
    ts_col = headers.get("timestamp") or headers.get("date")
    if ts_col is None:
        raise ValueError("CSV must contain a 'date' or 'timestamp' column")
    for req in ("open", "high", "low", "close", "volume"):
        if req not in headers:
            raise ValueError(f"CSV missing required column '{req}'")

    grouped: dict[str, list[MarketBar]] = {}
    for row in reader:
        sym = (row[headers["symbol"]].strip().upper() if "symbol" in headers and row[headers["symbol"]] else default_symbol)
        if not sym:
            raise ValueError("Symbol column or default_symbol is required")
        bar = MarketBar(
            symbol=sym,
            timestamp=_parse_timestamp(row[ts_col]),
            open=_parse_decimal(row[headers["open"]], "open"),
            high=_parse_decimal(row[headers["high"]], "high"),
            low=_parse_decimal(row[headers["low"]], "low"),
            close=_parse_decimal(row[headers["close"]], "close"),
            volume=_parse_decimal(row[headers["volume"]], "volume"),
        )
        grouped.setdefault(sym, []).append(bar)

    return {sym: validate_bars(series, now=now) for sym, series in sorted(grouped.items())}


def parse_intraday_to_daily_bars(csv_text: str, symbol: str, now: datetime | None = None) -> tuple[MarketBar, ...]:
    """Reuse existing quant_engine.backtesting.data.parse_csv to aggregate 5-min bars into daily bars."""
    intraday = parse_intraday_csv(csv_text, now=now)
    by_day: dict[str, list] = {}
    for b in intraday:
        by_day.setdefault(b.day, []).append(b)
    daily: list[MarketBar] = []
    for day in sorted(by_day):
        session = by_day[day]
        daily.append(
            MarketBar(
                symbol=symbol.upper(),
                timestamp=session[-1].session_close,
                open=session[0].o,
                high=max(x.h for x in session),
                low=min(x.l for x in session),
                close=session[-1].c,
                volume=sum((x.volume for x in session), Decimal("0")),
            )
        )
    return validate_bars(daily, now=now)


class FeatureStore:
    """Strict point-in-time feature and market-data access layer."""

    def __init__(self, bundle: DatasetBundle) -> None:
        self.bundle = bundle

    @classmethod
    def from_bars(
        cls,
        bars_by_symbol: Mapping[str, Sequence[MarketBar]],
        *,
        kind: DatasetKind,
        source: str,
        texts_by_symbol: Mapping[str, Sequence[TextObservation]] | None = None,
        now: datetime | None = None,
    ) -> "FeatureStore":
        validated: dict[str, tuple[MarketBar, ...]] = {}
        for sym, series in sorted(bars_by_symbol.items()):
            validated[sym.upper()] = validate_bars(series, now=now)

        symbols = tuple(sorted(validated))
        if not symbols:
            raise ValueError("FeatureStore requires at least one symbol")

        # Common aligned calendar across all symbols (intersection prevents ragged look-ahead).
        date_sets = [{b.day for b in validated[s]} for s in symbols]
        common_dates = tuple(sorted(set.intersection(*date_sets)))
        if len(common_dates) < 10:
            raise ValueError("Aligned dataset must contain at least 10 common trading sessions")

        aligned_bars: dict[str, tuple[MarketBar, ...]] = {}
        common_set = set(common_dates)
        for s in symbols:
            aligned_bars[s] = tuple(b for b in validated[s] if b.day in common_set)

        aligned_texts: dict[str, tuple[TextObservation, ...]] = {}
        if texts_by_symbol:
            for sym, docs in texts_by_symbol.items():
                ordered_docs = tuple(sorted(docs, key=lambda d: d.published_at))
                for doc in ordered_docs:
                    if doc.published_at.tzinfo is None or doc.published_at.utcoffset() is None:
                        raise ValueError("TextObservation published_at must be timezone-aware")
                    if now is not None and doc.published_at > now:
                        raise ValueError("Future text observation rejected")
                aligned_texts[sym.upper()] = ordered_docs

        warnings: list[str] = []
        if kind == "synthetic":
            warnings.append("SYNTHETIC DATASET: deterministic simulated data; cannot qualify as live or historical proof.")

        fingerprint = canonical_hash(
            {
                "kind": kind,
                "source": source,
                "symbols": symbols,
                "bars": {
                    s: [
                        (b.timestamp.isoformat(), str(b.open), str(b.high), str(b.low), str(b.close), str(b.volume))
                        for b in aligned_bars[s]
                    ]
                    for s in symbols
                },
                "texts": {
                    s: [(d.published_at.isoformat(), d.text) for d in aligned_texts.get(s, ())]
                    for s in symbols
                },
            }
        )
        bundle = DatasetBundle(
            dataset_id=fingerprint,
            kind=kind,
            source=source,
            symbols=symbols,
            bars_by_symbol=aligned_bars,
            texts_by_symbol=aligned_texts,
            dates=common_dates,
            warnings=tuple(warnings),
        )
        return cls(bundle)

    @property
    def symbols(self) -> tuple[str, ...]:
        return self.bundle.symbols

    @property
    def dates(self) -> tuple[str, ...]:
        return self.bundle.dates

    def bars(self, symbol: str, as_of: datetime | None = None) -> tuple[MarketBar, ...]:
        """Return bars for `symbol` strictly at or before `as_of`."""
        series = self.bundle.bars_by_symbol[symbol.upper()]
        if as_of is None:
            return series
        return tuple(b for b in series if b.timestamp <= as_of)

    def texts(self, symbol: str, as_of: datetime | None = None) -> tuple[TextObservation, ...]:
        """Return text observations for `symbol` strictly at or before `as_of`."""
        docs = self.bundle.texts_by_symbol.get(symbol.upper(), ())
        if as_of is None:
            return docs
        return tuple(d for d in docs if d.published_at <= as_of)

    def slice_by_dates(self, start_date: str | None = None, end_date: str | None = None) -> "FeatureStore":
        """Create a causal sub-store restricted to [start_date, end_date] inclusive."""
        bars_sub: dict[str, list[MarketBar]] = {}
        texts_sub: dict[str, list[TextObservation]] = {}
        for s in self.symbols:
            selected = [
                b
                for b in self.bundle.bars_by_symbol[s]
                if (start_date is None or b.day >= start_date) and (end_date is None or b.day <= end_date)
            ]
            bars_sub[s] = selected
            if selected:
                min_ts = selected[0].timestamp - timedelta(days=1)
                max_ts = selected[-1].timestamp
                texts_sub[s] = [
                    d
                    for d in self.bundle.texts_by_symbol.get(s, ())
                    if min_ts <= d.published_at <= max_ts
                ]
        return FeatureStore.from_bars(
            bars_sub,
            kind=self.bundle.kind,
            source=self.bundle.source,
            texts_by_symbol=texts_sub,
        )

    def slice_prefix(self, count: int) -> "FeatureStore":
        """Return strictly the first `count` aligned trading sessions (for causality audits)."""
        if count < 10 or count > len(self.dates):
            raise ValueError(f"Prefix count must be in [10, {len(self.dates)}], got {count}")
        return self.slice_by_dates(start_date=self.dates[0], end_date=self.dates[count - 1])


def generate_synthetic_us_equity_dataset(
    symbols: Sequence[str] = ("SPY", "QQQ", "IWM", "XLF", "XLK", "XLE"),
    n_days: int = 260,
    seed: int = 42,
    start_day: date = date(2024, 1, 2),
) -> FeatureStore:
    """Deterministic multi-regime synthetic US equity/ETF bars + headlines for tests and demos.

    Explicitly tagged kind='synthetic'; never accepted as historical evidence for paper promotion.
    """
    rng = random.Random(seed)
    trading_days: list[date] = []
    cur = start_day
    while len(trading_days) < n_days:
        if cur.weekday() < 5:
            trading_days.append(cur)
        cur += timedelta(days=1)

    # Multi-regime latent market factor so regime breakdown and correlation tests have rich structure.
    market_rets: list[float] = []
    regime_cycle = [
        (0.0009, 0.007),   # bull
        (0.0018, 0.009),   # strong_bull
        (0.0000, 0.008),   # chop
        (-0.0012, 0.011),  # bear
        (-0.0004, 0.024),  # high_volatility
        (0.0011, 0.008),   # recovery bull
    ]
    segment_len = max(1, n_days // len(regime_cycle))
    prev_m = 0.0
    for idx in range(n_days):
        mu, sig = regime_cycle[min(len(regime_cycle) - 1, idx // segment_len)]
        # Mild persistence + noise
        shock = rng.gauss(mu, sig)
        r_m = 0.15 * prev_m + shock
        market_rets.append(max(-0.08, min(0.08, r_m)))
        prev_m = r_m

    bullish_phrases = (
        "strong earnings beat guidance raised momentum inflow upgrade",
        "institutional accumulation breakout revenue growth optimistic outlook",
        "margin expansion buyback rally demand surge outperform",
        "solid payroll resilience productivity expansion bullish sentiment",
    )
    bearish_phrases = (
        "earnings miss guidance cut margin compression outflow downgrade",
        "weak demand slowdown inventory glut recession warning underperform",
        "volatility spike credit stress selloff contraction cautious outlook",
        "cost inflation headwind revenue decline bearish sentiment",
    )
    neutral_phrases = (
        "steady trading range balanced volume unchanged sector rotation",
        "market awaits macro release quiet session mixed flows",
    )

    bars_by_symbol: dict[str, list[MarketBar]] = {}
    texts_by_symbol: dict[str, list[TextObservation]] = {}
    base_prices = {"SPY": 470.0, "QQQ": 405.0, "IWM": 198.0, "XLF": 38.0, "XLK": 192.0, "XLE": 84.0}
    betas = {"SPY": 1.0, "QQQ": 1.25, "IWM": 1.15, "XLF": 0.92, "XLK": 1.28, "XLE": 0.65}

    for sym_idx, raw_sym in enumerate(symbols):
        sym = raw_sym.upper()
        price = base_prices.get(sym, 100.0 + sym_idx * 25.0)
        beta = betas.get(sym, 0.8 + 0.12 * (sym_idx % 5))
        sym_bars: list[MarketBar] = []
        sym_texts: list[TextObservation] = []
        idio_prev = 0.0

        for i, d in enumerate(trading_days):
            ts = datetime(d.year, d.month, d.day, 21, 0, tzinfo=timezone.utc)
            # Mild idiosyncratic mean-reversion plus market beta
            idio = -0.22 * idio_prev + rng.gauss(0.0, 0.0085)
            ret = max(-0.12, min(0.12, beta * market_rets[i] + idio))
            idio_prev = idio

            open_p = price * (1.0 + rng.gauss(0.0, 0.002))
            close_p = max(1.0, price * (1.0 + ret))
            hi = max(open_p, close_p) * (1.0 + abs(rng.gauss(0.003, 0.002)))
            lo = max(0.5, min(open_p, close_p) * (1.0 - abs(rng.gauss(0.003, 0.002))))
            vol = int(2_000_000 * (1.0 + abs(ret) * 40.0 + rng.random() * 0.5))

            sym_bars.append(
                MarketBar(
                    symbol=sym,
                    timestamp=ts,
                    open=q2(dec_from_float(open_p, "open")),
                    high=q2(dec_from_float(hi, "high")),
                    low=q2(dec_from_float(lo, "low")),
                    close=q2(dec_from_float(close_p, "close")),
                    volume=Decimal(vol),
                )
            )
            price = close_p

            # Strictly causal headline published at 15:30 ET (20:30 UTC), reflecting realized/contemporary tone
            # with noisy signal so Bernoulli NB can be tested without any future leakage.
            pub_ts = datetime(d.year, d.month, d.day, 20, 30, tzinfo=timezone.utc)
            signal_tone = ret + rng.gauss(0.0, 0.009)
            if signal_tone > 0.004:
                headline = f"{sym} {rng.choice(bullish_phrases)}"
            elif signal_tone < -0.004:
                headline = f"{sym} {rng.choice(bearish_phrases)}"
            else:
                headline = f"{sym} {rng.choice(neutral_phrases)}"
            sym_texts.append(TextObservation(symbol=sym, published_at=pub_ts, text=headline, source="synthetic_news"))

        bars_by_symbol[sym] = sym_bars
        texts_by_symbol[sym] = sym_texts

    return FeatureStore.from_bars(
        bars_by_symbol,
        kind="synthetic",
        source=f"Deterministic synthetic US equity/ETF research fixture (seed={seed}); not market evidence",
        texts_by_symbol=texts_by_symbol,
    )
