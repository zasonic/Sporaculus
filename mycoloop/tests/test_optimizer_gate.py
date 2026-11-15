"""Phase 7 gate: the optimizer recommends; a human approves; safety may veto."""
from __future__ import annotations

import inspect

from conftest import OPERATOR, advance_to

from mycoloop.modules import optimizer


def completed_cycle(client, device, biology, cycle_id: str, grams: float, *, contaminate: bool = False):
    advance_to(client, cycle_id, "HARVEST_READY")
    assert client.post(f"/grow-cycles/{cycle_id}/harvests", headers=OPERATOR,
                       json={"grams": grams, "flush": 1}).status_code == 201


def test_recommendation_is_grounded_and_pending(client, device, biology):
    completed_cycle(client, device, biology, biology["cycle_id"], 480)
    r = client.post("/optimizer/recommendations", headers=OPERATOR,
                    params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"})
    assert r.status_code == 201, r.text
    rec = r.json()
    assert rec["state"] == "pending"
    assert rec["based_on_cycles"] == 1
    assert rec["confidence"] in {"low", "medium", "high"}
    assert "half-step" in rec["reason"]
    assert rec["expected_effect"]["model"] == "heuristic-halfstep-v0"
    assert set(rec["delta"]) == {"temp_c", "humidity_pct", "fan_pct", "co2_ceiling_ppm"}
    # nothing was actuated by recommending
    assert device.received_commands == []


def test_unsafe_recommendation_cannot_be_approved(client, device, biology):
    completed_cycle(client, device, biology, biology["cycle_id"], 480)
    r = client.post("/optimizer/recommendations", headers=OPERATOR,
                    params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"},
                    json={"temp_min": 33.0, "temp_max": 34.0, "rh_min": 86, "rh_max": 96,
                          "co2_max": 1400, "fae_min": 35, "fae_max": 90})
    rec = r.json()
    assert rec["violations"], rec
    versions_before = len(client.get("/recipes", headers=OPERATOR).json())
    commands_before = len(device.received_commands)

    blocked = client.post(f"/optimizer/recommendations/{rec['recommendation_id']}/approve", headers=OPERATOR, json={})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["violations"]
    assert len(client.get("/recipes", headers=OPERATOR).json()) == versions_before
    assert len(device.received_commands) == commands_before
    audit = client.get("/audit", headers=OPERATOR, params={"action": "optimizer.approve_blocked"}).json()
    assert len(audit) == 1


def test_contamination_risk_is_a_real_constraint(client, device, biology):
    # a mostly-contaminated history plus high humidity target trips the risk constraint
    completed_cycle(client, device, biology, biology["cycle_id"], 400)
    client.post(f"/grow-cycles/{biology['cycle_id']}/contamination", headers=OPERATOR,
                json={"kind": "trichoderma", "severity": "systemic"})
    r = client.post("/optimizer/recommendations", headers=OPERATOR,
                    params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"},
                    json={"temp_min": 16, "temp_max": 22, "rh_min": 96, "rh_max": 96,
                          "co2_max": 1400, "fae_min": 35, "fae_max": 90})
    rec = r.json()
    assert any(v["rule"] == "contamination_risk" for v in rec["violations"])


def test_human_approval_creates_version_and_dispatches(client, device, biology):
    completed_cycle(client, device, biology, biology["cycle_id"], 480)
    rec = client.post("/optimizer/recommendations", headers=OPERATOR,
                      params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"},
                      json={"temp_min": 16, "temp_max": 20, "rh_min": 88, "rh_max": 93,
                            "co2_max": 1200, "fae_min": 40, "fae_max": 70}).json()
    assert rec["violations"] == []

    r = client.post(f"/optimizer/recommendations/{rec['recommendation_id']}/approve", headers=OPERATOR,
                    json={"note": "looks reasonable for flush 2"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "approved"
    assert body["applied"]["applied"] is True

    versions = client.get("/recipes", headers=OPERATOR, params={"recipe_id": biology["recipe_id"]}).json()
    assert len(versions) == 2
    assert versions[0]["approved_by"] == "operator"
    assert versions[0]["parent_version_id"] == biology["recipe_version_id"]
    assert "optimizer" in versions[0]["change_reason"]

    # the recipe reached the device through the command bus, not around it
    assert device.chamber.setpoints.temp_c == rec["params"]["temp_c"]
    actions = [a["action"] for a in client.get("/audit", headers=OPERATOR).json()]
    assert "optimizer.recommend" in actions and "optimizer.approve" in actions
    assert "command.dispatch" in actions


def test_reject_and_double_decision_are_handled(client, device, biology):
    rec = client.post("/optimizer/recommendations", headers=OPERATOR,
                      params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"}).json()
    assert client.post(f"/optimizer/recommendations/{rec['recommendation_id']}/reject", headers=OPERATOR,
                       json={"note": "not this flush"}).status_code == 200
    assert client.post(f"/optimizer/recommendations/{rec['recommendation_id']}/approve", headers=OPERATOR,
                       json={}).status_code == 409


def test_rollback_restores_previous_setpoints(client, device, biology):
    rec = client.post("/optimizer/recommendations", headers=OPERATOR,
                      params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"},
                      json={"temp_min": 16, "temp_max": 18, "rh_min": 88, "rh_max": 92,
                            "co2_max": 1200, "fae_min": 40, "fae_max": 70}).json()
    client.post(f"/optimizer/recommendations/{rec['recommendation_id']}/approve", headers=OPERATOR, json={})
    assert device.chamber.setpoints.temp_c != 19.0

    r = client.post("/optimizer/rollback", headers=OPERATOR,
                    params={"recipe_id": biology["recipe_id"], "device_id": "chamber-01"},
                    json={"reason": "pins aborted after change"})
    assert r.status_code == 200, r.text
    assert r.json()["rolled_back_to"] == biology["recipe_version_id"]
    assert device.chamber.setpoints.temp_c == 19.0
    audit = client.get("/audit", headers=OPERATOR, params={"action": "optimizer.rollback"}).json()
    assert len(audit) == 1


def test_optimizer_module_has_no_direct_hardware_path():
    """Architectural invariant, enforced as a test rather than a comment."""
    src = inspect.getsource(optimizer)
    assert "bus.publish" not in src
    assert "dispatcher.dispatch" not in src
    assert "CommandCreate" not in src  # it cannot even construct a command
    # it reaches hardware only by asking the control module, after approval
    assert "control.apply_recipe" in src


def test_trials_record_constraint_outcomes(client, device):
    r = client.post("/optimizer/trials", headers=OPERATOR, json={
        "params": {"temp_c": 34, "humidity_pct": 92, "co2_ceiling_ppm": 900, "fan_pct": 45, "light_pct": 60},
        "objective": 55.0, "constraints_ok": True,
    })
    assert r.status_code == 201
    assert r.json()["constraints_ok"] is False  # envelope overrides the caller's optimism
