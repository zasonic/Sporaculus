"""Phase 1 domain model — SQLAlchemy 2.x.

One relational source of truth. ``telemetry_clean`` becomes a TimescaleDB
hypertable on PostgreSQL (see migrations); on SQLite it is a plain table so the
whole stack runs with zero external services.

Append-only tables (audit_logs, stage_events, command_acks, telemetry_raw) are
protected by DB triggers created in ``db.install_append_only_guards``.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .schemas import CommandState, DeviceStatus, Stage, utcnow


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


JsonType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JsonType, list: JsonType}


def ts_col() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


# ---------------------------------------------------------------------------
# devices / hardware
# ---------------------------------------------------------------------------
class Device(Base):
    __tablename__ = "devices"
    device_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    edge_stack: Mapped[str] = mapped_column(String(32), default="simulated")
    firmware: Mapped[str] = mapped_column(String(32), default="0.0.0")
    token_hash: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default=DeviceStatus.UNKNOWN.value)
    watchdog_state: Mapped[str] = mapped_column(String(16), default="armed")
    registered_at: Mapped[datetime] = ts_col()
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)

    sensors: Mapped[list["Sensor"]] = relationship(back_populates="device", cascade="all, delete-orphan")
    actuators: Mapped[list["Actuator"]] = relationship(back_populates="device", cascade="all, delete-orphan")


class Sensor(Base):
    __tablename__ = "sensors"
    sensor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    unit: Mapped[str] = mapped_column(String(16), default="")
    calibration_offset: Mapped[float] = mapped_column(Float, default=0.0)
    calibration_scale: Mapped[float] = mapped_column(Float, default=1.0)
    health_score: Mapped[float] = mapped_column(Float, default=1.0)
    valid_count: Mapped[int] = mapped_column(Integer, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    device: Mapped[Device] = relationship(back_populates="sensors")


class Actuator(Base):
    __tablename__ = "actuators"
    actuator_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[bool] = mapped_column(Boolean, default=False)
    runtime_s: Mapped[float] = mapped_column(Float, default=0.0)
    updated_at: Mapped[datetime] = ts_col()
    device: Mapped[Device] = relationship(back_populates="actuators")


class DeviceHealthRow(Base):
    __tablename__ = "device_health"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    ts: Mapped[datetime] = ts_col()
    uptime_s: Mapped[int] = mapped_column(Integer, default=0)
    firmware: Mapped[str] = mapped_column(String(32), default="")
    sensor_health: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    watchdog_state: Mapped[str] = mapped_column(String(16), default="armed")
    watchdog_remaining_s: Mapped[int | None] = mapped_column(Integer)
    safe_state: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    faults: Mapped[list] = mapped_column(JsonType, default=list)


# ---------------------------------------------------------------------------
# command bus
# ---------------------------------------------------------------------------
class Command(Base):
    __tablename__ = "commands"
    __table_args__ = (
        UniqueConstraint("device_id", "idempotency_key", name="uq_command_idempotency"),
        Index("ix_commands_state_expires", "state", "expires_at"),
    )
    command_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("cmd"))
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    idempotency_key: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(20), default=CommandState.QUEUED.value)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    issued_by: Mapped[str] = mapped_column(String(80), default="system")
    issued_at: Mapped[datetime] = ts_col()
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)


class CommandAckRow(Base):
    """Append-only acknowledgement journal: sent -> received -> applied|failed."""

    __tablename__ = "command_acks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    command_id: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str | None] = mapped_column(Text)
    duplicate: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = ts_col()


# ---------------------------------------------------------------------------
# telemetry (raw / clean / quarantine)
# ---------------------------------------------------------------------------
class TelemetryRaw(Base):
    __tablename__ = "telemetry_raw"
    __table_args__ = (UniqueConstraint("device_id", "seq", name="uq_telemetry_device_seq"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ingested_at: Mapped[datetime] = ts_col()
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    quality_flags: Mapped[list] = mapped_column(JsonType, default=list)
    accepted: Mapped[bool] = mapped_column(Boolean, default=True)
    source: Mapped[str] = mapped_column(String(16), default="mqtt")


class TelemetryClean(Base):
    __tablename__ = "telemetry_clean"
    __table_args__ = (Index("ix_clean_device_ts", "device_id", "ts"), Index("ix_clean_cycle_stage", "cycle_id", "stage"))
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64))
    raw_id: Mapped[int | None] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    temperature_c: Mapped[float | None] = mapped_column(Float)
    humidity_pct: Mapped[float | None] = mapped_column(Float)
    co2_ppm: Mapped[float | None] = mapped_column(Float)
    substrate_moisture_pct: Mapped[float | None] = mapped_column(Float)
    fan_pct: Mapped[int | None] = mapped_column(Integer)
    light_pct: Mapped[int | None] = mapped_column(Integer)
    actuators: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    quality_flags: Mapped[list] = mapped_column(JsonType, default=list)
    quality_score: Mapped[float] = mapped_column(Float, default=1.0)
    validated: Mapped[bool] = mapped_column(Boolean, default=True)
    cycle_id: Mapped[str | None] = mapped_column(String(64))
    stage: Mapped[str | None] = mapped_column(String(32))


class TelemetryQuarantine(Base):
    __tablename__ = "telemetry_quarantine"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    quarantined_at: Mapped[datetime] = ts_col()
    reasons: Mapped[list] = mapped_column(JsonType, default=list)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


# ---------------------------------------------------------------------------
# biology ledger
# ---------------------------------------------------------------------------
class Strain(Base):
    __tablename__ = "strains"
    strain_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    species: Mapped[str | None] = mapped_column(String(120))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_col()


class SubstrateBatch(Base):
    __tablename__ = "substrate_batches"
    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    substrate_type: Mapped[str] = mapped_column(String(80), default="masters mix")
    sterilization: Mapped[str] = mapped_column(String(120), default="")
    mass_kg: Mapped[float] = mapped_column(Float, default=2.5)
    notes: Mapped[str | None] = mapped_column(Text)
    prepared_at: Mapped[datetime] = ts_col()


class Recipe(Base):
    __tablename__ = "recipes"
    recipe_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = ts_col()
    versions: Mapped[list["RecipeVersion"]] = relationship(back_populates="recipe", cascade="all, delete-orphan")


class RecipeVersion(Base):
    __tablename__ = "recipe_versions"
    __table_args__ = (UniqueConstraint("recipe_id", "version", name="uq_recipe_version"),)
    recipe_version_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rv"))
    recipe_id: Mapped[str] = mapped_column(ForeignKey("recipes.recipe_id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(32), default=Stage.PINNING_ACTIVE.value)
    setpoints: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    created_by: Mapped[str] = mapped_column(String(80), default="operator")
    approved_by: Mapped[str | None] = mapped_column(String(80))
    change_reason: Mapped[str | None] = mapped_column(Text)
    parent_version_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = ts_col()
    recipe: Mapped[Recipe] = relationship(back_populates="versions")


class GrowCycle(Base):
    __tablename__ = "grow_cycles"
    cycle_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    strain_id: Mapped[str] = mapped_column(ForeignKey("strains.strain_id"))
    substrate_batch_id: Mapped[str] = mapped_column(ForeignKey("substrate_batches.batch_id"))
    device_id: Mapped[str] = mapped_column(ForeignKey("devices.device_id"))
    recipe_version_id: Mapped[str] = mapped_column(ForeignKey("recipe_versions.recipe_version_id"))
    stage: Mapped[str] = mapped_column(String(32), default=Stage.INOCULATED.value)
    flush: Mapped[int] = mapped_column(Integer, default=1)
    yield_g: Mapped[float] = mapped_column(Float, default=0.0)
    contaminated: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = ts_col()
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StageEvent(Base):
    """Append-only. Never updated, never deleted."""

    __tablename__ = "stage_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ev"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("grow_cycles.cycle_id", ondelete="CASCADE"), index=True)
    from_stage: Mapped[str | None] = mapped_column(String(32))
    to_stage: Mapped[str] = mapped_column(String(32))
    actor: Mapped[str] = mapped_column(String(80))
    reason: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = ts_col()


class Harvest(Base):
    __tablename__ = "harvests"
    harvest_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("hv"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("grow_cycles.cycle_id", ondelete="CASCADE"), index=True)
    grams: Mapped[float] = mapped_column(Float)
    flush: Mapped[int] = mapped_column(Integer, default=1)
    quality: Mapped[str | None] = mapped_column(String(64))
    notes: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="operator")
    ts: Mapped[datetime] = ts_col()


class ContaminationEvent(Base):
    __tablename__ = "contamination_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("ct"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("grow_cycles.cycle_id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(48), default="unknown")
    severity: Mapped[str] = mapped_column(String(24), default="localized")
    notes: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="operator")
    ts: Mapped[datetime] = ts_col()


class Annotation(Base):
    __tablename__ = "operator_annotations"
    annotation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("an"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("grow_cycles.cycle_id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="operator")
    ts: Mapped[datetime] = ts_col()


# ---------------------------------------------------------------------------
# vision
# ---------------------------------------------------------------------------
class VisionModel(Base):
    __tablename__ = "vision_models"
    model_version: Mapped[str] = mapped_column(String(80), primary_key=True)
    family: Mapped[str] = mapped_column(String(32), default="yolo")
    dataset: Mapped[str | None] = mapped_column(String(120))
    metrics: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    edge_runtime: Mapped[str] = mapped_column(String(32), default="onnxruntime")
    registered_at: Mapped[datetime] = ts_col()


class VisionFrame(Base):
    __tablename__ = "vision_frames"
    frame_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("fr"))
    cycle_id: Mapped[str] = mapped_column(ForeignKey("grow_cycles.cycle_id", ondelete="CASCADE"), index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str] = mapped_column(String(32), default=Stage.INOCULATED.value)
    storage_uri: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = ts_col()
    meta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


class VisionInference(Base):
    __tablename__ = "vision_inferences"
    inference_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("inf"))
    frame_id: Mapped[str] = mapped_column(ForeignKey("vision_frames.frame_id", ondelete="CASCADE"), index=True)
    cycle_id: Mapped[str] = mapped_column(String(64), index=True)
    stage: Mapped[str] = mapped_column(String(32))
    model_version: Mapped[str] = mapped_column(String(80))
    features: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    human_corrected: Mapped[bool] = mapped_column(Boolean, default=False)
    ts: Mapped[datetime] = ts_col()


class HumanLabel(Base):
    __tablename__ = "human_labels"
    label_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("hl"))
    inference_id: Mapped[str] = mapped_column(ForeignKey("vision_inferences.inference_id", ondelete="CASCADE"), index=True)
    corrected_features: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    note: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str] = mapped_column(String(80), default="operator")
    ts: Mapped[datetime] = ts_col()


# ---------------------------------------------------------------------------
# optimizer
# ---------------------------------------------------------------------------
class OptimizerTrial(Base):
    __tablename__ = "optimizer_trials"
    trial_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("tr"))
    cycle_id: Mapped[str | None] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    objective: Mapped[float] = mapped_column(Float, default=0.0)
    constraints_ok: Mapped[bool] = mapped_column(Boolean, default=True)
    recommendation_id: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(80))
    notes: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = ts_col()


class Recommendation(Base):
    __tablename__ = "recommendations"
    recommendation_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("rec"))
    recipe_id: Mapped[str] = mapped_column(String(64), index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    delta: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    expected_effect: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)
    confidence: Mapped[str] = mapped_column(String(16), default="low")
    reason: Mapped[str] = mapped_column(Text, default="")
    based_on_cycles: Mapped[int] = mapped_column(Integer, default=0)
    violations: Mapped[list] = mapped_column(JsonType, default=list)
    state: Mapped[str] = mapped_column(String(16), default="pending")
    applied_recipe_version_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = ts_col()
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(String(80))


# ---------------------------------------------------------------------------
# audit
# ---------------------------------------------------------------------------
class AuditLog(Base):
    """Append-only. Every command, approval, override and failure lands here."""

    __tablename__ = "audit_logs"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True, default=lambda: new_id("au"))
    ts: Mapped[datetime] = ts_col()
    actor: Mapped[str] = mapped_column(String(80))
    action: Mapped[str] = mapped_column(String(64), index=True)
    subject: Mapped[str | None] = mapped_column(String(120), index=True)
    detail: Mapped[dict[str, Any]] = mapped_column(JsonType, default=dict)


APPEND_ONLY_TABLES = ("audit_logs", "stage_events", "command_acks", "telemetry_raw")
