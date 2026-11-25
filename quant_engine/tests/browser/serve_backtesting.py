"""Disposable loopback-only server for the backtesting DOM regression test."""
from pathlib import Path
from tempfile import TemporaryDirectory

import uvicorn
from fastapi.testclient import TestClient

from quant_engine.backtesting.engine import TEMPLATES
from quant_engine.dashboard.app import create_app


if __name__ == '__main__':
    with TemporaryDirectory(prefix='qe-backtest-test-') as directory:
        app = create_app(env={'QE_LOCAL_TOKEN':'backtesting-local-test-token-123456789',
                              'QE_SQLITE_PATH':str(Path(directory)/'state.sqlite')})
        with TestClient(app) as client:
            client.headers['Authorization'] = 'Bearer backtesting-local-test-token-123456789'
            for version,name in enumerate(TEMPLATES.values()):
                response = client.post('/api/commands',json={'version':version,'action':'create_strategy',
                    'reason':'Local interface test','values':{'name':name,'portfolio':'day','thesis':'Test only'}})
                response.raise_for_status()
        uvicorn.run(app,host='127.0.0.1',port=8766)
