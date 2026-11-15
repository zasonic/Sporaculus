"""Phase 0 data contracts — Pydantic v2.

These schemas are the *stable interface* every later module builds against.
They are exported to JSON Schema by ``scripts/export_contracts.py`` and are the
single source of truth for both the REST API and the MQTT payloads.

Two distinct validation layers, deliberately separated:

* **Schema validation** (here) rejects *physically impossible / malformed* data.
* **Envelope validation** (``mycoloop.safety``) rejects *biologically unsafe*
  setpoints and is the only thing allowed to veto the optimizer.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(dt: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; normalise everything to aware UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class Contract(BaseModel):
    """Strict base: unknown fields are a contract violation, not a silent no-op."""

    model_config = ConfigDict(extra="forbid", use_enum_values=False, populate_by_name=True)


class ReadModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# enums
# ---------------------------------------------------------------------------
class Stage(str, Enum):
    INOCULATED = "INOCULATED"
    COLONIZING = "COLONIZING"
    COLONIZATION_COMPLETE = "COLONIZATION_COMPLETE"
    PINNING_INITIATED = "PINNING_INITIATED"
    PINNING_ACTIVE = "PINNING_ACTIVE"
    MATURATION = "MATURATION"
    HARVEST_READY = "HARVEST_READY"
    HARVESTED = "HARVESTED"
    FLUSH_COMPLETE = "FLUSH_COMPLETE"
    CONTAMINATED = "CONTAMINATED"
    ABORTED = "ABORTED"


TERMINAL_STAGES = {Stage.CONTAMINATED, Stage.ABORTED}


class CommandKind(str, Enum):
    SETPOINT = "setpoint"
    ACTUATOR = "actuator"
    RECIPE = "recipe"
    REBOOT = "reboot"


class CommandState(str, Enum):
    QUEUED = "queued"
    SENT = "sent"
    RECEIVED = "received"
    APPLIED = "applied"
    FAILED = "failed"
    EXPIRED = "expired"
    REJECTED_SAFETY = "rejected_safety"


CLOSED_COMMAND_STATES = {
    CommandState.APPLIED,
    CommandState.FAILED,
    CommandState.EXPIRED,
    CommandState.REJECTED_SAFETY,
}


class AckState(str, Enum):
    RECEIVED = "received"
    APPLIED = "applied"
    FAILED = "failed"


class DeviceStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    FAIL_SAFE = "fail_safe"
    UNKNOWN = "unknown"


class Severity(str, Enum):
    LOCALIZED = "localized"
    SPREADING = "spreading"
    SYSTEMIC = "systemic"


# ---------------------------------------------------------------------------
# safety envelope + setpoints
# ---------------------------------------------------------------------------
class Setpoints(Contract):
    temp_c: float = Field(ge=-5, le=45)
    humidity_pct: float = Field(ge=0, le=100)
    co2_ceiling_ppm: int = Field(ge=350, le=10000)
    fan_pct: int = Field(ge=0, le=100)
    light_pct: int = Field(ge=0, le=100)


class SafetyEnvelope(Contract):
    """Biological + hardware safety envelope (contracts/safety-envelope.yaml)."""

    min_temp_c: float = 10.0
    max_temp_c: float = 30.0
    min_humidity_pct: float = 50.0
    max_humidity_pct: float = 99.0
    max_co2_ppm: int = 3000
    min_fae_pct: int = 10
    max_heater_runtime_min: int = 20
    max_humidifier_runtime_min: int = 30
    actuator_cooldown_s: int = 30
    watchdog_timeout_s: int = 60
    sensor_invalid_behavior: Literal["fail_safe"] = "fail_safe"
    max_contamination_probability: float = 0.35


class Violation(ReadModel):
    field: str
    value: Any
    rule: str
    message: str


class SafetyVerdict(ReadModel):
    ok: bool
    violations: list[Violation] = []


# ---------------------------------------------------------------------------
# telemetry / device health  (AsyncAPI payloads)
# ---------------------------------------------------------------------------
class Telemetry(Contract):
    device_id: str
    seq: int = Field(ge=0, description="monotonic per-device; duplicate rejection key")
    ts: datetime
    temperature_c: float | None = Field(default=None, ge=-40, le=85)
    humidity_pct: float | None = Field(default=None, ge=0, le=100)
    co2_ppm: int | None = Field(default=None, ge=0, le=60000)
    substrate_moisture_pct: float | None = Field(default=None, ge=0, le=100)
    fan_pct: int | None = Field(default=None, ge=0, le=100)
    light_pct: int | None = Field(default=None, ge=0, le=100)
    actuators: dict[str, bool] = {}
    quality_flags: list[str] = Field(default_factory=list, description="device-reported flags")

    @field_validator("actuators")
    @classmethod
    def _known_actuators(cls, v: dict[str, bool]) -> dict[str, bool]:
        allowed = {"heater", "humidifier", "fan", "light"}
        unknown = set(v) - allowed
        if unknown:
            raise ValueError(f"unknown actuators: {sorted(unknown)}")
        return v


class TelemetryBatch(Contract):
    items: list[Telemetry] = Field(min_length=1, max_length=500)


class DeviceHealth(Contract):
    device_id: str
    ts: datetime
    uptime_s: int = Field(ge=0)
    firmware: str
    sensor_health: dict[str, float] = {}
    watchdog_state: Literal["armed", "fail_safe"] = "armed"
    watchdog_remaining_s: int | None = None
    safe_state: dict[str, Any] = {}
    faults: list[str] = []


class DeviceStatusMessage(Contract):
    device_id: str
    ts: datetime
    state: Literal["online", "offline"]
    firmware: str | None = None
    reason: str | None = None


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
class CommandCreate(Contract):
    kind: CommandKind
    payload: dict[str, Any]
    idempotency_key: str = Field(min_length=8, max_length=120)
    ttl_s: int = Field(default=60, ge=1, le=3600)

    @field_validator("payload")
    @classmethod
    def _payload_matches_kind(cls, v: dict[str, Any], info) -> dict[str, Any]:
        kind = info.data.get("kind")
        if kind == CommandKind.SETPOINT:
            Setpoints.model_validate(v)
        elif kind == CommandKind.ACTUATOR:
            name, state = v.get("actuator"), v.get("state")
            if name not in {"heater", "humidifier", "fan", "light"}:
                raise ValueError("actuator must be heater|humidifier|fan|light")
            if not isinstance(state, bool):
                raise ValueError("state must be bool")
        elif kind == CommandKind.RECIPE:
            if "recipe_version_id" not in v:
                raise ValueError("recipe command requires recipe_version_id")
            Setpoints.model_validate(v.get("setpoints", {}))
        return v


class CommandAck(Contract):
    command_id: str
    device_id: str
    state: AckState
    ts: datetime
    detail: str | None = None
    duplicate: bool = False


class CommandRead(ReadModel):
    command_id: str
    device_id: str
    kind: CommandKind
    payload: dict[str, Any]
    idempotency_key: str
    state: CommandState
    attempts: int
    issued_at: datetime
    expires_at: datetime
    sent_at: datetime | None = None
    closed_at: datetime | None = None
    last_error: str | None = None
    issued_by: str
    idempotent_replay: bool = False


class DispatchResult(ReadModel):
    command: CommandRead
    safety: SafetyVerdict
    idempotent_replay: bool = False


# ---------------------------------------------------------------------------
# devices
# ---------------------------------------------------------------------------
class DeviceCreate(Contract):
    device_id: str = Field(min_length=3, max_length=64, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    name: str
    edge_stack: Literal["mycodo", "esphome", "simulated"] = "simulated"
    firmware: str = "0.0.0"
    sensors: list[str] = ["temperature", "humidity", "co2"]
    actuators: list[str] = ["heater", "humidifier", "fan", "light"]


class DeviceRead(ReadModel):
    device_id: str
    name: str
    edge_stack: str
    firmware: str
    status: DeviceStatus
    registered_at: datetime
    last_seen_at: datetime | None = None
    watchdog_state: str = "armed"


class DeviceToken(ReadModel):
    device_id: str
    token: str
    note: str = "Shown once. Store hashed; used for MQTT username/password and X-Device-Token."


class SensorRead(ReadModel):
    sensor_id: str
    device_id: str
    kind: str
    unit: str
    calibration_offset: float
    calibration_scale: float
    health_score: float
    last_seen_at: datetime | None = None


class CalibrationUpdate(Contract):
    offset: float = 0.0
    scale: float = Field(default=1.0, gt=0)


# ---------------------------------------------------------------------------
# biology ledger
# ---------------------------------------------------------------------------
class StrainCreate(Contract):
    strain_id: str
    name: str
    species: str | None = None
    notes: str | None = None


class StrainRead(ReadModel):
    strain_id: str
    name: str
    species: str | None = None
    notes: str | None = None


class SubstrateBatchCreate(Contract):
    batch_id: str
    substrate_type: str = "masters mix"
    sterilization: str = "autoclave 121C 90min"
    mass_kg: float = Field(default=2.5, gt=0)
    notes: str | None = None


class SubstrateBatchRead(ReadModel):
    batch_id: str
    substrate_type: str
    sterilization: str
    mass_kg: float
    prepared_at: datetime


class RecipeCreate(Contract):
    recipe_id: str
    name: str
    description: str | None = None
    stage: Stage = Stage.PINNING_ACTIVE
    setpoints: Setpoints
    change_reason: str | None = None


class RecipeVersionRead(ReadModel):
    recipe_version_id: str
    recipe_id: str
    version: int
    stage: Stage
    setpoints: dict[str, Any]
    created_at: datetime
    created_by: str
    approved_by: str | None = None
    change_reason: str | None = None
    parent_version_id: str | None = None


class GrowCycleCreate(Contract):
    cycle_id: str | None = None
    strain_id: str
    substrate_batch_id: str
    device_id: str
    recipe_version_id: str
    notes: str | None = None


class GrowCycleRead(ReadModel):
    cycle_id: str
    strain_id: str
    substrate_batch_id: str
    device_id: str
    recipe_version_id: str
    stage: Stage
    flush: int
    yield_g: float
    contaminated: bool
    started_at: datetime
    ended_at: datetime | None = None
    notes: str | None = None


class StageTransition(Contract):
    to_stage: Stage
    reason: str = Field(min_length=3, max_length=500)


class StageEventRead(ReadModel):
    event_id: str
    cycle_id: str
    from_stage: Stage | None
    to_stage: Stage
    actor: str
    reason: str
    ts: datetime


class HarvestCreate(Contract):
    grams: float = Field(gt=0, le=100000)
    flush: int = Field(default=1, ge=1, le=20)
    quality: str | None = None
    notes: str | None = None


class HarvestRead(ReadModel):
    harvest_id: str
    cycle_id: str
    grams: float
    flush: int
    quality: str | None
    notes: str | None
    actor: str
    ts: datetime


class ContaminationCreate(Contract):
    kind: Literal["trichoderma", "bacterial blotch", "cobweb", "black mold", "unknown"] = "unknown"
    severity: Severity = Severity.LOCALIZED
    notes: str | None = None


class AnnotationCreate(Contract):
    body: str = Field(min_length=1, max_length=2000)


class StageEnvironment(ReadModel):
    stage: Stage
    samples: int
    avg_temp_c: float | None
    avg_humidity_pct: float | None
    avg_co2_ppm: float | None
    min_co2_ppm: float | None
    max_co2_ppm: float | None
    from_ts: datetime | None
    to_ts: datetime | None


class CycleTimeline(ReadModel):
    cycle: GrowCycleRead
    events: list[dict[str, Any]]
    environment_by_stage: list[StageEnvironment]
    harvests: list[HarvestRead]


# ---------------------------------------------------------------------------
# vision / phenotype
# ---------------------------------------------------------------------------
class PhenotypeFeatures(Contract):
    colonization_percent: float = Field(ge=0, le=100)
    pin_count: int = Field(ge=0)
    pin_density: float = Field(ge=0)
    cap_area_estimate: float = Field(ge=0)
    cap_volume_estimate: float = Field(ge=0)
    maturity_score: float = Field(ge=0, le=1)
    aborted_pin_count: int = Field(ge=0)
    growth_rate_24h: float
    contamination_probability: float = Field(ge=0, le=1)
    contamination_type: str = "none"
    wetness_score: float = Field(ge=0, le=1)
    dryness_score: float = Field(ge=0, le=1)
    condensation_score: float = Field(ge=0, le=1)


class VisionFrameCreate(Contract):
    frame_id: str | None = None
    cycle_id: str
    device_id: str
    ts: datetime | None = None
    storage_uri: str = "file:///frames/placeholder.jpg"
    model_version: str
    features: PhenotypeFeatures
    confidence: float = Field(ge=0, le=1)


class VisionInferenceRead(ReadModel):
    inference_id: str
    frame_id: str
    cycle_id: str
    stage: Stage
    ts: datetime
    model_version: str
    features: dict[str, Any]
    confidence: float
    human_corrected: bool


class HumanLabelCreate(Contract):
    features: PhenotypeFeatures
    note: str | None = None


class ModelRegister(Contract):
    model_version: str
    family: Literal["yolo", "rt-detr", "sam2", "depth-anything"] = "yolo"
    dataset: str | None = None
    metrics: dict[str, float] = {}
    edge_runtime: Literal["onnxruntime", "tensorrt", "pytorch"] = "onnxruntime"


# ---------------------------------------------------------------------------
# optimizer
# ---------------------------------------------------------------------------
class SearchSpace(Contract):
    temp_min: float = 16.0
    temp_max: float = 22.0
    rh_min: float = 86.0
    rh_max: float = 96.0
    co2_max: int = 1400
    fae_min: int = 35
    fae_max: int = 90


class OptimizerTrialCreate(Contract):
    cycle_id: str | None = None
    params: Setpoints
    objective: float
    constraints_ok: bool = True
    notes: str | None = None


class OptimizerTrialRead(ReadModel):
    trial_id: str
    cycle_id: str | None
    params: dict[str, Any]
    objective: float
    constraints_ok: bool
    recommendation_id: str | None
    approved_by: str | None
    ts: datetime


class RecommendationRead(ReadModel):
    recommendation_id: str
    recipe_id: str
    device_id: str
    params: dict[str, Any]
    delta: dict[str, Any]
    expected_effect: dict[str, Any]
    confidence: Literal["low", "medium", "high"]
    reason: str
    based_on_cycles: int
    violations: list[dict[str, Any]]
    state: Literal["pending", "approved", "rejected", "rolled_back"]
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    applied_recipe_version_id: str | None = None


class ApprovalRequest(Contract):
    note: str | None = None


class RollbackRequest(Contract):
    reason: str = Field(min_length=3)


# ---------------------------------------------------------------------------
# audit / quality reporting
# ---------------------------------------------------------------------------
class AuditRead(ReadModel):
    audit_id: str
    ts: datetime
    actor: str
    action: str
    subject: str | None
    detail: dict[str, Any]


class QualityFlags:
    """Canonical telemetry quality vocabulary (Phase 3)."""

    DUPLICATE = "duplicate_seq"
    OUT_OF_RANGE = "out_of_range"
    PLAUSIBILITY_JUMP = "plausibility_jump"
    SENSOR_SATURATED = "sensor_saturated"
    SENSOR_MISSING = "sensor_missing"
    DRIFT_SUSPECTED = "drift_suspected"
    TS_SKEW = "timestamp_skew"
    STALE = "stale_reading"
    CALIBRATED = "calibrated"

    ALL = {
        DUPLICATE, OUT_OF_RANGE, PLAUSIBILITY_JUMP, SENSOR_SATURATED,
        SENSOR_MISSING, DRIFT_SUSPECTED, TS_SKEW, STALE, CALIBRATED,
    }
    # flags that make a field untrustworthy for the optimizer
    BLOCKING = {DUPLICATE, OUT_OF_RANGE, PLAUSIBILITY_JUMP, SENSOR_SATURATED, SENSOR_MISSING}


class IngestResult(ReadModel):
    accepted: int
    duplicates: int
    quarantined: int
    flagged: int
    details: list[dict[str, Any]] = []


class TelemetryCleanRead(ReadModel):
    id: int
    device_id: str
    ts: datetime
    temperature_c: float | None
    humidity_pct: float | None
    co2_ppm: float | None
    fan_pct: int | None
    light_pct: int | None
    actuators: dict[str, Any]
    quality_flags: list[str]
    quality_score: float
    validated: bool
    cycle_id: str | None
    stage: Stage | None


class QuarantineRead(ReadModel):
    id: int
    device_id: str
    seq: int
    ts: datetime
    reasons: list[str]
    payload: dict[str, Any]


class DriftReport(ReadModel):
    device_id: str
    sensor: str
    health_score: float
    samples: int
    invalid_ratio: float
    drift_suspected: bool
    mean: float | None
    stddev: float | None


class HealthResponse(ReadModel):
    status: Literal["ok", "degraded"]
    version: str
    database: str
    bus: str
    devices_online: int
    devices_total: int
    pending_commands: int
    dry_run: bool
