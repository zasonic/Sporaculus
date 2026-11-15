"""Phase 0 exit criteria: the contracts exist, agree with the code, and are strict."""
from __future__ import annotations

import json

import pytest
import yaml
from conftest import OPERATOR
from pydantic import ValidationError

from mycoloop import topics
from mycoloop.config import CONTRACTS_DIR
from mycoloop.safety import load_envelope
from mycoloop.schemas import CommandCreate, SafetyEnvelope, Setpoints, Telemetry


def test_openapi_covers_the_contract_surface(client):
    spec = client.get("/openapi.json").json()
    assert spec["openapi"].startswith("3.1")
    required = [
        "/devices", "/devices/{device_id}/commands", "/commands/{command_id}", "/telemetry",
        "/telemetry/clean", "/telemetry/quarantine", "/grow-cycles", "/grow-cycles/{cycle_id}/events",
        "/grow-cycles/{cycle_id}/timeline", "/vision/frames", "/optimizer/trials",
        "/optimizer/recommendations/{recommendation_id}/approve", "/audit", "/healthz",
    ]
    for path in required:
        assert path in spec["paths"], path
    assert "Telemetry" in spec["components"]["schemas"]
    assert "CommandCreate" in spec["components"]["schemas"]


def test_asyncapi_channels_match_the_code(client):
    doc = yaml.safe_load((CONTRACTS_DIR / "asyncapi.yaml").read_text())
    assert doc["asyncapi"] == "3.0.0"
    addresses = {c["address"] for c in doc["channels"].values()}
    assert addresses == {topics.STATUS, topics.TELEMETRY, topics.COMMANDS, topics.COMMAND_ACK,
                         topics.HEALTH, topics.BACKEND_STATUS}
    served = yaml.safe_load(client.get("/contracts/asyncapi.yaml").text)
    assert served["info"]["title"] == "MycoLoop Device Bus"
    # every declared message payload schema resolves
    for name, msg in doc["components"]["messages"].items():
        ref = msg["payload"]["$ref"].split("/")[-1]
        assert ref in doc["components"]["schemas"], name


def test_topic_helpers_round_trip():
    assert topics.device_id_from_topic(topics.telemetry("chamber-01")) == "chamber-01"
    assert topics.command_id_from_ack(topics.ack("chamber-01", "cmd_abc")) == "cmd_abc"
    assert topics.device_id_from_topic("farm/backend/status") is None


def test_envelope_file_matches_the_schema_defaults():
    from_file = SafetyEnvelope.model_validate(yaml.safe_load((CONTRACTS_DIR / "safety-envelope.yaml").read_text()))
    assert from_file == SafetyEnvelope()
    assert load_envelope() == from_file
    assert from_file.sensor_invalid_behavior == "fail_safe"


def test_published_json_schemas_are_available(client):
    schemas = client.get("/contracts/schemas.json").json()
    for name in ("Telemetry", "CommandCreate", "CommandAck", "DeviceHealth", "Setpoints",
                 "SafetyEnvelope", "PhenotypeFeatures", "RecommendationRead"):
        assert name in schemas
        assert schemas[name]["type"] == "object"
    assert schemas["Telemetry"]["additionalProperties"] is False


def test_contracts_reject_unknown_fields():
    with pytest.raises(ValidationError):
        Telemetry.model_validate({"device_id": "d", "seq": 1, "ts": "2026-01-01T00:00:00Z", "surprise": 1})
    with pytest.raises(ValidationError):
        Setpoints.model_validate({"temp_c": 19, "humidity_pct": 92, "co2_ceiling_ppm": 900,
                                  "fan_pct": 45, "light_pct": 60, "extra": True})


def test_command_payload_must_match_its_kind():
    with pytest.raises(ValidationError):
        CommandCreate.model_validate({"kind": "setpoint", "payload": {"temp_c": 19}, "idempotency_key": "abcdefgh"})
    with pytest.raises(ValidationError):
        CommandCreate.model_validate({"kind": "actuator", "payload": {"actuator": "laser", "state": True},
                                      "idempotency_key": "abcdefgh"})
    with pytest.raises(ValidationError):
        CommandCreate.model_validate({"kind": "recipe", "payload": {}, "idempotency_key": "abcdefgh"})
    ok = CommandCreate.model_validate({"kind": "reboot", "payload": {}, "idempotency_key": "abcdefgh"})
    assert ok.ttl_s == 60


def test_telemetry_rejects_unknown_actuator():
    with pytest.raises(ValidationError):
        Telemetry.model_validate({"device_id": "d", "seq": 1, "ts": "2026-01-01T00:00:00Z",
                                  "actuators": {"flamethrower": True}})


def test_healthz_reports_real_state(client, device):
    device.tick()
    body = client.get("/healthz").json()
    assert body["status"] == "ok"
    assert body["database"] == "sqlite"
    assert body["bus"] == "loopback"
    assert body["devices_total"] == 1
    assert body["devices_online"] == 1


def test_console_is_self_contained(client):
    html = client.get("/console").text
    assert "<!DOCTYPE html>" in html
    # no CDN, no external fonts, no remote scripts: works offline and in a sandbox
    for forbidden in ("https://cdn.", "http://cdn.", "unpkg.com", "jsdelivr", "googleapis.com"):
        assert forbidden not in html, forbidden
    assert "fetch(" in html


def test_simulator_fault_catalogue_is_exposed(client, device):
    body = client.get("/sim/devices", headers=OPERATOR).json()
    assert "heater_stuck" in body["faults_available"]
    assert body["devices"][0]["device_id"] == "chamber-01"
    r = client.post("/sim/devices/chamber-01/faults", headers=OPERATOR,
                    params={"name": "co2_invalid", "enabled": True}).json()
    assert "co2_invalid" in r["faults"]
