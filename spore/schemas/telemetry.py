"""Telemetry contracts (MQTT `device.telemetry` and `device.health` payloads)."""
from __future__ import annotations

import re
from typing import Optional

from pydantic import Field, field_validator

from .common import METRIC_UNITS, Metric, QualityFlag, SporeModel, Timestamped, Unit

DEVICE_ID_RE = re.compile(r"^dev-[a-z0-9-]+$")


class ReadingPayload(SporeModel):
    """One sensor sample inside a telemetry batch (pre data-quality gates)."""

    sensor_id: str = Field(min_length=1, max_length=64)
    metric: Metric
    value: float
    unit: Unit
    local_seq: Optional[int] = Field(default=None, ge=0)

    @field_validator("unit")
    @classmethod
    def _unit_matches_metric(cls, v: Unit, info):
        m = info.data.get("metric")
        if m is not None and METRIC_UNITS[m] != v:
            raise ValueError(f"unit {v.value} invalid for metric {m.value}")
        return v


class TelemetryBatch(Timestamped):
    """Payload of farm/devices/{device_id}/telemetry."""

    device_id: str
    readings: list[ReadingPayload] = Field(min_length=1, max_length=50)

    @field_validator("device_id")
    @classmethod
    def _device_id_shape(cls, v: str) -> str:
        if not DEVICE_ID_RE.match(v):
            raise ValueError("device_id must match ^dev-[a-z0-9-]+$")
        return v


class StoredReading(Timestamped):
    """Row shape of the raw/cleaned telemetry tables (Phase 1/3)."""

    device_id: str
    sensor_id: str
    metric: Metric
    value: float
    unit: Unit
    quality: QualityFlag
    ingest_seq: int = Field(ge=0, description="backend-assigned monotonic sequence")
    calibration_offset: float = 0.0


# Backwards-friendly alias used by the OpenAPI contract.
Reading = StoredReading


class SensorHealth(SporeModel):
    sensor_id: str
    ok: bool
    health_score: float = Field(default=1.0, ge=0, le=1)


class ActuatorHealth(SporeModel):
    actuator_id: str
    kind: str
    state: str = Field(pattern="^(on|off|fault)$")
    runtime_total_s: int = Field(default=0, ge=0)


class DeviceHealthMessage(Timestamped):
    """Payload of farm/devices/{device_id}/health (heartbeat, every 15 s)."""

    device_id: str
    rssi_dbm: Optional[float] = None
    uptime_s: int = Field(ge=0)
    fail_safe_active: bool = False
    sensors: list[SensorHealth]
    actuators: list[ActuatorHealth]
