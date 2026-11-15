"""Optimizer contracts (Phase 7). Trials and recommendations — never commands."""
from __future__ import annotations

from typing import Any, Optional
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import SporeModel, Timestamped


class OptimizerTrialCreate(SporeModel):
    study: str = Field(min_length=1)
    params: dict[str, float] = Field(
        description="Proposed setpoint deltas only — actuator keys are rejected."
    )
    objective_value: float
    constraints_satisfied: bool
    cycle_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_actuator_keys(self) -> "OptimizerTrialCreate":
        forbidden = {"heater", "humidifier", "fan", "dehumidifier", "light",
                     "misting", "relay", "actuator"}
        bad = forbidden & {k.lower() for k in self.params}
        if bad:
            raise ValueError(
                f"optimizer may not propose actuator writes: {sorted(bad)}"
            )
        return self


class OptimizerTrial(Timestamped):
    trial_id: UUID = Field(default_factory=uuid4)
    study: str
    params: dict[str, float]
    objective_value: float
    constraints_satisfied: bool
    cycle_ids: list[UUID] = Field(default_factory=list)


class Recommendation(Timestamped):
    recommendation_id: UUID = Field(default_factory=uuid4)
    based_on_cycle_count: int = Field(ge=0)
    proposed_changes: dict[str, float]
    expected_effects: list[str] = Field(default_factory=list)
    rationale: str
    confidence: str = Field(pattern="^(low|medium|high)$")
    status: str = Field(
        default="pending_approval",
        pattern="^(pending_approval|approved|rejected|applied|rolled_back)$",
    )
    clamped_by_safety: bool = False


class ApprovalDecision(SporeModel):
    approved: bool
    approver: str = Field(min_length=1)
    comment: str = ""


def objective_score(
    yield_g_per_day: float,
    contamination_events: int,
    aborted_pins: int,
    energy_kwh: float,
    labor_minutes: float,
    weights: Optional[dict[str, float]] = None,
) -> float:
    """Canonical Phase-7 objective: score = yield/day - penalties.

    Weights are versioned with the study; defaults keep units comparable.
    """
    w: dict[str, float] = {
        "contamination_penalty": 250.0,   # g/day-equivalent per event
        "aborted_pin_penalty": 20.0,      # g/day-equivalent per aborted pin
        "energy_penalty": 5.0,            # g/day-equivalent per kWh
        "labor_penalty": 1.0,             # g/day-equivalent per minute
    } | (weights or {})
    return (
        yield_g_per_day
        - contamination_events * w["contamination_penalty"]
        - aborted_pins * w["aborted_pin_penalty"]
        - energy_kwh * w["energy_penalty"]
        - labor_minutes * w["labor_penalty"]
    )
