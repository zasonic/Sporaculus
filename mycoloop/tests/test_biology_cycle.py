"""Phase 1 + Phase 5 exit criteria: a whole cycle through the API, reconstructable."""
from __future__ import annotations

import pytest
from conftest import ADMIN, OPERATOR, advance_to
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from mycoloop import db
from mycoloop.modules.biology import FORWARD, allowed_transitions
from mycoloop.schemas import Stage


def test_full_cycle_through_the_api(client, device, biology):
    cycle_id = biology["cycle_id"]
    for _ in range(3):
        device.tick()
    advance_to(client, cycle_id, "HARVEST_READY")

    r = client.post(f"/grow-cycles/{cycle_id}/harvests", headers=OPERATOR,
                    json={"grams": 412.5, "flush": 1, "quality": "A", "notes": "tight cluster"})
    assert r.status_code == 201, r.text
    cycle = client.get(f"/grow-cycles/{cycle_id}", headers=OPERATOR).json()
    assert cycle["stage"] == "HARVESTED"
    assert cycle["yield_g"] == 412.5

    # next flush increments the flush counter
    assert client.post(f"/grow-cycles/{cycle_id}/transitions", headers=OPERATOR,
                       json={"to_stage": "FLUSH_COMPLETE", "reason": "substrate rested"}).status_code == 200
    assert client.post(f"/grow-cycles/{cycle_id}/transitions", headers=OPERATOR,
                       json={"to_stage": "PINNING_INITIATED", "reason": "second flush"}).status_code == 200
    assert client.get(f"/grow-cycles/{cycle_id}", headers=OPERATOR).json()["flush"] == 2

    timeline = client.get(f"/grow-cycles/{cycle_id}/timeline", headers=OPERATOR).json()
    kinds = {e["kind"] for e in timeline["events"]}
    assert {"stage", "harvest"} <= kinds
    assert timeline["cycle"]["strain_id"] == "blue-oyster"
    assert timeline["cycle"]["substrate_batch_id"] == "SB-101"
    assert timeline["harvests"][0]["grams"] == 412.5
    assert any(s["stage"] == "PINNING_ACTIVE" for s in timeline["environment_by_stage"])
    # events are chronological
    stamps = [e["ts"] for e in timeline["events"]]
    assert stamps == sorted(stamps)


def test_illegal_transition_is_refused(client, biology):
    r = client.post(f"/grow-cycles/{biology['cycle_id']}/transitions", headers=OPERATOR,
                    json={"to_stage": "HARVEST_READY", "reason": "skip the biology"})
    assert r.status_code == 409
    assert "illegal transition" in r.json()["detail"]


def test_terminal_stage_is_terminal(client, biology):
    cycle_id = biology["cycle_id"]
    assert client.post(f"/grow-cycles/{cycle_id}/contamination", headers=OPERATOR,
                       json={"kind": "trichoderma", "severity": "systemic", "notes": "green patch"}).status_code == 200
    r = client.post(f"/grow-cycles/{cycle_id}/transitions", headers=OPERATOR,
                    json={"to_stage": "COLONIZING", "reason": "wishful thinking"})
    assert r.status_code == 409
    cycle = client.get(f"/grow-cycles/{cycle_id}", headers=OPERATOR).json()
    assert cycle["contaminated"] is True
    assert cycle["ended_at"] is not None


def test_harvest_from_wrong_stage_is_refused(client, biology):
    r = client.post(f"/grow-cycles/{biology['cycle_id']}/harvests", headers=OPERATOR, json={"grams": 100})
    assert r.status_code == 409


def test_stage_machine_is_a_single_connected_chain():
    stage = Stage.INOCULATED
    seen = [stage]
    while FORWARD.get(stage) and stage != Stage.FLUSH_COMPLETE:
        stage = FORWARD[stage]
        seen.append(stage)
    assert seen == [
        Stage.INOCULATED, Stage.COLONIZING, Stage.COLONIZATION_COMPLETE, Stage.PINNING_INITIATED,
        Stage.PINNING_ACTIVE, Stage.MATURATION, Stage.HARVEST_READY, Stage.HARVESTED, Stage.FLUSH_COMPLETE,
    ]
    assert allowed_transitions(Stage.CONTAMINATED) == set()
    assert Stage.CONTAMINATED in allowed_transitions(Stage.PINNING_ACTIVE)


def test_ledger_is_append_only_at_the_database(client, biology):
    """The guarantee is enforced by the DB, not by good intentions in the service."""
    from mycoloop.models import StageEvent
    from sqlalchemy import select

    with db.session_scope() as s:
        event_id = s.scalars(select(StageEvent).limit(1)).first().event_id
    with pytest.raises((DBAPIError, IntegrityError)):
        with db.session_scope() as s:
            s.execute(text("UPDATE stage_events SET reason='rewritten history' WHERE event_id=:i"), {"i": event_id})
    with pytest.raises((DBAPIError, IntegrityError)):
        with db.session_scope() as s:
            s.execute(text("DELETE FROM audit_logs"))


def test_annotations_are_attributed(client, biology):
    cycle_id = biology["cycle_id"]
    r = client.post(f"/grow-cycles/{cycle_id}/annotations", headers=OPERATOR, json={"body": "pins look even"})
    assert r.status_code == 201
    assert r.json()["actor"] == "operator"
    audit = client.get("/audit", headers=OPERATOR, params={"action": "cycle.annotate"}).json()
    assert audit[0]["actor"] == "operator"


def test_unknown_references_are_refused(client, device):
    r = client.post("/grow-cycles", headers=OPERATOR, json={
        "strain_id": "nope", "substrate_batch_id": "SB-101", "device_id": "chamber-01",
        "recipe_version_id": "rv_nope",
    })
    assert r.status_code == 400


def test_operator_key_cannot_rotate_device_tokens(client, device):
    assert client.post("/devices/chamber-01/token", headers=OPERATOR).status_code == 403
    assert client.post("/devices/chamber-01/token", headers=ADMIN).status_code == 200
    assert client.get("/devices", headers={"X-API-Key": "wrong"}).status_code == 401
