"""Tests for Alpha Center dashboard endpoints, authenticated permissions, and isolation."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from quant_engine.dashboard.app import create_app
from quant_engine.dashboard.store import SQLiteStore

TOKEN = "alpha-center-local-test-token-123456789"


@pytest.fixture
def client(tmp_path):
    store = SQLiteStore(tmp_path / "alpha_dash.sqlite3")
    c = TestClient(create_app(store, {"QE_LOCAL_TOKEN": TOKEN}))
    return c


def test_alpha_endpoints_require_auth(client):
    assert client.get("/api/alpha/overview").status_code == 401
    assert client.post("/api/alpha/run-cycle", json={}).status_code == 401
    assert client.post("/api/alpha/preview-allocation", json={"model": "v1"}).status_code == 401
    assert client.post("/api/alpha/budget", json={}).status_code == 401
    assert client.get("/api/alpha/overview", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_alpha_center_cycle_allocation_and_paper_permissions(client):
    headers = {"Authorization": "Bearer " + TOKEN}

    # Initial overview loads catalog and queued hypotheses cleanly
    ov0 = client.get("/api/alpha/overview", headers=headers)
    assert ov0.status_code == 200
    body0 = ov0.json()
    assert body0["execution"] == "PAPER"
    assert body0["execution_active"] is False
    assert len(body0["catalog"]) == 5
    assert len(body0["queue"]) == 5
    assert body0["selected_model"] == "v1"

    # Run controlled research cycle on synthetic demo
    run_res = client.post(
        "/api/alpha/run-cycle",
        headers=headers,
        json={"max_steps": 5, "max_trials_per_hypothesis": 1, "dataset_mode": "synthetic_demo"},
    )
    assert run_res.status_code == 200, run_res.text
    ov = run_res.json()["overview"]
    assert len(ov["alphas"]) == 5
    assert len(ov["experiments"]) == 5
    assert len(ov["failed_experiments"]) == 5  # Synthetic data is always rejected for paper recommendation
    assert len(ov["correlation"]["alpha_ids"]) == 5
    assert set(ov["ensemble_comparison"].keys()) == {"v1", "v2", "v3"}
    assert ov["ensemble_comparison"]["v3"]["research_only"] is True
    assert ov["strategy_contributions"]
    assert ov["regime_performance"]

    # Preview V2 constrained allocation
    prev = client.post(
        "/api/alpha/preview-allocation",
        headers=headers,
        json={"model": "v2", "max_weight": "0.45", "max_turnover": "0.50", "shrinkage": 0.25, "reason": "Review V2"},
    )
    assert prev.status_code == 200
    assert prev.json()["allocation"]["model"] == "v2"

    # Change allocation model through authenticated /api/commands with audit trail
    st = client.get("/api/state", headers=headers).json()
    ver = st["version"]
    cmd = client.post(
        "/api/commands",
        headers=headers,
        json={
            "version": ver,
            "action": "allocation_model",
            "target": "",
            "reason": "Switch research display to V2",
            "values": {"allocation_model": "v2"},
        },
    )
    assert cmd.status_code == 200
    assert cmd.json()["state"]["allocation_model"] == "v2"
    ver = cmd.json()["version"]

    # Invalid allocation model rejected
    bad_cmd = client.post(
        "/api/commands",
        headers=headers,
        json={
            "version": ver,
            "action": "allocation_model",
            "target": "",
            "reason": "Invalid model",
            "values": {"allocation_model": "v9"},
        },
    )
    assert bad_cmd.status_code == 422

    # Register and advance a strategy to human-approved paper stage, then update permissions from Alpha Center
    r = client.post(
        "/api/commands",
        headers=headers,
        json={
            "version": ver,
            "action": "create_strategy",
            "target": "",
            "reason": "Create test paper strategy",
            "values": {"name": "Approved Swing Alpha", "portfolio": "swing", "thesis": "Validated swing alpha"},
        },
    )
    assert r.status_code == 200
    ver = r.json()["version"]
    sid = r.json()["state"]["strategies"][0]["id"]

    # Permissions change must fail BEFORE paper approval
    denied = client.post(
        "/api/commands",
        headers=headers,
        json={
            "version": ver,
            "action": "strategy_permissions",
            "target": sid,
            "reason": "Try early enable",
            "values": {"enabled": True, "regimes": ["bull"]},
        },
    )
    assert denied.status_code == 422

    # Advance through stages to paper approval
    for step_vals in [
        {"stage": "specified"},
        {"stage": "backtested", "evidence": "historical SPY/QQQ 2020-2025", "trials": 3, "costs_modeled": True},
        {"stage": "validated", "held_out_trades": 35},
        {"stage": "paper", "enabled": False},
    ]:
        r = client.post(
            "/api/commands",
            headers=headers,
            json={"version": ver, "action": "strategy", "target": sid, "reason": "Advance stage", "values": step_vals},
        )
        assert r.status_code == 200
        ver = r.json()["version"]

    # Now authenticated operator CAN update approved paper-strategy permissions
    perm = client.post(
        "/api/commands",
        headers=headers,
        json={
            "version": ver,
            "action": "strategy_permissions",
            "target": sid,
            "reason": "Enable for bull and chop regimes",
            "values": {"enabled": True, "regimes": ["bull", "chop"]},
        },
    )
    assert perm.status_code == 200
    updated_strat = perm.json()["state"]["strategies"][0]
    assert updated_strat["enabled"] is True
    assert updated_strat["regimes"] == ["bull", "chop"]
    assert perm.json()["state"]["execution"] == "PAPER"

    # Verify Tax Center, 3 portfolios, and risk defaults preserved
    state_final = perm.json()
    assert {p["id"] for p in state_final["state"]["portfolios"]} == {"day", "swing", "long_term"}
    assert state_final["state"]["tax"]["carryforward"] == "35000"
    assert state_final["risk_limits"]["max_trade_risk_pct"] == "0.25"
    assert state_final["worker_acknowledged_version"] is None
