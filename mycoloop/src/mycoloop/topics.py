"""Canonical MQTT topic contract (mirrors contracts/asyncapi.yaml).

Every topic string in the codebase comes from here — a topic typo becomes an
AttributeError instead of a silently undelivered command.
"""
from __future__ import annotations

PREFIX = "farm"

STATUS = "farm/devices/{device_id}/status"
TELEMETRY = "farm/devices/{device_id}/telemetry"
COMMANDS = "farm/devices/{device_id}/commands"
COMMAND_ACK = "farm/devices/{device_id}/commands/{command_id}/ack"
HEALTH = "farm/devices/{device_id}/health"
BACKEND_STATUS = "farm/backend/status"

SUB_STATUS = "farm/devices/+/status"
SUB_TELEMETRY = "farm/devices/+/telemetry"
SUB_ACK = "farm/devices/+/commands/+/ack"
SUB_HEALTH = "farm/devices/+/health"


def status(device_id: str) -> str:
    return STATUS.format(device_id=device_id)


def telemetry(device_id: str) -> str:
    return TELEMETRY.format(device_id=device_id)


def commands(device_id: str) -> str:
    return COMMANDS.format(device_id=device_id)


def ack(device_id: str, command_id: str) -> str:
    return COMMAND_ACK.format(device_id=device_id, command_id=command_id)


def health(device_id: str) -> str:
    return HEALTH.format(device_id=device_id)


def device_id_from_topic(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) >= 3 and parts[0] == PREFIX and parts[1] == "devices":
        return parts[2]
    return None


def command_id_from_ack(topic: str) -> str | None:
    parts = topic.split("/")
    if len(parts) == 6 and parts[-1] == "ack":
        return parts[4]
    return None
