"""0DTE option marks from an implied-volatility smile calibrated to real Robinhood marks.

For each session, five near-the-money contracts per underlying have real 5- or 10-minute marks
(`fetch_data.py`). At every timestamp with marks, the implied vol of each contract is solved and laid
out against log-moneyness ln(K/S); any strike is then priced off that smile, interpolated linearly in
moneyness and in time. XSP and SPX have no fetched chains, so they use the SPY smile at their own spot.
"""

from __future__ import annotations

import bisect
import json
import math
from dataclasses import dataclass
from datetime import datetime, time
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
CLOSE = time(16, 0)
YEAR_MINUTES = 252 * 390
MIN_MARK = 0.05
MIN_TIME_VALUE = 0.03
SMILE_SOURCE = {"SPY": "SPY", "QQQ": "QQQ", "IWM": "IWM", "XSP": "SPY", "SPX": "SPY"}


def norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def years_to_close(ts: datetime) -> float:
    minutes = (CLOSE.hour * 60 + CLOSE.minute) - (ts.hour * 60 + ts.minute)
    return max(minutes, 1) / YEAR_MINUTES


def bs_price(spot: float, strike: float, t: float, vol: float, right: str) -> float:
    if t <= 0 or vol <= 0:
        return max(0.0, spot - strike) if right == "call" else max(0.0, strike - spot)
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    if right == "call":
        return spot * norm_cdf(d1) - strike * norm_cdf(d2)
    return strike * norm_cdf(-d2) - spot * norm_cdf(-d1)


def bs_delta(spot: float, strike: float, t: float, vol: float, right: str) -> float:
    sd = vol * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    return norm_cdf(d1) if right == "call" else norm_cdf(d1) - 1.0


def implied_vol(price: float, spot: float, strike: float, t: float, right: str) -> float | None:
    intrinsic = max(0.0, spot - strike) if right == "call" else max(0.0, strike - spot)
    if price < MIN_MARK or price - intrinsic < MIN_TIME_VALUE:
        return None
    lo, hi = 0.01, 5.0
    if bs_price(spot, strike, t, hi, right) < price:
        return None
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if bs_price(spot, strike, t, mid, right) < price:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


@dataclass(frozen=True)
class Smile:
    ks: tuple[float, ...]
    vols: tuple[float, ...]

    def vol(self, k: float) -> float:
        if k <= self.ks[0]:
            return self.vols[0]
        if k >= self.ks[-1]:
            return self.vols[-1]
        i = bisect.bisect_right(self.ks, k)
        k0, k1 = self.ks[i - 1], self.ks[i]
        w = (k - k0) / (k1 - k0)
        return self.vols[i - 1] * (1 - w) + self.vols[i] * w


def load_json(name: str):
    return json.loads((DATA / name).read_text())


@lru_cache(maxsize=None)
def underlying() -> dict:
    return load_json("underlying.json")


@lru_cache(maxsize=None)
def spot_series(symbol: str, day: str) -> dict[str, float]:
    """Underlying close keyed by bar end (ISO, ET). XSP is SPX / 10."""
    source = "SPX" if symbol in ("XSP", "SPX") else symbol
    scale = 0.1 if symbol == "XSP" else 1.0
    return {_bar_end(start): c * scale for start, _o, _h, _l, c, _v in underlying()[source][day]}


def _bar_end(start_iso: str) -> str:
    start = datetime.fromisoformat(start_iso)
    minute = start.hour * 60 + start.minute + 5
    return start.replace(hour=minute // 60, minute=minute % 60).isoformat()


@lru_cache(maxsize=None)
def calibration_points(symbol: str, day: str) -> dict[str, list[tuple[float, float, float, str]]]:
    """bar end -> [(strike, mark, spot, right)] for the real contracts expiring that day."""
    spots = spot_series(symbol, day)
    out: dict[str, list] = {}
    for entry in option_bars().values():
        if entry.get("symbol") != symbol or entry.get("expiry") != day:
            continue
        for end, (_o, _h, _l, c) in entry["bars"].items():
            if end[:10] != day or end not in spots:
                continue
            out.setdefault(end, []).append((entry["strike"], c, spots[end], entry["type"]))
    return out


@lru_cache(maxsize=None)
def option_bars() -> dict:
    return load_json("option_bars.json")


def smile_from_points(points: list[tuple[float, float, float, str]], ts: datetime,
                      skip: int | None = None) -> Smile | None:
    t = years_to_close(ts)
    pairs = []
    for i, (strike, mark, spot, right) in enumerate(points):
        if i == skip:
            continue
        iv = implied_vol(mark, spot, strike, t, right)
        if iv is not None:
            pairs.append((math.log(strike / spot), iv))
    if not pairs:
        return None
    pairs.sort()
    merged: dict[float, list[float]] = {}
    for k, v in pairs:
        merged.setdefault(round(k, 6), []).append(v)
    ks = tuple(sorted(merged))
    return Smile(ks, tuple(sum(merged[k]) / len(merged[k]) for k in ks))


@lru_cache(maxsize=None)
def smiles(symbol: str, day: str) -> tuple[tuple[datetime, Smile], ...]:
    source = SMILE_SOURCE[symbol]
    out = []
    for end, points in sorted(calibration_points(source, day).items()):
        ts = datetime.fromisoformat(end)
        smile = smile_from_points(points, ts)
        if smile is not None:
            out.append((ts, smile))
    return tuple(out)


def vol_at(symbol: str, day: str, ts: datetime, k: float) -> float:
    series = smiles(symbol, day)
    if not series:
        raise ValueError(f"no smile for {symbol} {day}")
    times = [s[0] for s in series]
    i = bisect.bisect_left(times, ts)
    if i < len(series) and times[i] == ts:
        return series[i][1].vol(k)
    if i == 0:
        return series[0][1].vol(k)
    if i == len(series):
        return series[-1][1].vol(k)
    (t0, s0), (t1, s1) = series[i - 1], series[i]
    w = (ts - t0).total_seconds() / (t1 - t0).total_seconds()
    return s0.vol(k) * (1 - w) + s1.vol(k) * w


def mark(symbol: str, day: str, ts: datetime, spot: float, strike: float, right: str) -> float:
    vol = vol_at(symbol, day, ts, math.log(strike / spot))
    return bs_price(spot, strike, years_to_close(ts), vol, right)


def delta(symbol: str, day: str, ts: datetime, spot: float, strike: float, right: str) -> float:
    vol = vol_at(symbol, day, ts, math.log(strike / spot))
    return bs_delta(spot, strike, years_to_close(ts), vol, right)


def validate(symbol: str) -> dict:
    """Leave-one-out: price each real mark from a smile built without it."""
    errors, rel = [], []
    for day in sorted(underlying()[symbol]):
        for end, points in calibration_points(symbol, day).items():
            ts = datetime.fromisoformat(end)
            if ts.time() > time(15, 40):
                continue
            for i, (strike, real, spot, right) in enumerate(points):
                if real < 0.30:
                    continue
                smile = smile_from_points(points, ts, skip=i)
                if smile is None or len(smile.ks) < 2:
                    continue
                model = bs_price(spot, strike, years_to_close(ts), smile.vol(math.log(strike / spot)), right)
                errors.append(abs(model - real))
                rel.append(abs(model - real) / real)
    errors.sort()
    rel.sort()
    if not errors:
        return {"symbol": symbol, "n": 0}
    return {"symbol": symbol, "n": len(errors),
            "median_abs_error": round(errors[len(errors) // 2], 3),
            "p90_abs_error": round(errors[int(len(errors) * 0.9)], 3),
            "median_pct_error": round(100 * rel[len(rel) // 2], 1)}


if __name__ == "__main__":
    for sym in ("SPY", "QQQ", "IWM"):
        print(validate(sym))
