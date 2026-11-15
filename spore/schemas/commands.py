"""Command bus contracts: idempotent backend->edge commands and acks."""
from __future__ import annotations

from enum import Enum
from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import Field

from .common import SporeModel, Timestamped, utcnow


class CommandAction(str, Enum):
    SET_SETPOINT = "set_setpoint"
    SET_ACTUATOR = "set_actuator"
    STOP_ALL = "stop_all"
    ENTER_FAIL_SAFE = "enter_fail_safe"
    CLEAR_FAIL_SAFE = "clear_fail_safe"
    START_RECIPE = "start_recipe"
    STOP_RECIPE = "stop_recipe"


class CommandStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    RECEIVED = "RECEIVED"
    ACKED = "ACKED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    REJECTED_BY_SAFETY = "REJECTED_BY_SAFETY"


TERMINAL_STATUSES = {
    CommandStatus.ACKED,
    CommandStatus.FAILED,
    CommandStatus.TIMED_OUT,
    CommandStatus.REJECTED_BY_SAFETY,
}


class CommandCreate(SporeModel):
    """REST body for POST /devices/{device_id}/commands."""

    action: CommandAction
    payload: dict[str, Any] = Field(default_factory=dict)
    issued_by: str = Field(min_length=1, max_length=128)
    dry_run: bool = False


class CommandMessage(Timestamped):
    """MQTT payload for farm/devices/{device_id}/commands."""

    command_id: UUID = Field(default_factory=uuid4)
    action: CommandAction
    payload: dict[str, Any] = Field(default_factory=dict)
    ttl_seconds: int = Field(default=30, ge=1, le=600)


class AckMessage(Timestamped):
    """MQTT payload for .../commands/{command_id}/ack."""

    command_id: UUID
    success: bool
    reason: Optional[str] = None
    resulting_state: dict[str, Any] = Field(default_factory=dict)

    def __init__(self, **data: Any) -> None:
        # contract name is executed_at; map to base ts field
        if "executed_at" in data:
            data["ts"] = data.pop("executed_at")
        super().__init__(**data)

    @property
    def executed_at(self):
        return self.ts


class CommandRecord(SporeModel):
    """Backend-side lifecycle record (source of truth for command state)."""

    command_id: UUID = Field(default_factory=uuid4)
    device_id: str
    idempotency_key: str = Field(min_length=8, max_length=128)
    action: CommandAction
    payload: dict[str, Any] = Field(default_factory=dict)
    status: CommandStatus = CommandStatus.PENDING
    issued_by: str = "system"
    created_at: Any = Field(default_factory=utcnow)
    updated_at: Any = Field(default_factory=utcnow)
    ack: Optional[AckMessage] = None
