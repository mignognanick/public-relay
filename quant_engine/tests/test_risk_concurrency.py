"""Concurrent entry requests must never spend the same risk headroom twice."""
import multiprocessing
import threading
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from quant_engine.risk import ExitRequest, FileStateStore, InMemoryStateStore, Reason
from quant_engine.tests.support import make_engine, option_entry, snapshot
from quant_engine.tests.workers import submit_entries

# 1 contract at 1.20 risks 120. The 0.50% aggregate on 100k is 500, so exactly 4 fit.
EXPECTED_APPROVALS = 4
AGGREGATE_LIMIT = Decimal("500")


def _race(engine, ids, requested_qty=1):
    barrier = threading.Barrier(len(ids))

    def submit(request_id):
        barrier.wait()
        return engine.evaluate_entry(option_entry(request_id, requested_qty=requested_qty), snapshot())

    with ThreadPoolExecutor(max_workers=len(ids)) as pool:
        return list(pool.map(submit, ids))


@pytest.mark.parametrize("store_kind", ["memory", "file"])
def test_concurrent_entries_never_exceed_aggregate_risk(tmp_path, store_kind):
    store = InMemoryStateStore() if store_kind == "memory" else FileStateStore(tmp_path / "risk.json")
    engine = make_engine(store=store)
    decisions = _race(engine, [f"t{n}" for n in range(32)])
    approved = [d for d in decisions if d.approved]
    assert len(approved) == EXPECTED_APPROVALS
    assert sum(d.planned_risk for d in approved) <= AGGREGATE_LIMIT
    assert all(d.reasons == (Reason.AGGREGATE_RISK_LIMIT,) for d in decisions if not d.approved)
    status = engine.status()
    assert set(status["positions"]) == {d.request_id for d in approved}
    assert Decimal(status["open_risk"]) == sum(d.planned_risk for d in approved)


def test_concurrent_full_size_requests_share_the_headroom():
    engine = make_engine()
    decisions = _race(engine, [f"f{n}" for n in range(16)], requested_qty=5)
    approved = [d for d in decisions if d.approved]
    assert [d.qty for d in approved] == [2, 2]
    assert Decimal(engine.status()["open_risk"]) == Decimal("480.00")


def test_concurrent_duplicates_reserve_once():
    engine = make_engine()
    decisions = _race(engine, ["same"] * 20)
    assert all(d.approved and d.qty == 1 for d in decisions)
    assert sum(1 for d in decisions if not d.detail.get("duplicate")) == 1
    assert engine.status()["open_risk"] == "120.00"


def test_entries_from_separate_processes_share_one_file_state(tmp_path):
    path = str(tmp_path / "risk.json")
    batches = [[f"p{worker}-{n}" for n in range(8)] for worker in range(4)]
    context = multiprocessing.get_context("spawn")
    with context.Pool(processes=4) as pool:
        results = [row for batch in pool.starmap(submit_entries, [(path, ids) for ids in batches]) for row in batch]
    approved = [row for row in results if row[1]]
    assert len(results) == 32
    assert len(approved) == EXPECTED_APPROVALS
    status = make_engine(store=FileStateStore(path)).status()
    assert set(status["positions"]) == {row[0] for row in approved}
    assert status["open_risk"] == "480.00"


def test_exits_stay_approved_while_entries_race_and_the_kill_switch_trips():
    engine = make_engine()
    for name in ("held-a", "held-b"):
        engine.evaluate_entry(option_entry(name, requested_qty=1), snapshot())
        engine.confirm_entry_fill(name, 1)
    barrier = threading.Barrier(41)

    def entry(n):
        barrier.wait()
        return engine.evaluate_entry(option_entry(f"r{n}", requested_qty=1), snapshot())

    def exit_(n):
        barrier.wait()
        return engine.evaluate_exit(ExitRequest("held-a" if n % 2 else "held-b"))

    def trip():
        barrier.wait()
        return engine.engage_kill_switch("race test", "nick")

    with ThreadPoolExecutor(max_workers=41) as pool:
        entries = [pool.submit(entry, n) for n in range(20)]
        exits = [pool.submit(exit_, n) for n in range(20)]
        pool.submit(trip).result()
        entry_decisions = [f.result() for f in entries]
        exit_decisions = [f.result() for f in exits]
    assert all(d.approved and d.qty == 1 for d in exit_decisions)
    assert sum(1 for d in entry_decisions if d.approved) <= 2
    assert not engine.evaluate_entry(option_entry("after"), snapshot()).approved
    assert all(engine.evaluate_exit(ExitRequest(name)).approved for name in ("held-a", "held-b"))


def test_kill_switch_engaged_before_a_burst_blocks_every_entry():
    engine = make_engine()
    engine.engage_kill_switch("halt", "nick")
    decisions = _race(engine, [f"k{n}" for n in range(16)])
    assert all(d.reasons == (Reason.KILL_SWITCH,) for d in decisions)
    assert engine.status()["positions"] == {}
