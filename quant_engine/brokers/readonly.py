"""Read-only Alpaca paper and Public.com snapshots. No order-submission paths."""
from urllib.parse import urlparse

import httpx

PAPER_ALPACA_URL = 'https://paper-api.alpaca.markets'
PUBLIC_API_URL = 'https://api.public.com'
PUBLIC_TOKEN_URL = PUBLIC_API_URL + '/userapiauthservice/personal/access-tokens'
TIMEOUT = httpx.Timeout(10.0)
DISCONNECTED = 'not connected'


class PaperOnlyError(ValueError):
    """Raised when Alpaca is pointed at any host other than the paper API."""


def paper_alpaca_base(url=PAPER_ALPACA_URL):
    parsed = urlparse((url or '').strip())
    normalized = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
    if normalized != PAPER_ALPACA_URL:
        raise PaperOnlyError('Alpaca access is limited to the paper API')
    return PAPER_ALPACA_URL


class GetOnlyClient:
    """httpx wrapper that can only issue GET requests to one allowed origin."""

    def __init__(self, client, allowed_base):
        self._client = client
        self._allowed_base = allowed_base.rstrip('/')

    def get(self, path, **kwargs):
        url = path if str(path).startswith('https://') else self._allowed_base + path
        parsed = urlparse(url)
        origin = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
        if origin != self._allowed_base:
            raise ValueError('Read-only client refused a request outside the allowed host')
        return self._client.get(url, **kwargs)


def _status(exc):
    if isinstance(exc, httpx.TimeoutException):
        return 'unavailable', 'Broker request timed out'
    if isinstance(exc, httpx.HTTPStatusError):
        return 'unavailable', 'Broker request was rejected'
    if isinstance(exc, httpx.HTTPError):
        return 'unavailable', 'Broker request failed'
    return 'unavailable', 'Broker snapshot is unavailable'


def _text(value):
    if value is None or value == '':
        return None
    return str(value)


def disconnected(broker_id, name):
    return {
        'id': broker_id,
        'name': name,
        'connected': False,
        'status': DISCONNECTED,
        'message': DISCONNECTED,
        'balances': None,
        'positions': [],
        'orders': [],
        'execution_active': False,
    }


def _transport(client):
    return client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)


def _raise_for_status(response):
    response.raise_for_status()
    return response


class AlpacaPaperClient:
    """GET-only client pinned to the Alpaca paper host."""

    def __init__(self, key_id, secret_key, client=None, base_url=PAPER_ALPACA_URL):
        self.base_url = paper_alpaca_base(base_url)
        self._key_id = key_id
        self._secret_key = secret_key
        self._http = GetOnlyClient(_transport(client), self.base_url)

    def _headers(self):
        return {
            'APCA-API-KEY-ID': self._key_id,
            'APCA-API-SECRET-KEY': self._secret_key,
        }

    def snapshot(self):
        headers = self._headers()
        account = _raise_for_status(self._http.get('/v2/account', headers=headers)).json()
        positions = _raise_for_status(self._http.get('/v2/positions', headers=headers)).json()
        orders = _raise_for_status(self._http.get(
            '/v2/orders',
            headers=headers,
            params={'status': 'all', 'limit': 50, 'direction': 'desc', 'nested': 'false'},
        )).json()
        return {
            'id': 'alpaca_paper',
            'name': 'Alpaca Paper',
            'connected': True,
            'status': 'connected',
            'message': 'Paper account · read only',
            'balances': {
                'equity': _text(account.get('equity')),
                'cash': _text(account.get('cash')),
                'buying_power': _text(account.get('buying_power')),
                'portfolio_value': _text(account.get('portfolio_value')),
                'currency': _text(account.get('currency')) or 'USD',
                'account_number': _text(account.get('account_number')),
            },
            'positions': [{
                'symbol': _text(row.get('symbol')),
                'qty': _text(row.get('qty')),
                'side': _text(row.get('side')),
                'market_value': _text(row.get('market_value')),
                'avg_entry_price': _text(row.get('avg_entry_price')),
                'current_price': _text(row.get('current_price')),
                'unrealized_pl': _text(row.get('unrealized_pl')),
            } for row in positions or []],
            'orders': [{
                'id': _text(row.get('id') or row.get('client_order_id')),
                'symbol': _text(row.get('symbol')),
                'side': _text(row.get('side')),
                'qty': _text(row.get('qty')),
                'filled_qty': _text(row.get('filled_qty')),
                'type': _text(row.get('type') or row.get('order_type')),
                'status': _text(row.get('status')),
                'submitted_at': _text(row.get('submitted_at')),
                'filled_at': _text(row.get('filled_at')),
            } for row in orders or []],
            'execution_active': False,
        }


class PublicReadonlyClient:
    """Public.com snapshots via official token exchange and GET portfolio/v2."""

    def __init__(self, secret, account_id, client=None, base_url=PUBLIC_API_URL):
        parsed = urlparse((base_url or '').strip())
        origin = f'{parsed.scheme}://{parsed.netloc}'.rstrip('/')
        if origin != PUBLIC_API_URL:
            raise ValueError('Public.com access is limited to the official API host')
        self.base_url = PUBLIC_API_URL
        self._secret = secret
        self._account_id = account_id
        raw = _transport(client)
        self._raw = raw
        self._http = GetOnlyClient(raw, self.base_url)

    def _access_token(self):
        response = self._raw.post(
            PUBLIC_TOKEN_URL,
            json={'validityInMinutes': 15, 'secret': self._secret},
            headers={'Content-Type': 'application/json'},
        )
        _raise_for_status(response)
        token = (response.json() or {}).get('accessToken')
        if not token:
            raise httpx.HTTPError('Access token missing')
        return token

    def snapshot(self):
        token = self._access_token()
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        portfolio = _raise_for_status(self._http.get(
            f'/userapigateway/trading/{self._account_id}/portfolio/v2',
            headers=headers,
        )).json() or {}
        buying = portfolio.get('buyingPower') or {}
        equity_rows = portfolio.get('equity') or []
        cash = next((row.get('value') for row in equity_rows if (row or {}).get('type') == 'CASH'), None)
        return {
            'id': 'public',
            'name': 'Public.com',
            'connected': True,
            'status': 'connected',
            'message': 'Read only · balances and positions',
            'balances': {
                'equity': _text(portfolio.get('totalAccountValue')),
                'cash': _text(portfolio.get('cash') if portfolio.get('cash') not in (None, '') else cash),
                'buying_power': _text(buying.get('buyingPower')),
                'cash_buying_power': _text(buying.get('cashOnlyBuyingPower')),
                'account_id': _text(portfolio.get('accountId') or self._account_id),
                'account_type': _text(portfolio.get('accountType')),
            },
            'positions': [{
                'symbol': _text(((row.get('instrument') or {}).get('symbol'))),
                'name': _text(((row.get('instrument') or {}).get('name'))),
                'asset_type': _text(((row.get('instrument') or {}).get('type'))),
                'qty': _text(row.get('quantity')),
                'market_value': _text(row.get('currentValue')),
                'percent_of_portfolio': _text(row.get('percentOfPortfolio')),
                'cost_basis': _text(((row.get('costBasis') or {}).get('totalCost'))),
            } for row in portfolio.get('positions') or []],
            'orders': [],
            'execution_active': False,
        }


def accounts_snapshot(env, client=None):
    """Return both broker views. Missing keys are 'not connected', never an exception."""
    alpaca_key = (env.get('ALPACA_PAPER_KEY_ID') or '').strip()
    alpaca_secret = (env.get('ALPACA_PAPER_SECRET_KEY') or '').strip()
    public_secret = (env.get('PUBLIC_READONLY_SECRET') or '').strip()
    public_account = (env.get('PUBLIC_ACCOUNT_ID') or '').strip()
    alpaca = disconnected('alpaca_paper', 'Alpaca Paper')
    public = disconnected('public', 'Public.com')
    owns = client is None
    http = client or httpx.Client(timeout=TIMEOUT, follow_redirects=False)
    try:
        if alpaca_key and alpaca_secret:
            try:
                alpaca = AlpacaPaperClient(alpaca_key, alpaca_secret, client=http).snapshot()
            except (PaperOnlyError, httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                alpaca = disconnected('alpaca_paper', 'Alpaca Paper')
                alpaca['status'], alpaca['message'] = _status(exc)
        if public_secret and public_account:
            try:
                public = PublicReadonlyClient(public_secret, public_account, client=http).snapshot()
            except (httpx.HTTPError, ValueError, TypeError, KeyError) as exc:
                public = disconnected('public', 'Public.com')
                public['status'], public['message'] = _status(exc)
        return {
            'execution_active': False,
            'read_only': True,
            'accounts': [alpaca, public],
        }
    finally:
        if owns:
            http.close()
