"""Process-pool entry points. Kept in a module so a spawned interpreter can import them."""
from __future__ import annotations

from quant_engine.risk import FileStateStore
from quant_engine.tests.support import make_engine, option_entry, snapshot


def submit_entries(path: str, ids: list[str]) -> list[tuple[str, bool, int]]:
    engine = make_engine(store=FileStateStore(path))
    results = []
    for request_id in ids:
        decision = engine.evaluate_entry(option_entry(request_id, requested_qty=1), snapshot())
        results.append((request_id, decision.approved, decision.qty or 0))
    return results
