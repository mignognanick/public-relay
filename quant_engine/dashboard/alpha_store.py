"""Owner-scoped storage adapter for Alpha Center research state (SQLite local + Supabase REST)."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from typing import Any

from quant_engine.dashboard.store import Conflict, SQLiteStore, Unavailable


class AlphaStore:
    """Persists Alpha Center snapshots, experiment runs, daily returns, and allocation history."""

    def __init__(self, dashboard_store) -> None:
        self.db = dashboard_store
        self.local = isinstance(dashboard_store, SQLiteStore)
        if self.local:
            with self.db.connect() as conn:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS qe_alpha_records ("
                    "owner TEXT NOT NULL, kind TEXT NOT NULL, id TEXT NOT NULL, "
                    "payload TEXT NOT NULL, created_at TEXT NOT NULL, "
                    "PRIMARY KEY(owner, kind, id))"
                )

    def get(self, owner: str, token: str, kind: str, record_id: str) -> dict[str, Any] | None:
        if self.local:
            with self.db.connect() as conn:
                row = conn.execute(
                    "SELECT payload, created_at FROM qe_alpha_records WHERE owner=? AND kind=? AND id=?",
                    (owner, kind, record_id),
                ).fetchone()
            return {"id": record_id, "payload": json.loads(row[0]), "created_at": row[1]} if row else None
        table = self._supabase_table(kind)
        id_col = self._supabase_id_col(kind)
        rows = self.db.request(
            "GET",
            f"/rest/v1/{table}",
            token,
            params={"owner": "eq." + owner, id_col: "eq." + record_id, "limit": 1},
        ).json()
        return {"id": record_id, "payload": rows[0], "created_at": rows[0].get("created_at")} if rows else None

    def list(self, owner: str, token: str, kind: str, limit: int = 100) -> list[dict[str, Any]]:
        if self.local:
            with self.db.connect() as conn:
                rows = conn.execute(
                    "SELECT id, payload, created_at FROM qe_alpha_records "
                    "WHERE owner=? AND kind=? ORDER BY created_at DESC LIMIT ?",
                    (owner, kind, limit),
                ).fetchall()
            return [{"id": r[0], "payload": json.loads(r[1]), "created_at": r[2]} for r in rows]
        table = self._supabase_table(kind)
        id_col = self._supabase_id_col(kind)
        rows = self.db.request(
            "GET",
            f"/rest/v1/{table}",
            token,
            params={"owner": "eq." + owner, "order": "created_at.desc", "limit": limit},
        ).json()
        return [{"id": str(r.get(id_col, "")), "payload": r, "created_at": r.get("created_at")} for r in rows]

    def upsert_local_or_append(self, owner: str, token: str, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        if self.local:
            try:
                with self.db.connect() as conn:
                    conn.execute(
                        "INSERT OR REPLACE INTO qe_alpha_records VALUES (?,?,?,?,?)",
                        (owner, kind, record_id, json.dumps(payload, allow_nan=False), now_iso),
                    )
            except sqlite3.Error as exc:
                raise Unavailable() from exc
            return
        table = self._supabase_table(kind)
        body = {"owner": owner, **payload}
        try:
            self.db.request("POST", f"/rest/v1/{table}", token, json=body)
        except Conflict:
            return

    def append_immutable(self, owner: str, token: str, kind: str, record_id: str, payload: dict[str, Any]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        if self.local:
            try:
                with self.db.connect() as conn:
                    conn.execute(
                        "INSERT INTO qe_alpha_records VALUES (?,?,?,?,?)",
                        (owner, kind, record_id, json.dumps(payload, allow_nan=False), now_iso),
                    )
            except sqlite3.IntegrityError:
                raise Conflict() from None
            except sqlite3.Error as exc:
                raise Unavailable() from exc
            return
        table = self._supabase_table(kind)
        self.db.request("POST", f"/rest/v1/{table}", token, json={"owner": owner, **payload})

    @staticmethod
    def _supabase_table(kind: str) -> str:
        mapping = {
            "source": "qe_academic_sources",
            "hypothesis": "qe_research_hypotheses",
            "alpha": "qe_alpha_definitions",
            "experiment": "qe_experiment_results",
            "daily_return": "qe_daily_alpha_returns",
            "correlation": "qe_correlation_matrices",
            "allocation": "qe_allocation_history",
            "approval": "qe_validation_approvals",
        }
        return mapping.get(kind, "qe_research_records")

    @staticmethod
    def _supabase_id_col(kind: str) -> str:
        mapping = {
            "source": "source_id",
            "hypothesis": "hypothesis_id",
            "alpha": "alpha_id",
            "experiment": "experiment_id",
            "correlation": "matrix_id",
            "allocation": "allocation_id",
            "approval": "approval_id",
        }
        return mapping.get(kind, "id")
