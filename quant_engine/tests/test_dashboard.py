from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from quant_engine.dashboard.app import create_app
from quant_engine.dashboard.domain import Command, State, Strategy, Tax, eligibility, initial_state, tax_plan, transition
from quant_engine.dashboard.store import Conflict, SQLiteStore, SupabaseStore, Unavailable

TOKEN = 'local-test-token-not-a-secret-123456789'
NOW = datetime.now(timezone.utc)


@pytest.fixture
def store(tmp_path):
    return SQLiteStore(tmp_path / 'state.sqlite3')


@pytest.fixture
def client(store):
    return TestClient(create_app(store, {'QE_LOCAL_TOKEN': TOKEN}))


def apply(state, action, values, target='', actor='reviewer'):
    return transition(state, Command(version=0, action=action, target=target, reason='test review', values=values), actor, NOW)


def new_strategy():
    return apply(initial_state(), 'create_strategy', {'name': 'Test idea', 'portfolio': 'day', 'thesis': 'Closed bars only'})


def test_auth_and_fail_closed(client, store):
    for path in ['/api/state', '/api/history']:
        assert client.get(path).status_code == 401
        assert client.get(path, headers={'Authorization': 'Bearer incorrect'}).status_code == 401
    assert client.post('/api/commands', json={}).status_code == 401
    assert client.post('/api/login', json={'password': 'bad'}).status_code == 401
    assert client.post('/api/login', json={'password': TOKEN}).json()['access_token'] == TOKEN
    response = client.get('/api/state', headers={'Authorization': 'Bearer ' + TOKEN})
    assert response.status_code == 200
    assert response.json()['state']['tax']['carryforward'] == '35000'
    assert response.json()['state']['tax']['source'] == 'user_stated_unverified'
    assert response.json()['balances'] is None
    assert response.headers['cache-control'] == 'no-store'
    locked = TestClient(create_app(store, {}))
    assert locked.get('/api/state', headers={'Authorization': 'Bearer ' + TOKEN}).status_code == 503
    with pytest.raises(RuntimeError):
        create_app(store, {'VERCEL': '1', 'QE_LOCAL_TOKEN': TOKEN})


def test_commands_persist_audit_and_reject_conflict(client, store):
    headers = {'Authorization': 'Bearer ' + TOKEN}
    command = {'version': 0, 'action': 'global_pause', 'values': {'paused': False}, 'reason': 'Paper planning'}
    r = client.post('/api/commands', json=command, headers=headers)
    assert r.status_code == 200
    assert r.json()['version'] == 1
    assert r.json()['worker_acknowledged_version'] is None
    assert client.post('/api/commands', json=command, headers=headers).status_code == 409
    log = client.get('/api/history', headers=headers).json()
    assert len(log) == 1 and log[0]['owner'] == 'local-operator'
    assert log[0]['state']['paused'] is False
    assert SQLiteStore(store.path).read('local-operator')[1] == 1
    assert store.read('someone-else')[1] == 0


def test_store_concurrent_edits_one_winner(store):
    def save(_):
        try:
            return store.save('actor', '', 0, initial_state(), 'concurrent test')
        except Conflict:
            return None
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(save, range(32)))
    assert results.count(1) == 1
    assert len(store.history('actor')) == 1


def test_live_unknown_keys_and_risk_mutation_rejected(client):
    headers = {'Authorization': 'Bearer ' + TOKEN}
    for action, target, values in [('portfolio', 'day', {'execution':'LIVE'}),
                                    ('global_pause', '', {'paused': False, 'max_trade_risk_pct': 100}),
                                    ('create_strategy', '', {'name':'Skip', 'portfolio':'day', 'thesis':'test','stage':'paper'}),
                                    ('tax', '', {'source':'verified'})]:
        assert client.post('/api/commands', headers=headers, json={'version':0,'action':action,'target':target,'values':values,'reason':'test'}).status_code == 422
    with pytest.raises(ValidationError):
        State.model_validate(initial_state().model_dump() | {'execution':'LIVE'})
    assert client.post('/webhook', json={}).status_code == 404
    assert client.post('/orders', json={}).status_code == 404


def test_lifecycle_and_approval():
    state = new_strategy()
    sid = state.strategies[0].id
    for values in [{'stage':'paper'}, {'enabled':True}, {'stage':'validated'}]:
        with pytest.raises(ValueError):
            apply(state, 'strategy', values, sid)
    state = apply(state, 'strategy', {'stage':'specified'}, sid)
    with pytest.raises(ValueError):
        apply(state, 'strategy', {'stage':'backtested'}, sid)
    state = apply(state, 'strategy', {'stage':'backtested','evidence':'dataset/test.csv; SHA abc; costs .01; train 2020-2022 test 2023', 'trials': 12, 'costs_modeled':True}, sid)
    with pytest.raises(ValueError):
        apply(state, 'strategy', {'stage':'validated','held_out_trades':29}, sid)
    state = apply(state, 'strategy', {'stage':'validated','held_out_trades':30}, sid)
    state = apply(state, 'strategy', {'stage':'paper','enabled':True}, sid, actor='nick')
    assert state.strategies[0].approved_by == 'nick'
    assert not eligibility(state, state.strategies[0], NOW)['eligible_for_risk_review']
    state = apply(state, 'global_pause', {'paused':False})
    state = apply(state, 'portfolio', {'paused':False}, 'day')
    assert eligibility(state, state.strategies[0], NOW)['eligible_for_risk_review']
    state = apply(state, 'strategy', {'stage':'archived'}, sid)
    assert not state.strategies[0].enabled
    with pytest.raises(ValueError):
        apply(state, 'strategy', {'stage':'paper'}, sid)


def test_regimes_expiry_isolation_and_automatic_failure():
    state = initial_state()
    state.paused = False
    state.portfolios[0].paused = False
    s = Strategy(id='s',name='s',portfolio='day',thesis='test',stage='paper',enabled=True,approved_by='nick')
    state.strategies.append(s)
    state = apply(state, 'portfolio', {'mode':'automatic'}, 'day')
    assert not eligibility(state, s, NOW)['eligible_for_risk_review']
    state = apply(state, 'portfolio', {'override':'chop','override_until':(NOW+timedelta(minutes=30)).isoformat()}, 'day')
    assert eligibility(state, s, NOW)['eligible_for_risk_review']
    assert not eligibility(state, s, NOW+timedelta(minutes=30))['eligible_for_risk_review']
    assert state.portfolios[1].mode == 'hybrid' and state.portfolios[1].paused
    for values in [{'override':'bull'}, {'override':'bull', 'override_until':'2030-01-01T00:00:00'},
                   {'override':'bull', 'override_until':NOW.isoformat()}]:
        with pytest.raises(ValueError):
            apply(initial_state(), 'portfolio', values, 'day')


@pytest.mark.parametrize('gains,losses,filing,income,offset,deduction,remaining', [
    ('20000','0','other',True,'20000','3000','12000'),
    ('50000','0','other',True,'35000','0','0'),
    ('-5000','0','other',True,'0','3000','37000'),
    ('10000','-10000','married_filing_separately',True,'0','1500','33500'),
    ('20000','0','other',False,'20000','0','15000')])
def test_tax_decimal_scenarios(gains,losses,filing,income,offset,deduction,remaining):
    p = tax_plan(Tax(short_term_net=gains,long_term_net=losses,filing=filing,sufficient_income=income))
    assert Decimal(p['gains_offset']) == Decimal(offset)
    assert Decimal(p['illustrative_deduction']) == Decimal(deduction)
    assert Decimal(p['illustrative_next_carryforward']) == Decimal(remaining)


@pytest.mark.parametrize('value', ['NaN','Infinity','-1','35000.001', True])
def test_invalid_carryforward(value):
    with pytest.raises(ValidationError):
        Tax(carryforward=value)


def test_supabase_transport_and_verified_user():
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/auth/v1/user':
            return httpx.Response(200, json={'id':'user-123','is_anonymous':False})
        if request.method == 'GET':
            return httpx.Response(200, json=[])
        return httpx.Response(409, json={})
    http = httpx.Client(transport=httpx.MockTransport(handler))
    env = {'QE_AUTH_MODE':'supabase','QE_SUPABASE_URL':'https://dev.example.test','QE_SUPABASE_PUBLISHABLE_KEY':'publishable-test'}
    client = TestClient(create_app(env=env, client=http))
    headers = {'Authorization':'Bearer user-jwt'}
    assert client.get('/api/state',headers=headers).status_code == 200
    assert calls[0].url.path == '/auth/v1/user'
    assert calls[1].url.params['owner'] == 'eq.user-123'
    assert calls[1].headers['authorization'] == 'Bearer user-jwt'
    r = client.post('/api/commands',headers=headers,json={'version':0,'action':'global_pause','reason':'test','values':{'paused':True}})
    assert r.status_code == 409
    assert all(r.headers['apikey'] == 'publishable-test' for r in calls)


def test_supabase_unavailable_and_anonymous_rejected():
    http = httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(503)))
    with pytest.raises(Unavailable):
        SupabaseStore('https://dev.test','public',http).read('user','token')
    env={'QE_AUTH_MODE':'supabase','QE_SUPABASE_URL':'https://dev.test','QE_SUPABASE_PUBLISHABLE_KEY':'public'}
    http=httpx.Client(transport=httpx.MockTransport(lambda r:httpx.Response(200,json={'id':'anonymous','is_anonymous':True})))
    assert TestClient(create_app(env=env,client=http)).get('/api/state',headers={'Authorization':'Bearer bad'}).status_code == 403


def test_corrupt_storage_fails_closed(client, store):
    with store.connect() as db:
        db.execute('INSERT INTO qe_versions VALUES (?,?,?,?,?)',('local-operator',1,'{"execution":"LIVE"}','bad','now'))
    assert client.get('/api/state',headers={'Authorization':'Bearer '+TOKEN}).status_code == 503
