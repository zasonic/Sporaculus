"""Phase 2 exit criteria: sent / received / acknowledged / failed / offline."""
from __future__ import annotations

from datetime import timedelta

from conftest import OPERATOR, setpoint_command
from sqlalchemy import select

from mycoloop import db, topics
from mycoloop.models import Command, CommandAckRow
from mycoloop.schemas import CommandState, utcnow


def test_command_is_sent_received_and_applied(client, device):
    r = client.post("/devices/chamber-01/commands", headers=OPERATOR, json=setpoint_command())
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["safety"]["ok"] is True
    command_id = body["command"]["command_id"]
    assert body["command"]["state"] == "applied"

    detail = client.get(f"/commands/{command_id}", headers=OPERATOR).json()
    assert [a["state"] for a in detail["acks"]] == ["received", "applied"]
    assert command_id in device.applied
    assert device.chamber.setpoints.temp_c == 19.0

    # the wire contract was actually used
    published = [m.topic for m in device.bus.published]
    assert topics.commands("chamber-01") in published
    assert topics.ack("chamber-01", command_id) in published


def test_idempotency_key_replay_never_actuates_twice(client, device):
    first = client.post("/devices/chamber-01/commands", headers=OPERATOR,
                        json=setpoint_command(key="idem-key-12345")).json()
    second = client.post("/devices/chamber-01/commands", headers=OPERATOR,
                         json=setpoint_command(key="idem-key-12345")).json()
    assert second["idempotent_replay"] is True
    assert first["command"]["command_id"] == second["command"]["command_id"]
    assert len(device.applied) == 1
    with db.session_scope() as s:
        assert len(list(s.scalars(select(Command)))) == 1


def test_idempotency_key_reuse_with_different_payload_conflicts(client, device):
    client.post("/devices/chamber-01/commands", headers=OPERATOR, json=setpoint_command(key="idem-key-99999"))
    r = client.post("/devices/chamber-01/commands", headers=OPERATOR,
                    json=setpoint_command(key="idem-key-99999", temp_c=20.0))
    assert r.status_code == 409


def test_device_reports_failure_for_welded_relay(client, device):
    device.set_fault("heater_stuck")
    r = client.post("/devices/chamber-01/commands", headers=OPERATOR, json={
        "kind": "actuator", "payload": {"actuator": "heater", "state": False},
        "idempotency_key": "stuck-relay-001", "ttl_s": 60,
    })
    assert r.status_code == 201
    cmd = r.json()["command"]
    assert cmd["state"] == "failed"
    assert "stuck" in (cmd["last_error"] or "")
    detail = client.get(f"/commands/{cmd['command_id']}", headers=OPERATOR).json()
    assert [a["state"] for a in detail["acks"]] == ["received", "failed"]


def test_safety_veto_is_persisted_and_never_published(client, device):
    before = len(device.bus.published)
    r = client.post("/devices/chamber-01/commands", headers=OPERATOR,
                    json=setpoint_command(key="unsafe-sp-0001", temp_c=35.0))
    assert r.status_code == 201
    body = r.json()
    assert body["safety"]["ok"] is False
    assert body["command"]["state"] == "rejected_safety"
    assert any(v["rule"] == "temp_range" for v in body["safety"]["violations"])
    # nothing new went out on the command topic
    cmds = [m for m in device.bus.published[before:] if m.topic == topics.commands("chamber-01")]
    assert cmds == []
    audit = client.get("/audit", headers=OPERATOR, params={"action": "command.vetoed"}).json()
    assert len(audit) == 1


def test_partitioned_device_is_offline_and_command_expires(client, device, settings):
    device.tick()  # establish a heartbeat
    device.bus.set_partition("chamber-01", True)
    r = client.post("/devices/chamber-01/commands", headers=OPERATOR,
                    json=setpoint_command(key="offline-cmd-001"))
    cmd = r.json()["command"]
    assert cmd["state"] == "queued"  # transport refused, not silently "sent"

    # age the command past its TTL and the heartbeat past its timeout
    with db.session_scope() as s:
        row = s.get(Command, cmd["command_id"])
        row.expires_at = utcnow() - timedelta(seconds=1)
        from mycoloop.models import Device
        s.get(Device, "chamber-01").last_seen_at = utcnow() - timedelta(seconds=settings.heartbeat_timeout_s + 5)

    sweep = client.post("/gateway/sweep", headers=OPERATOR).json()
    assert cmd["command_id"] in sweep["expired"]
    assert "chamber-01" in sweep["offline"]

    detail = client.get(f"/commands/{cmd['command_id']}", headers=OPERATOR).json()
    assert detail["command"]["state"] == "expired"
    assert client.get("/devices/chamber-01", headers=OPERATOR).json()["status"] == "offline"


def test_retry_republishes_same_command_id_and_device_dedupes(client, device):
    device.bus.set_partition("chamber-01", True)
    cmd = client.post("/devices/chamber-01/commands", headers=OPERATOR,
                      json=setpoint_command(key="retry-cmd-0001")).json()["command"]
    assert len(device.received_commands) == 0

    device.bus.set_partition("chamber-01", False)
    with db.session_scope() as s:
        s.get(Command, cmd["command_id"]).next_retry_at = utcnow() - timedelta(seconds=1)

    sweep = client.post("/gateway/sweep", headers=OPERATOR).json()
    assert cmd["command_id"] in sweep["retried"]
    assert device.received_commands == [cmd["command_id"]]
    with db.session_scope() as s:
        row = s.get(Command, cmd["command_id"])
        assert row.attempts == 2
        assert row.state == CommandState.APPLIED.value

    # a second retry of an already-applied command must be a duplicate ack, not a re-application
    with db.session_scope() as s:
        row = s.get(Command, cmd["command_id"])
        row.state = CommandState.SENT.value
        row.next_retry_at = utcnow() - timedelta(seconds=1)
    client.post("/gateway/sweep", headers=OPERATOR)
    assert device.received_commands.count(cmd["command_id"]) == 2
    with db.session_scope() as s:
        acks = list(s.scalars(select(CommandAckRow).where(CommandAckRow.command_id == cmd["command_id"])))
        assert any(a.duplicate for a in acks)
    assert len(device.applied) == 1


def test_unknown_command_ack_is_audited_not_crashed(client, device):
    device.bus.publish_json(topics.ack("chamber-01", "cmd_does_not_exist"), {
        "command_id": "cmd_does_not_exist", "device_id": "chamber-01", "state": "applied",
        "ts": utcnow().isoformat(),
    })
    audit = client.get("/audit", headers=OPERATOR, params={"action": "command.ack_unknown"}).json()
    assert len(audit) == 1


def test_malformed_telemetry_is_audited_and_rejected(client, device):
    device.bus.publish_json(topics.telemetry("chamber-01"), {"device_id": "chamber-01", "seq": "not-an-int"})
    audit = client.get("/audit", headers=OPERATOR, params={"action": "bus.invalid_payload"}).json()
    assert len(audit) == 1
