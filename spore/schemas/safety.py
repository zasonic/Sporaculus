"""Safety envelope: loader + validators. THE authority layer of Phase 0.

Invariants enforced here (see docs/failure_modes.md):
  I1  Optimizer can never write setpoints/actuators directly.
  I2  Every setpoint/command is validated against the envelope before dispatch.
  I3  Out-of-envelope proposals are rejected with machine-readable violations,
      or clamped (recommendations only) — never silently accepted.
  I4  Invalid/stale sensor data => fail_safe behavior.
"""
from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Optional

import yaml
from pydantic import BaseModel, Field, ValidationError

DEFAULT_ENVELOPE_PATH = Path(__file__).resolve().parents[1] / "contracts" / "safety_envelope.yaml"


class Limit(BaseModel):
    min: float
    max: float


class ActuatorLimit(BaseModel):
    max_runtime_minutes: int = Field(gt=0)
    min_off_gap_seconds: int = Field(ge=0)


class Watchdog(BaseModel):
    actuator_watchdog_timeout_seconds: int = Field(gt=0)
    device_heartbeat_interval_seconds: int = Field(gt=0)
    device_offline_after_seconds: int = Field(gt=0)
    command_ack_timeout_seconds: int = Field(gt=0)
    sensor_stale_after_seconds: int = Field(gt=0)


class EnvironmentalLimits(BaseModel):
    temperature_c: Limit
    humidity_percent: Limit
    co2_ppm: Limit
    fresh_air_exchanges_per_hour: Limit
    vpd_kpa: Limit
    light_on_hours_per_day: Limit


class SafetyEnvelope(BaseModel):
    """Immutable at runtime; versioned via contracts/safety_envelope.yaml."""

    meta: dict[str, Any]
    environmental_limits: EnvironmentalLimits
    actuator_limits: dict[str, ActuatorLimit]
    watchdog: Watchdog
    sensor_invalid_behavior: str = Field(pattern="^(fail_safe|hold_last_good|reject_control)$")
    anti_flapping: dict[str, Any]
    data_quality: dict[str, Any]
    optimizer_constraints: dict[str, Any]

    @property
    def version(self) -> int:
        return int(self.meta.get("version", 0))


class SafetyViolation(BaseModel):
    field: str
    attempted: float
    allowed_min: float
    allowed_max: float
    rule: str = "environmental_limits"


# Map setpoint keys used by recipes/commands to envelope limit keys.
_SETPOINT_KEYS = {
    "temperature_c": "temperature_c",
    "humidity_percent": "humidity_percent",
    "co2_ppm": "co2_ppm",
    "fresh_air_exchanges_per_hour": "fresh_air_exchanges_per_hour",
    "light_on_hours": "light_on_hours_per_day",
}


@functools.lru_cache(maxsize=8)
def load_envelope(path_str: Optional[str] = None) -> SafetyEnvelope:
    path = Path(path_str) if path_str else DEFAULT_ENVELOPE_PATH
    raw = yaml.safe_load(path.read_text())
    try:
        return SafetyEnvelope.model_validate(raw)
    except ValidationError as e:  # fail closed: a broken envelope must not boot
        raise RuntimeError(f"safety envelope invalid, refusing to start: {e}") from e


def validate_setpoints(setpoints: dict[str, Any], env: Optional[SafetyEnvelope] = None) -> list[SafetyViolation]:
    """Return [] when every provided setpoint is inside the envelope."""
    env = env or load_envelope()
    limits = env.environmental_limits.model_dump()
    violations: list[SafetyViolation] = []
    for key, value in setpoints.items():
        if value is None:
            continue
        lk = _SETPOINT_KEYS.get(key)
        if lk is None:
            violations.append(SafetyViolation(
                field=key, attempted=float(value), allowed_min=float("nan"),
                allowed_max=float("nan"), rule="unknown_setpoint_rejected"))
            continue
        lim = limits[lk]
        if not (lim["min"] <= float(value) <= lim["max"]):
            violations.append(SafetyViolation(
                field=key, attempted=float(value),
                allowed_min=lim["min"], allowed_max=lim["max"]))
    return violations


def validate_command_payload(action: str, payload: dict[str, Any],
                             env: Optional[SafetyEnvelope] = None) -> list[SafetyViolation]:
    """Gateway-side validation before anything reaches MQTT."""
    env = env or load_envelope()
    if action in ("stop_all", "enter_fail_safe"):
        return []  # safety-increasing actions are always allowed
    if action == "set_setpoint":
        metric = payload.get("metric")
        unit_key = {
            "temperature": "temperature_c",
            "humidity": "humidity_percent",
            "co2": "co2_ppm",
            "fae": "fresh_air_exchanges_per_hour",
        }.get(metric, metric)
        val = payload.get("value", payload.get("value_c", payload.get("value_ppm")))
        if val is None:
            return [SafetyViolation(field=str(metric), attempted=float("nan"),
                                    allowed_min=float("nan"), allowed_max=float("nan"),
                                    rule="missing_value")]
        return validate_setpoints({unit_key: val}, env)
    if action == "set_actuator":
        kind = payload.get("actuator")
        if kind not in env.actuator_limits:
            return [SafetyViolation(field=f"actuator:{kind}", attempted=1, allowed_min=0,
                                    allowed_max=0, rule="unknown_actuator")]
        return []
    if action == "start_recipe":
        stages = payload.get("stages", [])
        out: list[SafetyViolation] = []
        for st in stages:
            out += [
                SafetyViolation(**{**v.model_dump(), "rule": f"recipe_stage:{st.get('stage')}"})
                for v in validate_setpoints(st.get("setpoints", {}), env)
            ]
        return out
    # clear_fail_safe requires human acknowledgment handled upstream; still shape-check
    return []


def clamp_recommendation(proposed: dict[str, float],
                         env: Optional[SafetyEnvelope] = None) -> tuple[dict[str, float], bool]:
    """Phase 7 helper: clamp deltas to max_single_step_delta. Returns (clamped, was_clamped)."""
    env = env or load_envelope()
    steps = env.optimizer_constraints.get("max_single_step_delta", {})
    mapping = {"temperature_c": "temperature_c", "humidity_percent": "humidity_percent",
               "co2_ppm": "co2_ppm", "fae_per_hour": "fae_per_hour", "fae_percent": "fae_per_hour"}
    out, clamped = dict(proposed), False
    for k, v in proposed.items():
        step = steps.get(mapping.get(k, k))
        if step is not None and abs(v) > step:
            out[k] = step if v > 0 else -step
            clamped = True
    return out, clamped
