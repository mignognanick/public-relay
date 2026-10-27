"""Plan and ingest the market data the backtest needs.

The data comes from the Robinhood MCP (`robinhood-trading`), which only an agent or MCP client can call,
so this script does not talk to the network. It prints the exact tool calls still missing, and it turns
the saved tool responses in `research/data/raw/` into the normalized files `backtest.py` reads.

    python research/fetch_data.py plan-lookups   # get_option_instruments calls still needed
    python research/fetch_data.py plan-bars      # get_option_historicals calls still needed
    python research/fetch_data.py ingest         # raw/* -> underlying.json, instruments.json, option_bars.json

Underlying bars: get_equity_historicals (SPY, QQQ, IWM) and get_index_historicals (SPX, one week per call),
5-minute, regular hours. Option bars: 5-minute marks (10-minute before FIVE_MINUTE_OPTIONS_FROM) for a
handful of near-the-money 0DTE contracts per session, used to calibrate an implied-volatility smile
(see `pricing.py`).
"""

from __future__ import annotations

import base64
import json
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
RAW = DATA / "raw"
ET = ZoneInfo("America/New_York")

WARMUP_SESSIONS = ("2026-09-24", "2026-09-25")
OPTION_UNDERLYINGS = ("SPY", "QQQ", "IWM")
CALIBRATION_STEP = {"SPY": 3, "QQQ": 3, "IWM": 1}
LOOKUP_PAGE_ROWS = 100
BARS_PER_CALL = 10
SPX_INDEX_ID = "432fbbb8-b82c-454a-852d-eb85382c7066"
INTERVAL_MINUTES = {"5minute": 5, "10minute": 10}
# Robinhood keeps real 5-minute option marks for about a week; older sessions only have real 10-minute bars
# (the 5-minute series comes back as interpolated gap-fill).
FIVE_MINUTE_OPTIONS_FROM = "2026-10-19"


def _load(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def _rth(ts: str) -> datetime | None:
    """Bar start in ET, or None when the bar starts outside 09:30-15:55."""
    start = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(ET)
    if time(9, 30) <= start.time() <= time(15, 55):
        return start
    return None


def ingest() -> None:
    underlying: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    instruments: dict[str, dict] = {}
    option_bars: dict[str, dict] = {}
    for path in sorted(RAW.iterdir()):
        doc = _load(path)
        if not doc or "data" not in doc:
            continue
        data = doc["data"]
        for inst in data.get("instruments", []):
            instruments[inst["id"]] = {
                "symbol": inst["chain_symbol"], "expiry": inst["expiration_date"],
                "type": inst["type"], "strike": float(inst["strike_price"]),
            }
        for res in data.get("results", []):
            bars = res.get("bars", [])
            if "occ_symbol" in res:
                # Keyed by bar end so 5- and 10-minute marks line up with the underlying 5-minute closes.
                width = timedelta(minutes=INTERVAL_MINUTES[res["interval"]])
                kept = {}
                for b in bars:
                    start = _rth(b["begins_at"])
                    if start is None or b.get("interpolated"):
                        continue
                    kept[(start + width).isoformat()] = [float(b["open_price"]), float(b["high_price"]),
                                                         float(b["low_price"]), float(b["close_price"])]
                entry = option_bars.setdefault(res["instrument_id"], {"occ": res["occ_symbol"].replace(" ", ""),
                                                                      "bars": {}})
                entry["bars"].update(kept)
                continue
            symbol = res.get("symbol")
            for b in bars:
                start = _rth(b["begins_at"])
                if start is None or b.get("interpolated"):
                    continue
                if "open_value" in b:
                    ohlc = [b["open_value"], b["high_value"], b["low_value"], b["close_value"]]
                    volume = 0
                else:
                    ohlc = [b["open_price"], b["high_price"], b["low_price"], b["close_price"]]
                    volume = int(b.get("volume") or 0)
                underlying[symbol][start.date().isoformat()].append(
                    [start.isoformat(), *map(float, ohlc), volume])
    clean = {}
    for symbol, days in underlying.items():
        clean[symbol] = {}
        for day, rows in sorted(days.items()):
            unique = {r[0]: r for r in rows}
            clean[symbol][day] = [unique[k] for k in sorted(unique)]
    DATA.mkdir(exist_ok=True)
    (DATA / "underlying.json").write_text(json.dumps(clean))
    (DATA / "instruments.json").write_text(json.dumps(instruments))
    for meta_id, entry in option_bars.items():
        entry.update(instruments.get(meta_id, {}))
    (DATA / "option_bars.json").write_text(json.dumps(option_bars))
    print(json.dumps({
        "underlying": {s: len(d) for s, d in clean.items()},
        "bars_per_session": {s: sorted({len(v) for v in d.values()}) for s, d in clean.items()},
        "instruments": len(instruments),
        "option_contracts_with_bars": sum(1 for e in option_bars.values() if e["bars"]),
    }, indent=1))


def sessions() -> list[str]:
    days = json.loads((DATA / "underlying.json").read_text())["SPY"]
    return [d for d in sorted(days) if d not in WARMUP_SESSIONS]


def calibration_strikes(symbol: str, day: str) -> list[tuple[str, float]]:
    """Five near-the-money contracts: puts below the session midpoint, calls at and above it."""
    bars = json.loads((DATA / "underlying.json").read_text())[symbol][day]
    mid = (max(b[2] for b in bars) + min(b[3] for b in bars)) / 2
    k0, s = round(mid), CALIBRATION_STEP[symbol]
    return [("put", k0 - 2 * s), ("put", k0 - s), ("call", k0), ("call", k0 + s), ("call", k0 + 2 * s)]


def plan_lookups() -> list[dict]:
    have = {(i["symbol"], i["expiry"], i["type"], i["strike"])
            for i in json.loads((DATA / "instruments.json").read_text()).values()}
    calls = []
    for symbol in OPTION_UNDERLYINGS:
        missing = []
        for day in sessions():
            wanted = calibration_strikes(symbol, day)
            if any((symbol, day, t, float(k)) not in have for t, k in wanted):
                missing.append((day, min(k for _, k in wanted), max(k for _, k in wanted)))
        group: list[tuple[str, int, int]] = []

        def flush() -> None:
            if group:
                lo = min(g[1] for g in group)
                cursor = base64.b64encode(f"p={lo - 1:.4f}".encode()).decode()
                calls.append({"chain_symbol": symbol, "state": "expired", "cursor": cursor,
                              "expiration_dates": ",".join(g[0] for g in group)})

        for item in missing:
            trial = group + [item]
            span = max(g[2] for g in trial) - min(g[1] for g in trial) + 1
            if span * 2 * len(trial) > LOOKUP_PAGE_ROWS:
                flush()
                group = [item]
            else:
                group = trial
        flush()
    return calls


def plan_bars() -> list[dict]:
    instruments = json.loads((DATA / "instruments.json").read_text())
    option_bars = json.loads((DATA / "option_bars.json").read_text())
    index = {(i["symbol"], i["expiry"], i["type"], i["strike"]): iid for iid, i in instruments.items()}

    def has_expiry_bars(iid: str, day: str) -> bool:
        return any(k[:10] == day for k in option_bars.get(iid, {}).get("bars", {}))

    pending: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for symbol in OPTION_UNDERLYINGS:
        for day in sessions():
            for t, k in calibration_strikes(symbol, day):
                iid = index.get((symbol, day, t, float(k)))
                if iid and not has_expiry_bars(iid, day):
                    interval = "5minute" if day >= FIVE_MINUTE_OPTIONS_FROM else "10minute"
                    pending[interval].append((day, iid))
    calls = []
    for interval, items in pending.items():
        items.sort()
        while items:
            batch, items = items[:BARS_PER_CALL], items[BARS_PER_CALL:]
            first, last = batch[0][0], batch[-1][0]
            start = datetime.fromisoformat(first).replace(hour=9, minute=30, tzinfo=ET).astimezone(timezone.utc)
            end = (datetime.fromisoformat(last).replace(hour=16, tzinfo=ET)
                   + timedelta(minutes=5)).astimezone(timezone.utc)
            calls.append({"instrument_ids": [iid for _, iid in batch], "interval": interval,
                          "start_time": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                          "end_time": end.strftime("%Y-%m-%dT%H:%M:%SZ")})
    return calls


def main(argv: list[str]) -> None:
    match argv[1:]:
        case ["ingest"]:
            ingest()
        case ["plan-lookups"]:
            print(json.dumps(plan_lookups(), indent=1))
        case ["plan-bars"]:
            print(json.dumps(plan_bars(), indent=1))
        case _:
            sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv)
