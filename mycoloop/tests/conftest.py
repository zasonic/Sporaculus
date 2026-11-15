"""Test harness: real app, real bus (in-process), real SQLite, real simulator.

No mocks of the thing under test. The only substitution is the transport
(loopback instead of MQTT) and the database (SQLite instead of Postgres), both of
which sit behind explicit abstraction boundaries.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mycoloop import db
from mycoloop.bus import LoopbackBus
from mycoloop.config import Settings
from mycoloop.main import create_app
from mycoloop.modules import ingestion
from mycoloop.sim import SimChamber, SimDevice

OPERATOR = {"X-API-Key": "test-operator-key"}
ADMIN = {"X-API-Key": "test-admin-key"}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=f"sqlite+pysqlite:///{tmp_path/'test.db'}",
        bus="loopback",
        operator_api_key="test-operator-key",
        admin_api_key="test-admin-key",
        device_token_secret="test-secret",
        background_workers=False,   # sweeps are invoked explicitly -> deterministic
        sim_autostart=False,
        sim_enabled=True,
        timescale_enabled=False,
        create_schema_on_boot=True,
        heartbeat_timeout_s=60,
        command_ttl_s=60,
        command_max_attempts=3,
        command_retry_backoff_s=5,
        dry_run=False,
        envelope_path=Settings().envelope_path,
    )


@pytest.fixture
def bus() -> LoopbackBus:
    return LoopbackBus(inline=True)


@pytest.fixture
def app(settings, bus):
    ingestion.reset_trackers()
    application = create_app(settings, bus=bus)
    yield application
    db.reset_for_tests()


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def device(client, app, bus, settings) -> SimDevice:
    """A registered simulated chamber, stepped manually (no background thread)."""
    r = client.post("/devices", headers=OPERATOR, json={
        "device_id": "chamber-01", "name": "Sim chamber", "edge_stack": "simulated", "firmware": "sim-1.0.0",
    })
    assert r.status_code == 201, r.text
    token = r.json()["token"]
    dev = SimDevice(bus, "chamber-01", token, chamber=SimChamber(seed=7, noise=0.0))
    dev.start()
    app.state.sim_devices["chamber-01"] = dev
    return dev


@pytest.fixture
def token(client) -> str:
    r = client.post("/devices", headers=OPERATOR, json={"device_id": "chamber-02", "name": "Second chamber"})
    assert r.status_code == 201, r.text
    return r.json()["token"]


@pytest.fixture
def biology(client, device):
    """strain -> substrate -> recipe -> cycle, all through the public API."""
    assert client.post("/strains", headers=OPERATOR,
                       json={"strain_id": "blue-oyster", "name": "Blue Oyster", "species": "P. ostreatus"}).status_code == 201
    assert client.post("/substrate-batches", headers=OPERATOR,
                       json={"batch_id": "SB-101", "mass_kg": 2.5}).status_code == 201
    r = client.post("/recipes", headers=OPERATOR, json={
        "recipe_id": "R-07", "name": "Oyster fruiting", "stage": "PINNING_ACTIVE",
        "setpoints": {"temp_c": 19, "humidity_pct": 92, "co2_ceiling_ppm": 900, "fan_pct": 45, "light_pct": 60},
    })
    assert r.status_code == 201, r.text
    rv = r.json()["recipe_version_id"]
    r = client.post("/grow-cycles", headers=OPERATOR, json={
        "cycle_id": "GC-TEST-001", "strain_id": "blue-oyster", "substrate_batch_id": "SB-101",
        "device_id": "chamber-01", "recipe_version_id": rv,
    })
    assert r.status_code == 201, r.text
    return {"cycle_id": "GC-TEST-001", "recipe_id": "R-07", "recipe_version_id": rv, "device_id": "chamber-01"}


def setpoint_command(key: str = "sp-test-0001", **overrides) -> dict:
    payload = {"temp_c": 19.0, "humidity_pct": 92.0, "co2_ceiling_ppm": 900, "fan_pct": 45, "light_pct": 60}
    payload.update(overrides)
    return {"kind": "setpoint", "payload": payload, "idempotency_key": key, "ttl_s": 60}


def advance_to(client, cycle_id: str, stage: str) -> None:
    order = ["COLONIZING", "COLONIZATION_COMPLETE", "PINNING_INITIATED", "PINNING_ACTIVE",
             "MATURATION", "HARVEST_READY", "HARVESTED", "FLUSH_COMPLETE"]
    for s in order:
        r = client.post(f"/grow-cycles/{cycle_id}/transitions", headers=OPERATOR,
                        json={"to_stage": s, "reason": f"test advance to {s}"})
        assert r.status_code == 200, r.text
        if s == stage:
            return
    raise AssertionError(f"stage {stage} not reached")
