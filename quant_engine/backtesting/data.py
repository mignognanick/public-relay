"""Strict five-minute regular-session data, with explicit provenance."""
import csv
import hashlib
import io
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
MAX_BARS = 25000
MAX_CSV_BYTES = 3000000
HEADERS = ['timestamp', 'open', 'high', 'low', 'close', 'volume', 'session_close']


@dataclass(frozen=True)
class Bar:
    start: datetime
    o: Decimal
    h: Decimal
    l: Decimal
    c: Decimal
    volume: Decimal
    session_close: datetime

    @property
    def day(self):
        return self.start.astimezone(ET).date().isoformat()


def number(value, positive=True):
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        raise ValueError('Prices and volume must be decimal numbers') from None
    if not result.is_finite() or result < 0 or (positive and result == 0) or result > Decimal('1000000000000'):
        raise ValueError('Invalid price or volume')
    if result.as_tuple().exponent < -8:
        raise ValueError('At most eight decimal places are supported')
    return result


def timestamp(value):
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, AttributeError):
        raise ValueError('Timestamps must be ISO 8601 with an explicit timezone') from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError('Timestamps require an explicit timezone')
    return dt.astimezone(ET)


def parse_csv(text, now=None):
    if len(text.encode('utf-8')) > MAX_CSV_BYTES:
        raise ValueError('CSV exceeds the 3 MB limit; import a smaller period')
    reader = csv.DictReader(io.StringIO(text.lstrip('\ufeff')))
    if reader.fieldnames != HEADERS:
        raise ValueError('CSV columns must be: ' + ','.join(HEADERS))
    bars = []
    now = now or datetime.now(timezone.utc)
    for row in reader:
        if len(bars) >= MAX_BARS:
            raise ValueError('At most 25,000 bars per dataset')
        if None in row or any(v is None for v in row.values()):
            raise ValueError('CSV row has missing or extra columns')
        start, close = timestamp(row['timestamp']), timestamp(row['session_close'])
        if start.weekday() > 4 or start.second or start.microsecond or start.minute % 5:
            raise ValueError('Bars must start on weekday five-minute boundaries')
        if close.date() != start.date() or close.time() not in (time(13), time(16)):
            raise ValueError('session_close must be 13:00 or 16:00 New York on the same date')
        if not time(9, 30) <= start.time() < close.time():
            raise ValueError('Only regular-session bars are accepted')
        if start + timedelta(minutes=5) > now:
            raise ValueError('Future or unfinished bars are not accepted')
        o, h, l, c = [number(row[k]) for k in ('open', 'high', 'low', 'close')]
        volume = number(row['volume'], positive=False)
        if h < max(o, c, l) or l > min(o, c, h):
            raise ValueError('OHLC prices are inconsistent')
        if bars and start <= bars[-1].start:
            raise ValueError('Bars must be chronological, unique and sorted')
        bars.append(Bar(start, o, h, l, c, volume, close))
    sessions = {}
    for bar in bars:
        sessions.setdefault(bar.day, []).append(bar)
    if len(sessions) < 4:
        raise ValueError('At least four complete sessions are required')
    for day in sessions.values():
        first = day[0].start.replace(hour=9, minute=30)
        end = day[0].session_close
        expected = int((end - first).total_seconds() // 300)
        if len(day) != expected or any(b.start != first + timedelta(minutes=5*i) or b.session_close != end for i, b in enumerate(day)):
            raise ValueError('Missing bars or inconsistent session calendar on ' + day[0].day + '; no gaps are filled silently')
        if sum(b.volume for b in day) == 0:
            raise ValueError('A session has no volume; VWAP cannot be computed')
    return bars


def dataset_payload(text, symbol, source, kind='historical', calendar_verified=False):
    if symbol not in ('SPY', 'QQQ') or kind not in ('historical', 'synthetic'):
        raise ValueError('Supported symbols are SPY/QQQ and kinds historical/synthetic')
    if not source.strip() or len(source) > 300:
        raise ValueError('A source description of at most 300 characters is required')
    bars = parse_csv(text)
    normalized = csv_text(bars)
    content_hash = hashlib.sha256(normalized.encode()).hexdigest()
    meta = {'symbol': symbol, 'source': source.strip(), 'kind': kind, 'sha256': content_hash,
            'bars': len(bars), 'sessions': len({b.day for b in bars}), 'start': bars[0].day,
            'end': bars[-1].day, 'calendar_verified': calendar_verified,
            'warnings': ([] if calendar_verified else ['Session closes are user supplied; omitted whole sessions/holidays are not independently verified.']) +
                        ['Underlying shares only. No options prices or fills.']}
    identity = hashlib.sha256(json.dumps(meta, sort_keys=True).encode()).hexdigest()
    return identity, {'meta': meta, 'csv': normalized}


def csv_text(bars):
    stream = io.StringIO()
    writer = csv.writer(stream, lineterminator='\n')
    writer.writerow(HEADERS)
    for b in bars:
        writer.writerow([b.start.isoformat(), b.o, b.h, b.l, b.c, b.volume, b.session_close.isoformat()])
    return stream.getvalue()


def demo_csv():
    """Deterministic manufactured prices for UI/engine checks, never evidence."""
    bars = []
    day = datetime(2024, 2, 5, 9, 30, tzinfo=ET)
    for n in range(12):
        while day.weekday() > 4:
            day += timedelta(days=1)
        price = Decimal('450') + n
        for i in range(78):
            move = Decimal('0.10') * (1 if n % 2 == 0 else -1)
            if i % 9 == 7:
                move *= -3
            close = price + move
            bars.append(Bar(day + timedelta(minutes=i*5), price, max(price, close)+Decimal('.04'),
                            min(price, close)-Decimal('.04'), close, Decimal(10000+i), day.replace(hour=16, minute=0)))
            price = close
        day += timedelta(days=1)
    return csv_text(bars)
