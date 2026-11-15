"""Shared primitives for all Spore contracts."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SporeModel(BaseModel):
    """Base model: strict extra-field rejection + JSON-schema friendly."""

    model_config = ConfigDict(extra="forbid", use_enum_values=True)


class Metric(str, Enum):
    TEMPERATURE = "temperature"
    HUMIDITY = "humidity"
    CO2 = "co2"
    SUBSTRATE_MOISTURE = "substrate_moisture"
    LIGHT_STATE = "light_state"
    FAN_STATE = "fan_state"
    HEATER_STATE = "heater_state"
    HUMIDIFIER_STATE = "humidifier_state"
    CAMERA_HEALTH = "camera_health"


class Unit(str, Enum):
    C = "C"
    PERCENT = "percent"
    PPM = "ppm"
    G = "g"
    BOOL = "bool"
    RAW = "raw"


METRIC_UNITS: dict[Metric, Unit] = {
    Metric.TEMPERATURE: Unit.C,
    Metric.HUMIDITY: Unit.PERCENT,
    Metric.CO2: Unit.PPM,
    Metric.SUBSTRATE_MOISTURE: Unit.PERCENT,
    Metric.LIGHT_STATE: Unit.BOOL,
    Metric.FAN_STATE: Unit.BOOL,
    Metric.HEATER_STATE: Unit.BOOL,
    Metric.HUMIDIFIER_STATE: Unit.BOOL,
    Metric.CAMERA_HEALTH: Unit.RAW,
}


class QualityFlag(str, Enum):
    """Assigned by the ingestion data-quality gates (Phase 3)."""

    VALID = "valid"
    SUSPECT = "suspect"          # e.g. outside plausible rate-of-change; kept, excluded from training
    QUARANTINED = "quarantined"  # physically impossible value; never reaches optimizer
    STALE = "stale"              # older than sensor_stale_after_seconds
    DUPLICATE = "duplicate"      # rejected duplicate within duplicate_window_seconds


class Timestamped(SporeModel):
    ts: datetime = Field(default_factory=utcnow, description="UTC, ISO-8601")
