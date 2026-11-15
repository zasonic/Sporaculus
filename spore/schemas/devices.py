"""Device registry contracts."""
from __future__ import annotations

import re
from enum import Enum
from typing import Optional

from pydantic import Field, field_validator

from .common import Metric, SporeModel, Timestamped

DEVICE_ID_RE = re.compile(r"^dev-[a-z0-9-]+$")


class DeviceKind(str, Enum):
    CHAMBER = "chamber"
    INCUBATOR = "incubator"
    TENT = "tent"
    SENSOR_NODE = "sensor_node"


class EdgeStack(str, Enum):
    """Hardware abstraction layer: the backend never assumes a specific edge."""

    MYCODO = "mycodo"
    ESPHOME = "esphome"
    SIMULATOR = "simulator"
    CUSTOM = "custom"


class ActuatorKind(str, Enum):
    HEATER = "heater"
    HUMIDIFIER = "humidifier"
    FAN = "fan"
    DEHUMIDIFIER = "dehumidifier"
    LIGHT = "light"
    MISTING = "misting"


class ActuatorState(str, Enum):
    ON = "on"
    OFF = "off"
    FAULT = "fault"


class ActuatorRef(SporeModel):
    actuator_id: str
    kind: ActuatorKind
    state: ActuatorState = ActuatorState.OFF


class SensorRef(SporeModel):
    sensor_id: str
    metric: Metric
    health_score: float = Field(default=1.0, ge=0, le=1)
    calibration_offset: float = 0.0


class Device(Timestamped):
    device_id: str
    name: str
    kind: DeviceKind
    edge_stack: EdgeStack = EdgeStack.SIMULATOR
    status: str = Field(default="offline", pattern="^(online|offline|degraded)$")
    last_heartbeat: Optional[Timestamped] = None
    sensors: list[SensorRef] = Field(default_factory=list)
    actuators: list[ActuatorRef] = Field(default_factory=list)

    @field_validator("device_id")
    @classmethod
    def _shape(cls, v: str) -> str:
        if not DEVICE_ID_RE.match(v):
            raise ValueError("device_id must match ^dev-[a-z0-9-]+$")
        return v


class DeviceRegisterRequest(SporeModel):
    device_id: str
    name: str
    kind: DeviceKind
    edge_stack: EdgeStack = EdgeStack.SIMULATOR

    @field_validator("device_id")
    @classmethod
    def _shape(cls, v: str) -> str:
        if not DEVICE_ID_RE.match(v):
            raise ValueError("device_id must match ^dev-[a-z0-9-]+$")
        return v


class DeviceRegistered(Device):
    auth_token: str = Field(description="Shown exactly once at registration.")
