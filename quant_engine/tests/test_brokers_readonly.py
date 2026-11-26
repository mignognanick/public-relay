"""Read-only broker snapshots must never place or cancel orders."""
import inspect

import httpx
import pytest
from fastapi.testclient import TestClient

from quant_engine.brokers import readonly
from quant_engine.brokers.readonly import (
    PAPER_ALPACA_URL,
    PUBLIC_TOKEN_URL,
    AlpacaPaperClient,
    GetOnlyClient,
    PaperOnlyError,
    PublicReadonlyClient,
    accounts_snapshot,
    paper_alpaca_base,
)
from quant_engine.dashboard.app import create_app
from quant_engine.dashboard.domain import Strategy, eligibility, initial_state, view
from quant_engine.tests.test_dashboard import TOKEN
from datetime import datetime, timezone


@pytest.fixture
def store(tmp_path):
    from quant_engine.dashboard.store import SQLiteStore
    return SQLiteStore(tmp_path / 'state.sqlite3')


class RecordingClient:
    def __init__(self, responses=None, errors=None):
        self.calls = []
        self.responses = responses or {}
        self.errors = errors or {}

    def _handle(self, method, url, **kwargs):
        key = (method.upper(), str(url).split('?')[0])
        self.calls.append((method.upper(), str(url)))
        if key in self.errors:
            raise self.errors[key]
        # Match by method + exact path or method + prefix
        for (m, prefix), value in self.responses.items():
            if m == method.upper() and str(url).startswith(prefix):
                if isinstance(value, Exception):
                    raise value
                return value
        raise AssertionError('Unexpected request ' + method + ' ' + str(url))

    def get(self, url, **kwargs):
        return self._handle('GET', url, **kwargs)

    def post(self, url, **kwargs):
        return self._handle('POST', url, **kwargs)


def json_response(payload, url='https://example.invalid'):
    return httpx.Response(200, json=payload, request=httpx.Request('GET', url))


def token_response():
    return httpx.Response(
        200,
        json={'accessToken': 'public-access-token-for-tests'},
        request=httpx.Request('POST', PUBLIC_TOKEN_URL),
    )


def test_non_paper_alpaca_url_is_refused_without_http():
    client = RecordingClient()
    with pytest.raises(PaperOnlyError, match='paper API'):
        paper_alpaca_base('https://api.alpaca.markets')
    with pytest.raises(PaperOnlyError, match='paper API'):
        AlpacaPaperClient('paper-key-id', 'paper-secret-key', client=client, base_url='https://api.alpaca.markets')
    with pytest.raises(PaperOnlyError, match='paper API'):
        AlpacaPaperClient('paper-key-id', 'paper-secret-key', client=client, base_url='http://paper-api.alpaca.markets')
    assert client.calls == []
    assert paper_alpaca_base() == PAPER_ALPACA_URL
    assert paper_alpaca_base(PAPER_ALPACA_URL + '/') == PAPER_ALPACA_URL


def test_alpaca_snapshot_uses_only_get_on_paper_host():
    client = RecordingClient(responses={
        ('GET', PAPER_ALPACA_URL + '/v2/account'): json_response({
            'equity': '100000', 'cash': '25000', 'buying_power': '40000',
            'portfolio_value': '100000', 'currency': 'USD', 'account_number': 'PA123',
        }),
        ('GET', PAPER_ALPACA_URL + '/v2/positions'): json_response([
            {'symbol': 'SPY', 'qty': '2', 'side': 'long', 'market_value': '1000',
             'avg_entry_price': '500', 'current_price': '500', 'unrealized_pl': '0'},
        ]),
        ('GET', PAPER_ALPACA_URL + '/v2/orders'): json_response([
            {'id': 'ord-1', 'symbol': 'SPY', 'side': 'buy', 'qty': '1', 'filled_qty': '1',
             'type': 'limit', 'status': 'filled', 'submitted_at': '2026-11-25T14:00:00Z', 'filled_at': '2026-11-25T14:01:00Z'},
        ]),
    })
    snapshot = AlpacaPaperClient('paper-key-id', 'paper-secret-key', client=client).snapshot()
    assert snapshot['connected'] is True
    assert snapshot['execution_active'] is False
    assert snapshot['balances']['equity'] == '100000'
    assert snapshot['balances']['buying_power'] == '40000'
    assert snapshot['positions'][0]['symbol'] == 'SPY'
    assert snapshot['orders'][0]['id'] == 'ord-1'
    assert [method for method, _ in client.calls] == ['GET', 'GET', 'GET']
    assert all(url.startswith(PAPER_ALPACA_URL) for _, url in client.calls)
    assert {urlparse_path(url) for _, url in client.calls} == {'/v2/account', '/v2/positions', '/v2/orders'}
    blob = str(snapshot)
    assert 'paper-secret-key' not in blob and 'paper-key-id' not in blob


def urlparse_path(url):
    return httpx.URL(url).path


def test_public_snapshot_posts_only_for_the_token_then_gets_portfolio():
    portfolio_url = 'https://api.public.com/userapigateway/trading/acct-123/portfolio/v2'
    client = RecordingClient(responses={
        ('POST', PUBLIC_TOKEN_URL): token_response(),
        ('GET', portfolio_url): json_response({
            'accountId': 'acct-123',
            'accountType': 'BROKERAGE',
            'totalAccountValue': '12000.00',
            'buyingPower': {'buyingPower': '8000.00', 'cashOnlyBuyingPower': '4000.00'},
            'equity': [{'type': 'CASH', 'value': '4000.00'}],
            'positions': [{'instrument': {'symbol': 'AAPL', 'name': 'Apple Inc.', 'type': 'EQUITY'},
                           'quantity': '10', 'currentValue': '1500.00', 'percentOfPortfolio': '12.50',
                           'costBasis': {'totalCost': '1400.00'}}],
            'orders': [{'orderId': 'should-not-be-copied'}],
        }),
    })
    snapshot = PublicReadonlyClient('public-readonly-secret', 'acct-123', client=client).snapshot()
    assert snapshot['connected'] is True
    assert snapshot['execution_active'] is False
    assert snapshot['balances']['buying_power'] == '8000.00'
    assert snapshot['positions'][0]['symbol'] == 'AAPL'
    assert snapshot['orders'] == []
    assert client.calls == [('POST', PUBLIC_TOKEN_URL), ('GET', portfolio_url)]
    assert all(method == 'GET' or url == PUBLIC_TOKEN_URL for method, url in client.calls)
    assert 'public-readonly-secret' not in str(snapshot)


def test_missing_keys_are_not_connected_and_make_no_requests():
    client = RecordingClient()
    snapshot = accounts_snapshot({}, client=client)
    assert snapshot['execution_active'] is False
    assert snapshot['read_only'] is True
    assert [row['status'] for row in snapshot['accounts']] == ['not connected', 'not connected']
    assert all(row['connected'] is False and row['balances'] is None for row in snapshot['accounts'])
    assert client.calls == []
    partial = accounts_snapshot({'ALPACA_PAPER_KEY_ID': 'paper-key-id', 'PUBLIC_READONLY_SECRET': 'secret'}, client=client)
    assert all(row['status'] == 'not connected' for row in partial['accounts'])
    assert client.calls == []


def test_timeout_and_rejected_requests_are_unavailable_without_secrets():
    timed = RecordingClient(errors={
        ('GET', PAPER_ALPACA_URL + '/v2/account'): httpx.TimeoutException('request timed out'),
    })
    alpaca = accounts_snapshot({
        'ALPACA_PAPER_KEY_ID': 'paper-key-id',
        'ALPACA_PAPER_SECRET_KEY': 'paper-secret-key',
    }, client=timed)
    row = alpaca['accounts'][0]
    assert row['connected'] is False
    assert row['status'] == 'unavailable'
    assert row['message'] == 'Broker request timed out'
    assert 'paper-secret-key' not in str(alpaca) and 'paper-key-id' not in str(alpaca)
    assert alpaca['execution_active'] is False

    rejected = RecordingClient(responses={
        ('POST', PUBLIC_TOKEN_URL): httpx.Response(
            401, json={'message': 'invalid secret public-readonly-secret'},
            request=httpx.Request('POST', PUBLIC_TOKEN_URL),
        ),
    })
    public = accounts_snapshot({
        'PUBLIC_READONLY_SECRET': 'public-readonly-secret',
        'PUBLIC_ACCOUNT_ID': 'acct-123',
    }, client=rejected)
    row = public['accounts'][1]
    assert row['connected'] is False
    assert row['status'] == 'unavailable'
    assert row['message'] == 'Broker request was rejected'
    assert 'public-readonly-secret' not in str(public)


def test_get_only_client_cannot_leave_the_allowed_host():
    inner = RecordingClient()
    http = GetOnlyClient(inner, PAPER_ALPACA_URL)
    with pytest.raises(ValueError, match='allowed host'):
        http.get('https://api.alpaca.markets/v2/account')
    assert inner.calls == []
    assert not hasattr(http, 'post')
    assert not hasattr(http, 'put')
    assert not hasattr(http, 'delete')
    assert not hasattr(http, 'patch')
    assert not hasattr(http, 'request')


def test_accounts_route_is_authenticated_and_keeps_execution_off(store):
    app = create_app(store, {'QE_LOCAL_TOKEN': TOKEN})
    client = TestClient(app)
    assert client.get('/api/accounts').status_code == 401
    assert client.get('/api/accounts', headers={'Authorization': 'Bearer incorrect'}).status_code == 401
    headers = {'Authorization': 'Bearer ' + TOKEN}
    response = client.get('/api/accounts', headers=headers)
    assert response.status_code == 200
    payload = response.json()
    assert payload['execution_active'] is False
    assert payload['read_only'] is True
    assert [row['status'] for row in payload['accounts']] == ['not connected', 'not connected']
    state = client.get('/api/state', headers=headers).json()
    assert state['balances'] is None
    assert state['broker_connected'] is False
    assert all(item['execution_active'] is False for item in state['eligibility'].values()) or state['eligibility'] == {}
    shown = view(initial_state(), 0)
    assert shown['broker_connected'] is False
    assert shown['balances'] is None
    strategy = Strategy(id='x', name='Test idea', portfolio='day', thesis='Closed bars only')
    assert eligibility(initial_state(), strategy, datetime.now(timezone.utc))['execution_active'] is False


def test_accounts_route_uses_injected_client_and_hides_secrets(store):
    recorder = RecordingClient(responses={
        ('GET', PAPER_ALPACA_URL + '/v2/account'): json_response({
            'equity': '10', 'cash': '10', 'buying_power': '10', 'portfolio_value': '10', 'currency': 'USD',
        }),
        ('GET', PAPER_ALPACA_URL + '/v2/positions'): json_response([]),
        ('GET', PAPER_ALPACA_URL + '/v2/orders'): json_response([]),
    })
    app = create_app(store, {
        'QE_LOCAL_TOKEN': TOKEN,
        'ALPACA_PAPER_KEY_ID': 'paper-key-id',
        'ALPACA_PAPER_SECRET_KEY': 'paper-secret-key',
    }, brokers_client=recorder)
    payload = TestClient(app).get('/api/accounts', headers={'Authorization': 'Bearer ' + TOKEN}).json()
    assert payload['accounts'][0]['connected'] is True
    assert payload['execution_active'] is False
    assert [method for method, _ in recorder.calls] == ['GET', 'GET', 'GET']
    assert 'paper-secret-key' not in str(payload)


def test_readonly_module_has_no_order_submission_paths():
    source = inspect.getsource(readonly)
    for needle in ('live-api.alpaca.markets', 'submit_order', 'place_order', 'cancel_order', '/v2/orders/', 'orderIds'):
        assert needle not in source
    assert '_raw.post(' in inspect.getsource(PublicReadonlyClient._access_token)
    assert '/userapiauthservice/personal/access-tokens' in source
    # Data methods must not define POST/PUT/DELETE helpers
    assert 'def post' not in inspect.getsource(GetOnlyClient)
    assert 'def get' in inspect.getsource(GetOnlyClient)
