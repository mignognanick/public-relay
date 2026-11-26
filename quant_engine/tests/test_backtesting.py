from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from quant_engine.backtesting.alpaca import DataUnavailable, import_alpaca
from quant_engine.backtesting.cli import replay
from quant_engine.backtesting.data import ET, csv_text, dataset_payload, demo_csv, parse_csv
from quant_engine.backtesting.engine import TEMPLATES, run_backtest, signals, simulate
from quant_engine.backtesting.models import AlpacaImport, Parameters, RunRequest
from quant_engine.backtesting.store import ResearchStore
from quant_engine.dashboard.app import create_app
from quant_engine.dashboard.store import Conflict, SQLiteStore

TOKEN = 'backtesting-local-test-token-123456789'

@pytest.fixture
def dataset():
    return dataset_payload(demo_csv(), 'SPY', 'Manufactured test fixture', 'synthetic')


def request(dataset_id, **changes):
    return RunRequest(request_id=uuid4(), dataset_id=dataset_id, strategy_id='test',
                      template='orb_bull', split_date='2024-02-12', **changes)


@pytest.mark.parametrize('mutation', ['duplicate','gap','unsorted','naive','ohlc','nan','volume','future'])
def test_bad_csv_rejected(mutation):
    bars = parse_csv(demo_csv())
    if mutation == 'duplicate': bars.insert(1,bars[0])
    if mutation == 'gap': bars.pop(20)
    if mutation == 'unsorted': bars[0],bars[1] = bars[1],bars[0]
    if mutation == 'naive': bars[0] = replace(bars[0],start=bars[0].start.replace(tzinfo=None))
    if mutation == 'ohlc': bars[0] = replace(bars[0],h=D('1'))
    if mutation == 'nan': bars[0] = replace(bars[0],c=D('NaN'))
    if mutation == 'volume': bars = [replace(b,volume=D(0)) for b in bars]
    now = datetime(2024,2,5,9,31,tzinfo=ET) if mutation == 'future' else None
    with pytest.raises(ValueError): parse_csv(csv_text(bars),now=now)


def test_half_sessions_and_hash(dataset):
    bars = parse_csv(dataset[1]['csv'])
    half = [replace(b,session_close=b.session_close.replace(hour=13)) for b in bars if b.start.hour < 13]
    assert len(parse_csv(csv_text(half))) == 12*42
    assert dataset == dataset_payload('\ufeff'+dataset[1]['csv'], 'SPY', 'Manufactured test fixture','synthetic')


@pytest.mark.parametrize('template', list(TEMPLATES))
def test_signal_causality(template):
    bars = parse_csv(demo_csv())[:78]
    params = Parameters()
    expected = signals(bars,template,params)
    for stop in range(1,len(bars)):
        assert signals(bars[:stop],template,params) == expected[:stop]


@pytest.mark.parametrize('template,side', [('vwap_bull',1),('vwap_bear',-1)])
def test_vwap_positive_signal(template,side):
    bars = parse_csv(demo_csv())[:78]
    values = [D(100)+side*D(i)/10 for i in range(78)]
    bars = [replace(b,o=v,c=v,h=v+D('.01'),l=v-D('.01'),volume=D(100)) for b,v in zip(bars,values)]
    bars[6] = replace(bars[6], l=D(99)) if side == 1 else replace(bars[6],h=D(101))
    result = signals(bars,template,Parameters(ema_period=5))
    assert not any(result[:6])
    assert result[6]


def controlled_session():
    bars = parse_csv(demo_csv())[:78]
    bars = [replace(b,o=D(100),h=D('100.1'),l=D('99.9'),c=D(100)) for b in bars]
    bars[3] = replace(bars[3],h=D(101),c=D(101))
    bars[4] = replace(bars[4],o=D(101),h=D(104),l=D(98),c=D(101))
    return bars


def test_next_open_stop_first_costs_and_sizing():
    bars = controlled_session()
    params = Parameters(stop_pct=1,spread_bps=0,slippage_bps=0,commission_per_share=0)
    result = simulate(bars,'orb_bull',params)
    trade = result['trades'][0]
    assert trade['entry_time'] == bars[4].start.isoformat()
    assert trade['signal_time'] == trade['entry_time']
    assert trade['signal_bar_start'] == bars[3].start.isoformat()
    assert trade['reason'] == 'stop'  # Target also touched, stop wins.
    assert trade['entry'] == '101.000000'
    assert D(trade['planned_risk']) <= D(250)
    assert trade['qty'] == 247
    assert D(trade['net_pnl']) == D('-249.47')
    assert D(result['metrics']['ending_equity']) == D(100000)+sum(D(t['net_pnl']) for t in result['trades'])
    costly = simulate(bars,'orb_bull',Parameters(stop_pct=1,commission_per_share='.01'))
    t = costly['trades'][0]
    assert D(t['entry']) > 101 and D(t['fees_and_borrow']) > 0
    assert D(t['exit_price']) < D(t['stop'])


@pytest.mark.parametrize('scenario', ['gap','flatten','target'])
def test_exits(scenario):
    bars = controlled_session()
    bars[4] = replace(bars[4],h=D('101.1'),l=D('100.9'))
    for i in range(5,len(bars)):
        bars[i] = replace(bars[i],o=D(101),c=D(101),h=D('101.1'),l=D('100.9'))
    if scenario == 'gap': bars[5] = replace(bars[5],o=D(95),l=D(94),c=D(95))
    if scenario == 'target': bars[5] = replace(bars[5],o=D(105),h=D(106),l=D(104),c=D(105))
    trade = simulate(bars,'orb_bull',Parameters(stop_pct=1,spread_bps=0,slippage_bps=0,commission_per_share=0))['trades'][0]
    assert trade['reason'] == {'gap':'gap_stop','flatten':'session_flatten','target':'target'}[scenario]
    assert D(trade['exit_price']) == {'gap':D(95),'flatten':D(101),'target':D('103.02')}[scenario]


def test_split_repeatability_and_no_promotion(dataset):
    req = request(dataset[0])
    first = run_backtest(dataset[1],req)
    assert first == run_backtest(dataset[1],req)
    assert first['development']['sessions'] == first['holdout']['sessions'] == 6
    assert all(t['entry_time'][:10] > '2024-02-12' for t in first['holdout']['trades'])
    assert not first['paper_approved'] and not first['minimum_trade_count_met'] and not first['execution_active']
    with pytest.raises(ValueError,match='two complete'):
        run_backtest(dataset[1],req.model_copy(update={'split_date':datetime(2024,2,5).date()}))


@pytest.fixture
def api(tmp_path):
    store = SQLiteStore(tmp_path/'test.sqlite')
    client = TestClient(create_app(store,{'QE_LOCAL_TOKEN':TOKEN}))
    client.headers['Authorization'] = 'Bearer '+TOKEN
    response = client.post('/api/commands',json={'version':0,'action':'create_strategy','reason':'Research test',
        'values':{'name':TEMPLATES['orb_bull'],'portfolio':'day','thesis':'Opening range test'}})
    assert response.status_code == 200
    return client,store


def test_authenticated_workflow_and_replay(api):
    client,store = api
    assert client.get('/api/research/datasets',headers={'Authorization':'Bearer bad'}).status_code == 401
    before = client.get('/api/state').json()
    data = client.post('/api/research/demo',json={}).json()
    assert client.post('/api/research/demo',json={}).json()['id'] == data['id']
    assert len(client.get('/api/research/datasets').json()) == 1
    body = request(data['id']).model_dump(mode='json')
    body['strategy_id'] = before['state']['strategies'][0]['id']
    result = client.post('/api/research/runs',json=body)
    assert result.status_code == 200, result.text
    assert result.json() == client.post('/api/research/runs',json=body).json()
    assert len(client.get('/api/research/runs').json()) == 1
    assert client.get('/api/state').json() == before
    changed = body | {'template':'orb_bear'}
    assert client.post('/api/research/runs',json=changed).status_code == 409
    assert client.get('/api/research/datasets/'+data['id']+'/csv').text.startswith('timestamp,')
    research = ResearchStore(SQLiteStore(store.path))
    assert research.get('other-owner','','dataset',data['id']) is None
    assert research.list('other-owner','','trial') == []
    assert 'csv' not in research.list('local-operator','','dataset')[0]['payload']
    assert 'report' not in research.list('local-operator','','result')[0]['payload']
    with pytest.raises(Conflict): research.append('local-operator','','result',body['request_id'],{})


def test_failed_trials_remain_and_wrong_template_rejected(api):
    client,store = api
    data = client.post('/api/research/demo',json={}).json()
    state = client.get('/api/state').json()
    body = request(data['id']).model_dump(mode='json')
    body['strategy_id'] = state['state']['strategies'][0]['id']
    assert client.post('/api/research/runs',json=body | {'template':'orb_bear'}).status_code == 422
    body['split_date'] = '2024-02-05'
    assert client.post('/api/research/runs',json=body).status_code == 422
    runs = client.get('/api/research/runs').json()
    assert len(runs) == 1 and runs[0]['status'] == 'failed' and runs[0]['trial_units'] == 1
    assert client.post('/api/research/runs',json=body).json()['payload']['status'] == 'failed'


@pytest.mark.parametrize('failure', [None,'denied','missing_session','missing_bar','pagination_loop'])
def test_alpaca_get_only_import(failure):
    bars = parse_csv(demo_csv())
    sessions = sorted({b.day for b in bars})
    called = []
    def handler(req):
        called.append(req)
        assert req.method == 'GET'
        assert req.headers['APCA-API-KEY-ID'] == 'fake-key'
        if req.url.path == '/v2/calendar':
            assert req.url.host == 'paper-api.alpaca.markets'
            return httpx.Response(200,json=[{'date':d,'open':'09:30','close':'16:00'} for d in sessions])
        assert req.url.host == 'data.alpaca.markets' and req.url.path == '/v2/stocks/bars'
        assert req.url.params['feed'] == 'iex' and req.url.params['timeframe'] == '5Min'
        if failure == 'denied': return httpx.Response(403,text='sensitive provider error')
        selected = bars if failure != 'missing_bar' else bars[1:]
        if failure == 'missing_session': selected = bars[78:]
        return httpx.Response(200,json={'bars':{'SPY':[{'t':b.start.isoformat(),'o':str(b.o),'h':str(b.h),'l':str(b.l),'c':str(b.c),'v':str(b.volume)} for b in selected]},'next_page_token':'repeated' if failure == 'pagination_loop' else None})
    req = AlpacaImport(symbol='SPY',start=sessions[0],end=sessions[-1])
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        if failure:
            with pytest.raises(ValueError) as err: import_alpaca(req,{'ALPACA_PAPER_KEY_ID':'fake-key','ALPACA_PAPER_SECRET_KEY':'fake-secret'},client)
            assert 'sensitive' not in str(err.value)
        else:
            _,result = import_alpaca(req,{'ALPACA_PAPER_KEY_ID':'fake-key','ALPACA_PAPER_SECRET_KEY':'fake-secret'},client)
            assert result['meta']['calendar_verified'] and result['meta']['kind'] == 'historical'
    assert called


def test_no_credentials_or_invalid_range():
    with pytest.raises(DataUnavailable,match='not configured'):
        import_alpaca(AlpacaImport(symbol='SPY',start='2024-02-01',end='2024-02-20'),{})
    with pytest.raises(ValueError,match='180'):
        import_alpaca(AlpacaImport(symbol='SPY',start='2023-01-01',end='2024-02-20'),{})


def test_replay_rejects_changed_data(api):
    client,_ = api
    dataset = client.post('/api/research/demo',json={}).json()
    body = request(dataset['id']).model_dump(mode='json')
    body['strategy_id'] = client.get('/api/state').json()['state']['strategies'][0]['id']
    saved = client.post('/api/research/runs',json=body).json()
    trial = client.get('/api/research/runs').json()[0]
    csv = client.get('/api/research/datasets/'+dataset['id']+'/csv').text
    assert replay({'trial':trial,'result':saved},csv) == saved['payload']['report']
    bars = parse_csv(csv)
    bars[0] = replace(bars[0],volume=bars[0].volume+1)
    with pytest.raises(ValueError,match='hash'):
        replay({'trial':trial,'result':saved},csv_text(bars))


def test_short_borrow_and_no_trade_metrics():
    bars = controlled_session()
    bars = [replace(b,o=200-b.o,h=200-b.l,l=200-b.h,c=200-b.c) for b in bars]
    trade = simulate(bars,'orb_bear',Parameters(stop_pct=1))['trades'][0]
    assert trade['direction'] == 'short' and D(trade['borrow']) > 0
    flat = [replace(b,o=D(100),h=D(100),l=D(100),c=D(100)) for b in bars]
    result = simulate(flat,'orb_bear',Parameters())
    assert result['metrics']['trades'] == 0 and result['metrics']['profit_factor'] is None
    assert result['metrics']['net_return_pct'] == '0.000000'


def test_gap_loss_locks_future_entries_and_still_exits():
    bars = parse_csv(demo_csv())
    days = sorted({b.day for b in bars})
    changed = []
    for day in days:
        session = [b for b in bars if b.day == day]
        pattern = controlled_session()
        for i,b in enumerate(session):
            source = pattern[i]
            changed.append(replace(b,o=source.o,h=source.h,l=source.l,c=source.c))
        changed[-74] = replace(changed[-74],h=D('101.1'),l=D('100.9'))
        changed[-73] = replace(changed[-73],o=D(90),h=D(91),l=D(89),c=D(90))
    result = simulate(changed,'orb_bull',Parameters(stop_pct=1,spread_bps=0,slippage_bps=0,commission_per_share=0))
    assert result['trades'][0]['reason'] == 'gap_stop'
    assert D(result['trades'][0]['net_pnl']) < -2000
    assert len(result['trades']) == 1  # Weekly kill remains latched even in later weeks.
    assert result['rejected_entries']
