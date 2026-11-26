"""Separate ASGI entrypoint. Never imports the production relay or broker clients."""
import hmac
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError

from quant_engine.backtesting.api import router as research_router
from quant_engine.dashboard.domain import Command, transition, view
from quant_engine.brokers.readonly import accounts_snapshot
from quant_engine.dashboard.store import Conflict, SQLiteStore, SupabaseStore, Unavailable

STATIC = Path(__file__).parent / 'static'


class Login(BaseModel):
    email: str = Field(default='', max_length=320)
    password: str = Field(min_length=1, max_length=1024)


def create_app(store=None, env=None, client=None, brokers_client=None, history_client=None):
    keys = ('QE_AUTH_MODE', 'QE_LOCAL_TOKEN', 'QE_SUPABASE_URL', 'QE_SUPABASE_PUBLISHABLE_KEY', 'QE_SQLITE_PATH', 'VERCEL', 'ALPACA_PAPER_KEY_ID', 'ALPACA_PAPER_SECRET_KEY', 'PUBLIC_READONLY_SECRET', 'PUBLIC_ACCOUNT_ID')
    config = {key: os.environ[key] for key in keys if key in os.environ} if env is None else dict(env)
    auth_mode = config.get('QE_AUTH_MODE', 'local')
    token = config.get('QE_LOCAL_TOKEN', '')
    url = config.get('QE_SUPABASE_URL', '').rstrip('/')
    public_key = config.get('QE_SUPABASE_PUBLISHABLE_KEY', '')
    transport = client or httpx.Client(timeout=10)
    if auth_mode not in ('local', 'supabase'):
        raise RuntimeError('QE_AUTH_MODE must be local or supabase')
    if auth_mode == 'local' and config.get('VERCEL'):
        raise RuntimeError('Local authentication/storage is forbidden on Vercel')
    if auth_mode == 'supabase' and (urlparse(url).scheme != 'https' or not public_key):
        raise RuntimeError('Supabase mode requires an HTTPS project URL and publishable key')
    database = store
    if database is None and auth_mode == 'supabase':
        database = SupabaseStore(url, public_key, transport)
    if database is None and len(token) >= 32:
        database = SQLiteStore(config.get('QE_SQLITE_PATH', 'var/quant-dashboard.sqlite3'))
    app = FastAPI(title='Quant Engine — paper control plane', docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware('http')
    async def security(request: Request, call_next):
        response = await call_next(request)
        response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                                 'Referrer-Policy': 'no-referrer', 'X-Frame-Options': 'DENY',
                                 'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def identity(request: Request):
        bearer = request.headers.get('Authorization', '')
        if not bearer.startswith('Bearer ') or len(bearer) > 8192:
            raise HTTPException(401, 'Sign in required')
        supplied = bearer[7:]
        if auth_mode == 'local':
            if len(token) < 32 or database is None:
                raise HTTPException(503, 'Configure QE_LOCAL_TOKEN with at least 32 characters')
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                raise HTTPException(401, 'Invalid credentials')
            return ('local-operator', supplied)
        try:
            response = transport.get(url + '/auth/v1/user', headers={'apikey': public_key, 'Authorization': bearer})
        except httpx.HTTPError:
            raise HTTPException(503, 'Authentication service unavailable') from None
        if response.status_code != 200:
            raise HTTPException(401, 'Session expired or invalid')
        user = response.json()
        if not user.get('id') or user.get('is_anonymous'):
            raise HTTPException(403, 'A registered account is required')
        return (user['id'], supplied)

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse({'detail': 'Settings changed in another session. Refresh and review before saving.'}, status_code=409)

    @app.exception_handler(Unavailable)
    @app.exception_handler(sqlite3.Error)
    async def unavailable(request, exc):
        return JSONResponse({'detail': 'Configuration storage unavailable; no change confirmed.'}, status_code=503)

    @app.get('/')
    def shell():
        return FileResponse(STATIC / 'index.html')

    @app.get('/api/session')
    def session():
        return {'auth_mode': auth_mode}

    @app.post('/api/login')
    def login(body: Login):
        if auth_mode == 'local':
            if len(token) < 32:
                raise HTTPException(503, 'Local access is not configured')
            if not hmac.compare_digest(body.password.encode(), token.encode()):
                raise HTTPException(401, 'Invalid credentials')
            return {'access_token': token}
        try:
            response = transport.post(url + '/auth/v1/token?grant_type=password',
                                      headers={'apikey': public_key}, json=body.model_dump())
        except httpx.HTTPError:
            raise HTTPException(503, 'Authentication service unavailable') from None
        if response.status_code != 200:
            raise HTTPException(401, 'Sign-in failed')
        return {'access_token': response.json()['access_token']}

    @app.get('/api/state')
    def state(who=Depends(identity)):
        try:
            current, version = database.read(*who)
            return view(current, version)
        except ValidationError:
            raise HTTPException(503, 'Stored configuration is invalid; entries unavailable') from None

    @app.get('/api/history')
    def history(who=Depends(identity)):
        return database.history(*who)

    @app.get('/api/accounts')
    def accounts(who=Depends(identity)):
        # Read-only snapshots. Missing keys are "not connected"; never fail the page.
        snapshot = accounts_snapshot(config, client=brokers_client)
        snapshot['execution_active'] = False
        return snapshot

    @app.post('/api/commands')
    def command(body: Command, who=Depends(identity)):
        try:
            current, version = database.read(*who)
        except ValidationError:
            raise HTTPException(503, 'Stored configuration is invalid') from None
        if version != body.version:
            raise Conflict()
        try:
            updated = transition(current, body, who[0], datetime.now(timezone.utc))
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        version = database.save(*who, version, updated, body.action + ': ' + body.reason)
        return view(updated, version)

    app.include_router(research_router(database, identity, config, history_client))

    app.mount('/static', StaticFiles(directory=STATIC), name='static')
    return app


app = create_app()
