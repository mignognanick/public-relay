"""Intraday signal strategies on 5-minute underlying bars.

Every strategy returns, for each session, one target per bar evaluated at the bar close:
+1 (want a call), -1 (want a put), 0 (want flat) or None (no change). Targets are states, not
events, so a strategy that still wants a side after an exit can re-enter on a later bar. That matches
the Pine port, which closes on a reversal and enters the other side on the next bar, because the relay
flattens on an opposite signal and does not open the other side on the same alert.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Callable, Iterable

Target = int | None


@dataclass(frozen=True)
class Bar:
    day: str
    end: datetime
    o: float
    h: float
    l: float
    c: float
    v: float


@dataclass(frozen=True)
class Variant:
    family: str
    params: tuple[tuple[str, object], ...]

    @property
    def name(self) -> str:
        args = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.family}({args})"

    def param(self, key: str):
        return dict(self.params)[key]


def bars_from_rows(rows_by_day: dict[str, list]) -> dict[str, list[Bar]]:
    out = {}
    for day, rows in sorted(rows_by_day.items()):
        day_bars = []
        for start, o, h, l, c, v in rows:
            s = datetime.fromisoformat(start)
            minute = s.hour * 60 + s.minute + 5
            day_bars.append(Bar(day, s.replace(hour=minute // 60, minute=minute % 60), o, h, l, c, v))
        out[day] = day_bars
    return out


def _ema(values: Iterable[float], n: int) -> list[float]:
    alpha, out, prev = 2 / (n + 1), [], None
    for x in values:
        prev = x if prev is None else prev + alpha * (x - prev)
        out.append(prev)
    return out


def _rsi(values: list[float], n: int) -> list[float | None]:
    out: list[float | None] = [None]
    gain = loss = 0.0
    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        up, down = max(change, 0.0), max(-change, 0.0)
        if i <= n:
            gain += up / n
            loss += down / n
            out.append(None if i < n else _rsi_value(gain, loss))
            continue
        gain = (gain * (n - 1) + up) / n
        loss = (loss * (n - 1) + down) / n
        out.append(_rsi_value(gain, loss))
    return out


def _rsi_value(gain: float, loss: float) -> float:
    if loss == 0:
        return 100.0
    return 100 - 100 / (1 + gain / loss)


def _bollinger(values: list[float], n: int, k: float) -> list[tuple[float, float, float] | None]:
    out: list[tuple[float, float, float] | None] = []
    for i in range(len(values)):
        if i + 1 < n:
            out.append(None)
            continue
        window = values[i + 1 - n:i + 1]
        mean = sum(window) / n
        sd = (sum((x - mean) ** 2 for x in window) / n) ** 0.5
        out.append((mean - k * sd, mean, mean + k * sd))
    return out


def _vwap(day_bars: list[Bar]) -> list[float]:
    num = den = 0.0
    out = []
    for b in day_bars:
        typical = (b.h + b.l + b.c) / 3
        weight = b.v if b.v > 0 else 1.0
        num += typical * weight
        den += weight
        out.append(num / den)
    return out


def _flatten(bars: dict[str, list[Bar]]) -> list[Bar]:
    return [b for day in sorted(bars) for b in bars[day]]


def _split(seq: list, bars: dict[str, list[Bar]]) -> dict[str, list]:
    out, i = {}, 0
    for day in sorted(bars):
        out[day] = seq[i:i + len(bars[day])]
        i += len(bars[day])
    return out


def orb(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    minutes, exit_inside = v.param("minutes"), v.param("exit") == "inside"
    out = {}
    for day, day_bars in bars.items():
        n = minutes // 5
        hi = max(b.h for b in day_bars[:n])
        lo = min(b.l for b in day_bars[:n])
        targets: list[Target] = [None] * n
        for b in day_bars[n:]:
            if b.c > hi:
                targets.append(1)
            elif b.c < lo:
                targets.append(-1)
            else:
                targets.append(0 if exit_inside else None)
        out[day] = targets
    return out


def vwap_cross(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    confirm, band = v.param("confirm"), v.param("band")
    out = {}
    for day, day_bars in bars.items():
        vw = _vwap(day_bars)
        targets: list[Target] = []
        for i, b in enumerate(day_bars):
            if i < confirm:
                targets.append(None)
                continue
            recent = range(i - confirm + 1, i + 1)
            if all(day_bars[j].c > vw[j] * (1 + band) for j in recent):
                targets.append(1)
            elif all(day_bars[j].c < vw[j] * (1 - band) for j in recent):
                targets.append(-1)
            else:
                targets.append(None)
        out[day] = targets
    return out


def ema_cross(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    fast, slow, use_vwap = v.param("fast"), v.param("slow"), v.param("vwap_filter")
    flat = _flatten(bars)
    closes = [b.c for b in flat]
    f_by_day = _split(_ema(closes, fast), bars)
    s_by_day = _split(_ema(closes, slow), bars)
    out = {}
    for day, day_bars in bars.items():
        vw = _vwap(day_bars)
        targets: list[Target] = []
        for i, b in enumerate(day_bars):
            up = f_by_day[day][i] > s_by_day[day][i]
            if use_vwap:
                targets.append(1 if up and b.c > vw[i] else -1 if not up and b.c < vw[i] else 0)
            else:
                targets.append(1 if up else -1)
        out[day] = targets
    return out


def prior_day_break(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    exit_inside = v.param("exit") == "inside"
    days = sorted(bars)
    out = {}
    for prev, day in zip(days, days[1:]):
        pdh = max(b.h for b in bars[prev])
        pdl = min(b.l for b in bars[prev])
        out[day] = [1 if b.c > pdh else -1 if b.c < pdl else (0 if exit_inside else None) for b in bars[day]]
    out[days[0]] = [None] * len(bars[days[0]])
    return out


def rsi_fade(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    lo, hi = v.param("lo"), 100 - v.param("lo")
    flat = _flatten(bars)
    rsi_by_day = _split(_rsi([b.c for b in flat], 2), bars)
    out = {}
    for day, day_bars in bars.items():
        state, targets = 0, []
        for r in rsi_by_day[day]:
            if r is not None:
                if r < lo:
                    state = 1
                elif r > hi:
                    state = -1
                elif (state == 1 and r > 50) or (state == -1 and r < 50):
                    state = 0
            targets.append(state)
        out[day] = targets
    return out


def bollinger_fade(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    flat = _flatten(bars)
    bands_by_day = _split(_bollinger([b.c for b in flat], v.param("n"), v.param("k")), bars)
    out = {}
    for day, day_bars in bars.items():
        state, targets = 0, []
        for b, band in zip(day_bars, bands_by_day[day]):
            if band is not None:
                lower, mid, upper = band
                if b.c < lower:
                    state = 1
                elif b.c > upper:
                    state = -1
                elif (state == 1 and b.c >= mid) or (state == -1 and b.c <= mid):
                    state = 0
            targets.append(state)
        out[day] = targets
    return out


def gap(bars: dict[str, list[Bar]], v: Variant) -> dict[str, list[Target]]:
    threshold, mode = v.param("gap"), v.param("mode")
    hold_until = time(*map(int, v.param("hold_until").split(":")))
    days = sorted(bars)
    out = {days[0]: [None] * len(bars[days[0]])}
    for prev, day in zip(days, days[1:]):
        prev_close = bars[prev][-1].c
        first = bars[day][0]
        move = first.o / prev_close - 1
        direction = 0
        if abs(move) >= threshold:
            direction = (1 if move > 0 else -1) * (1 if mode == "go" else -1)
            confirmed = (first.c > first.o) == (direction > 0)
            if not confirmed:
                direction = 0
        out[day] = [direction if b.end.time() < hold_until else 0 for b in bars[day]]
    return out


FAMILIES: dict[str, Callable[[dict[str, list[Bar]], Variant], dict[str, list[Target]]]] = {
    "ORB": orb,
    "VWAP": vwap_cross,
    "EMA": ema_cross,
    "PDHL": prior_day_break,
    "RSI2": rsi_fade,
    "BB": bollinger_fade,
    "GAP": gap,
}


def variants() -> list[Variant]:
    out = []
    for minutes in (5, 15, 30):
        for exit_mode in ("opposite", "inside"):
            out.append(Variant("ORB", (("minutes", minutes), ("exit", exit_mode))))
    for confirm in (1, 2):
        for band in (0.0, 0.0005):
            out.append(Variant("VWAP", (("confirm", confirm), ("band", band))))
    for fast, slow in ((9, 21), (5, 13)):
        for use_vwap in (True, False):
            out.append(Variant("EMA", (("fast", fast), ("slow", slow), ("vwap_filter", use_vwap))))
    for exit_mode in ("opposite", "inside"):
        out.append(Variant("PDHL", (("exit", exit_mode),)))
    for lo in (10, 5):
        out.append(Variant("RSI2", (("lo", lo),)))
    out.append(Variant("BB", (("n", 20), ("k", 2.0))))
    for threshold in (0.002, 0.004):
        for mode in ("go", "fade"):
            for hold in ("10:30", "12:00"):
                out.append(Variant("GAP", (("gap", threshold), ("mode", mode), ("hold_until", hold))))
    return out


def targets_for(variant: Variant, bars: dict[str, list[Bar]]) -> dict[str, list[Target]]:
    return FAMILIES[variant.family](bars, variant)
