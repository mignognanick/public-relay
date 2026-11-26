"""Bounded GET-only historical import. No account/order endpoints or subscriptions."""
import csv
import io
from datetime import datetime, time, timedelta

import httpx

from quant_engine.backtesting.data import ET, HEADERS, MAX_BARS, dataset_payload, timestamp
from quant_engine.brokers.readonly import GetOnlyClient


class DataUnavailable(ValueError):
    pass


def import_alpaca(request, config, client=None):
    today = datetime.now(ET).date()
    if request.start >= request.end or request.end >= today or (request.end-request.start).days > 180:
        raise ValueError('Choose 2–180 calendar days ending before today')
    key, secret = config.get('ALPACA_PAPER_KEY_ID'), config.get('ALPACA_PAPER_SECRET_KEY')
    if not key or not secret:
        raise DataUnavailable('Alpaca paper data access is not configured. Import a historical CSV instead.')
    headers = {'APCA-API-KEY-ID': key, 'APCA-API-SECRET-KEY': secret}
    owned = client is None
    http = client or httpx.Client(timeout=10, follow_redirects=False)
    market = GetOnlyClient(http, 'https://data.alpaca.markets')
    calendar_client = GetOnlyClient(http, 'https://paper-api.alpaca.markets')
    try:
        response = calendar_client.get('/v2/calendar', headers=headers, params={'start':str(request.start),'end':str(request.end)})
        if response.status_code != 200:
            raise DataUnavailable('Historical calendar unavailable; no dataset imported.')
        calendar = response.json()
        sessions = {}
        for row in calendar:
            if row['open'] != '09:30' or row['close'] not in ('13:00', '16:00'):
                raise DataUnavailable('Unsupported session schedule')
            sessions[row['date']] = datetime.fromisoformat(row['date']+'T'+row['close']).replace(tzinfo=ET)
        bars, seen_tokens, next_token = [], set(), None
        start = datetime.combine(request.start, time(0), ET).isoformat()
        end = datetime.combine(request.end+timedelta(days=1), time(0), ET).isoformat()
        for _ in range(12):
            params = {'symbols':request.symbol,'timeframe':'5Min','start':start,'end':end,
                      'feed':'iex','adjustment':'raw','sort':'asc','limit':10000}
            if next_token:
                params['page_token'] = next_token
            response = market.get('/v2/stocks/bars', headers=headers, params=params)
            if response.status_code != 200:
                raise DataUnavailable('Historical IEX data unavailable or rate limited. Check existing data access or import CSV; no paid feed is requested.')
            payload = response.json()
            for row in (payload.get('bars') or {}).get(request.symbol, []):
                stamp = timestamp(row['t'])
                session_close = sessions.get(stamp.date().isoformat())
                if session_close and time(9,30) <= stamp.time() < session_close.time():
                    bars.append([stamp.isoformat(), *[str(row[k]) for k in ('o','h','l','c','v')], session_close.isoformat()])
                    if len(bars) > MAX_BARS:
                        raise DataUnavailable('Too many bars; choose a shorter date range')
            next_token = payload.get('next_page_token')
            if not next_token:
                break
            if next_token in seen_tokens:
                raise DataUnavailable('Historical pagination repeated; import aborted')
            seen_tokens.add(next_token)
        else:
            raise DataUnavailable('Historical pagination limit reached; choose a shorter range')
        if set(sessions) != {r[0][:10] for r in bars}:
            raise DataUnavailable('Entire trading sessions are missing from the feed; import aborted')
        stream = io.StringIO()
        writer = csv.writer(stream, lineterminator='\n')
        writer.writerow(HEADERS)
        writer.writerows(bars)
        return dataset_payload(stream.getvalue(), request.symbol,
                               'Alpaca historical IEX, raw prices; provider calendar. IEX volume is not consolidated market volume.',
                               calendar_verified=True)
    except (httpx.HTTPError, KeyError, TypeError):
        raise DataUnavailable('Historical data request failed; no dataset imported') from None
    finally:
        if owned:
            http.close()
