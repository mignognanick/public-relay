"""Authenticated research API; registration never promotes or activates a strategy."""
import hashlib
import json

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import ValidationError

from quant_engine.backtesting.alpaca import DataUnavailable, import_alpaca
from quant_engine.backtesting.data import dataset_payload, demo_csv
from quant_engine.backtesting.engine import ENGINE_VERSION, RULES, TEMPLATES, implementation_hash, run_backtest
from quant_engine.backtesting.models import AlpacaImport, RunRequest, Upload
from quant_engine.backtesting.store import ResearchStore
from quant_engine.dashboard.store import Conflict


def router(database, identity, config, history_client=None):
    api = APIRouter(prefix='/api/research')
    store = ResearchStore(database) if database is not None else None

    def save_dataset(who, record_id, payload):
        existing = store.get(*who, 'dataset', record_id)
        if existing is None:
            try:
                store.append(*who, 'dataset', record_id, payload)
            except Conflict:
                if store.get(*who, 'dataset', record_id) is None:
                    raise
        return {'id':record_id, **payload['meta']}

    @api.get('/catalog')
    def catalog(who=Depends(identity)):
        return {'templates':TEMPLATES,'rules':RULES,'engine_version':ENGINE_VERSION}

    @api.get('/datasets')
    def datasets(who=Depends(identity)):
        return [{'id':r['id'],'created_at':r['created_at'],**r['payload']['meta']} for r in store.list(*who,'dataset')]

    @api.post('/datasets')
    def upload(body: Upload, who=Depends(identity)):
        try:
            record_id, payload = dataset_payload(body.csv, body.symbol, body.source, body.kind)
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        return save_dataset(who, record_id, payload)

    @api.post('/demo')
    def demo(who=Depends(identity)):
        record_id, payload = dataset_payload(demo_csv(),'SPY','Manufactured deterministic demo; not historical market data','synthetic')
        return save_dataset(who,record_id,payload)

    @api.post('/alpaca')
    def alpaca(body: AlpacaImport, who=Depends(identity)):
        try:
            record_id, payload = import_alpaca(body,config,client=history_client)
        except DataUnavailable as exc:
            raise HTTPException(503,str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422,str(exc)) from None
        return save_dataset(who,record_id,payload)

    @api.get('/datasets/{record_id}/csv')
    def export_data(record_id: str, who=Depends(identity)):
        record = store.get(*who,'dataset',record_id)
        if record is None:
            raise HTTPException(404,'Dataset not found')
        return Response(record['payload']['csv'],media_type='text/csv',headers={'Content-Disposition':'attachment; filename="dataset.csv"'})

    @api.get('/runs')
    def runs(who=Depends(identity)):
        # Intent records remain visible if a serverless invocation ends before saving its result.
        trials = store.list(*who,'trial')
        results = {r['id']:r for r in store.list(*who,'result')}
        return [{'id':t['id'],'created_at':t['created_at'],**t['payload'],
                 'status':results[t['id']]['payload']['status'] if t['id'] in results else 'incomplete',
                 'metrics':results[t['id']]['payload'].get('metrics') if t['id'] in results else None} for t in trials]

    @api.get('/runs/{record_id}')
    def result(record_id: str, who=Depends(identity)):
        record = store.get(*who,'result',record_id)
        if record is None:
            raise HTTPException(404,'Result not found; trial may be incomplete')
        return record

    @api.post('/runs')
    def run(body: RunRequest, who=Depends(identity)):
        record_id = str(body.request_id)
        request_json = body.model_dump(mode='json')
        old = store.get(*who,'trial',record_id)
        if old:
            if old['payload']['request'] != request_json:
                raise HTTPException(409,'This request ID belongs to different inputs')
            saved = store.get(*who,'result',record_id)
            if saved:
                return saved
            raise HTTPException(409,'Trial already recorded but not completed; refresh history before starting a new trial')
        try:
            state, version = database.read(*who)
        except ValidationError:
            raise HTTPException(503,'Stored registry is invalid') from None
        strategy = next((s for s in state.strategies if s.id == body.strategy_id),None)
        if strategy is None or strategy.stage == 'archived':
            raise HTTPException(422,'Choose an existing, non-archived strategy')
        if strategy.portfolio != 'day':
            raise HTTPException(422,'These templates are intraday only; choose a day-trading strategy')
        if strategy.name != TEMPLATES[body.template]:
            raise HTTPException(422,'The selected template must match its registered ORB/VWAP strategy name')
        dataset = store.get(*who,'dataset',body.dataset_id)
        if dataset is None:
            raise HTTPException(404,'Dataset not found')
        snapshot = strategy.model_dump(mode='json')
        spec = {'template':body.template,'parameters':request_json['parameters'],'rules':RULES,'engine_version':ENGINE_VERSION}
        spec_hash = hashlib.sha256(json.dumps(spec,sort_keys=True).encode()).hexdigest()
        trial = {'request':request_json,'strategy_snapshot':snapshot,'configuration_version':version,
                 'strategy_version':spec_hash,'implementation_sha256':implementation_hash(),
                 'dataset':dataset['payload']['meta'],'actor':who[0],
                 'specification':spec,'trial_units':1,'reason':'User-requested underlying-share research; no promotion or orders'}
        store.append(*who,'trial',record_id,trial)
        try:
            report = run_backtest(dataset['payload'],body)
        except ValueError as exc:
            failed = {'status':'failed','error':str(exc),'trial_id':record_id}
            store.append(*who,'result',record_id,failed)
            raise HTTPException(422,str(exc)) from None
        completed = {'status':'completed','trial_id':record_id,'report':report,
                     'metrics':{p:report[p]['metrics'] for p in ('development','holdout')}}
        store.append(*who,'result',record_id,completed)
        return store.get(*who,'result',record_id)

    return api
