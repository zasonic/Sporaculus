"""Recipe contracts. Setpoints are validated against the safety envelope."""
from __future__ import annotations

from typing import Optional
from uuid import UUID, uuid4

from pydantic import Field

from .common import SporeModel, Timestamped
from .stages import Stage


class Setpoints(SporeModel):
    """Desired environmental targets. Bounds enforced by safety.validate_setpoints."""

    temperature_c: Optional[float] = None
    humidity_percent: Optional[float] = None
    co2_ppm: Optional[float] = None
    fresh_air_exchanges_per_hour: Optional[float] = None
    light_on_hours: Optional[int] = Field(default=None, ge=0, le=24)


class RecipeStage(SporeModel):
    stage: Stage
    duration_hours: float = Field(ge=0)
    setpoints: Setpoints


class RecipeCreate(SporeModel):
    recipe_id: str = Field(min_length=1)
    strain_filter: Optional[str] = None
    stages: list[RecipeStage] = Field(min_length=1)


class Recipe(Timestamped):
    recipe_id: str
    version: int = Field(ge=1)
    version_uuid: UUID = Field(default_factory=uuid4)
    strain_filter: Optional[str] = None
    stages: list[RecipeStage]
    created_by: str = "operator"
