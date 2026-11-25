"""Immutable datasets, trial intents and results, separate from trade controls."""
import json
import sqlite3
from datetime import datetime, timezone

from quant_engine.dashboard.store import Conflict, SQLiteStore, Unavailable


class ResearchStore:
    def __init__(self, dashboard_store):
        self.db = dashboard_store
        self.local = isinstance(dashboard_store, SQLiteStore)
        if self.local:
            with self.db.connect() as connection:
                connection.execute('CREATE TABLE IF NOT EXISTS qe_research_records (owner TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(owner, kind, id))')

    def get(self, owner, token, kind, record_id):
        if self.local:
            with self.db.connect() as connection:
                row = connection.execute('SELECT payload, created_at FROM qe_research_records WHERE owner=? AND kind=? AND id=?', (owner, kind, record_id)).fetchone()
            return {'id':record_id, 'payload':json.loads(row[0]), 'created_at':row[1]} if row else None
        rows = self.db.request('GET', '/rest/v1/qe_research_records', token,
                               params={'owner':'eq.'+owner,'kind':'eq.'+kind,'id':'eq.'+record_id,'select':'id,payload,created_at','limit':1}).json()
        return rows[0] if rows else None

    def list(self, owner, token, kind):
        if self.local:
            with self.db.connect() as connection:
                rows = connection.execute("SELECT id, CASE kind WHEN 'dataset' THEN json_object('meta',json_extract(payload,'$.meta')) WHEN 'result' THEN json_object('status',json_extract(payload,'$.status'),'metrics',json_extract(payload,'$.metrics')) ELSE payload END, created_at FROM qe_research_records WHERE owner=? AND kind=? ORDER BY created_at DESC LIMIT 100", (owner,kind)).fetchall()
            return [{'id':r[0],'payload':json.loads(r[1]),'created_at':r[2]} for r in rows]
        projection = {'dataset':'id,meta:payload->meta,created_at','result':'id,status:payload->>status,metrics:payload->metrics,created_at'}.get(kind,'id,payload,created_at')
        rows = self.db.request('GET','/rest/v1/qe_research_records',token,
                               params={'owner':'eq.'+owner,'kind':'eq.'+kind,'select':projection,'order':'created_at.desc','limit':100}).json()
        if kind in ('dataset','result'):
            return [{'id':r['id'],'created_at':r['created_at'],'payload':{k:v for k,v in r.items() if k not in ('id','created_at')}} for r in rows]
        return rows

    def append(self, owner, token, kind, record_id, payload):
        if self.local:
            try:
                with self.db.connect() as connection:
                    connection.execute('INSERT INTO qe_research_records VALUES (?,?,?,?,?)',
                                       (owner,kind,record_id,json.dumps(payload,allow_nan=False),datetime.now(timezone.utc).isoformat()))
            except sqlite3.IntegrityError:
                raise Conflict() from None
            except sqlite3.Error:
                raise Unavailable() from None
        else:
            self.db.request('POST','/rest/v1/qe_research_records',token,
                            json={'owner':owner,'kind':kind,'id':record_id,'payload':payload})
