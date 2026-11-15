"""Grow cycle, stage-event ledger, and harvest contracts (Phase 5 foundation)."""
from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from pydantic import Field, model_validator

from .common import SporeModel, Timestamped, utcnow
from .stages import Stage, can_transition


class GrowCycleCreate(SporeModel):
    strain_id: str = Field(min_length=1)
    substrate_batch_id: str = Field(min_length=1)
    device_id: str = Field(pattern=r"^dev-[a-z0-9-]+$")
    recipe_version_id: UUID


class GrowCycle(Timestamped):
    cycle_id: UUID = Field(default_factory=uuid4)
    strain_id: str
    substrate_batch_id: str
    device_id: str
    recipe_version_id: UUID
    stage: Stage = Stage.INOCULATED
    started_at: object = Field(default_factory=utcnow)
    ended_at: Optional[object] = None
    total_yield_g: Optional[float] = Field(default=None, ge=0)


class StageEventCreate(SporeModel):
    event_type: str = Field(
        pattern=(
            r"^(stage_transition|annotation|contamination_event|harvest|"
            r"flush_started|flush_completed|abort)$"
        )
    )
    to_stage: Optional[Stage] = None
    note: str = ""
    actor: str = Field(min_length=1, description="operator id | vision-pipeline | state-machine")
    severity: str = Field(default="info", pattern="^(info|warning|critical)$")

    @model_validator(mode="after")
    def _transition_needs_target(self) -> "StageEventCreate":
        if self.event_type == "stage_transition" and self.to_stage is None:
            raise ValueError("stage_transition requires to_stage")
        return self


class StageEvent(Timestamped):
    """Append-only ledger row. event_id is monotonic per cycle."""

    event_id: int = Field(ge=0)
    cycle_id: UUID
    event_type: str
    from_stage: Optional[Stage] = None
    to_stage: Optional[Stage] = None
    note: str = ""
    actor: str
    severity: str = "info"

    def validate_against(self, current_stage: Stage) -> None:
        """Called by the transition engine; raises ValueError when illegal."""
        if self.event_type == "stage_transition":
            if self.to_stage is None or not can_transition(current_stage, self.to_stage):
                raise ValueError(
                    f"ILLEGAL_TRANSITION {current_stage} -> {self.to_stage}"
                )


class HarvestCreate(SporeModel):
    fresh_weight_g: float = Field(ge=0)
    flush_number: int = Field(ge=1)
    note: str = ""


class Harvest(Timestamped):
    harvest_id: UUID = Field(default_factory=uuid4)
    cycle_id: UUID
    fresh_weight_g: float
    flush_number: int
    note: str = ""
