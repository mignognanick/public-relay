"""Append-only configuration history; unique versions reject concurrent edits."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httpx

from quant_engine.dashboard.domain import State, initial_state


class Conflict(Exception):
    pass


class Unavailable(Exception):
    pass


class SQLiteStore:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS qe_versions (owner TEXT NOT NULL, version INTEGER NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(owner, version))')

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def history(self, owner, token='', limit=50):
        with self.connect() as db:
            rows = db.execute('SELECT version, state, reason, created_at FROM qe_versions WHERE owner=? ORDER BY version DESC LIMIT ?', (owner, limit)).fetchall()
        return [dict(version=r[0], state=json.loads(r[1]), reason=r[2], created_at=r[3], owner=owner) for r in rows]

    def read(self, owner, token=''):
        rows = self.history(owner, token, 1)
        return (State.model_validate(rows[0]['state']), rows[0]['version']) if rows else (initial_state(), 0)

    def save(self, owner, token, expected, state, reason):
        try:
            with self.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                current = db.execute('SELECT COALESCE(MAX(version), 0) FROM qe_versions WHERE owner=?', (owner,)).fetchone()[0]
                if current != expected:
                    raise Conflict()
                db.execute('INSERT INTO qe_versions VALUES (?, ?, ?, ?, ?)',
                           (owner, expected + 1, state.model_dump_json(), reason, datetime.now(timezone.utc).isoformat()))
        except sqlite3.Error as exc:
            raise Unavailable() from exc
        return expected + 1


class SupabaseStore:
    def __init__(self, url, key, client=None):
        self.url, self.key = url.rstrip('/'), key
        self.client = client or httpx.Client(timeout=10)

    def request(self, method, path, token, **kwargs):
        try:
            response = self.client.request(method, self.url + path,
                                           headers={'apikey': self.key, 'Authorization': 'Bearer ' + token}, **kwargs)
        except httpx.HTTPError as exc:
            raise Unavailable() from exc
        if response.status_code == 409:
            raise Conflict()
        if not response.is_success:
            raise Unavailable()
        return response

    def history(self, owner, token, limit=50):
        return self.request('GET', '/rest/v1/qe_versions', token,
                            params={'owner': 'eq.' + owner, 'order': 'version.desc', 'limit': limit, 'select': '*'}).json()

    def read(self, owner, token):
        rows = self.history(owner, token, 1)
        return (State.model_validate(rows[0]['state']), rows[0]['version']) if rows else (initial_state(), 0)

    def save(self, owner, token, expected, state, reason):
        self.request('POST', '/rest/v1/qe_versions', token,
                     json={'owner': owner, 'version': expected + 1, 'state': state.model_dump(mode='json'), 'reason': reason})
        return expected + 1
