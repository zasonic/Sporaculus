"""Vision contracts: frames, phenotype features, inferences, human corrections."""
from __future__ import annotations

import re
from typing import Optional
from uuid import UUID, uuid4

from pydantic import Field

from .common import SporeModel, Timestamped
from .stages import Stage

SHA256_RE = re.compile(r"^[a-f0-9]{64}$")


class PhenotypeFeatures(SporeModel):
    """Optimizer-ready biological signals extracted from imagery (Phase 6)."""

    colonization_percent: Optional[float] = Field(default=None, ge=0, le=100)
    pin_count: Optional[int] = Field(default=None, ge=0)
    pin_density: Optional[float] = Field(default=None, ge=0)
    cap_area_estimate_cm2: Optional[float] = Field(default=None, ge=0)
    cap_volume_estimate_cm3: Optional[float] = Field(default=None, ge=0)
    maturity_score: Optional[float] = Field(default=None, ge=0, le=1)
    aborted_pin_count: Optional[int] = Field(default=None, ge=0)
    growth_rate_24h: Optional[float] = None
    contamination_probability: Optional[float] = Field(default=None, ge=0, le=1)
    contamination_type: Optional[str] = None
    wetness_score: Optional[float] = Field(default=None, ge=0, le=1)
    dryness_score: Optional[float] = Field(default=None, ge=0, le=1)
    condensation_score: Optional[float] = Field(default=None, ge=0, le=1)


class FrameCreate(SporeModel):
    device_id: str = Field(pattern=r"^dev-[a-z0-9-]+$")
    cycle_id: UUID
    captured_at: object
    content_sha256: str = Field(pattern=SHA256_RE.pattern)
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    has_depth: bool = False


class VisionFrame(Timestamped):
    frame_id: UUID = Field(default_factory=uuid4)
    device_id: str
    cycle_id: UUID
    stage_at_capture: Optional[Stage] = None
    captured_at: object
    content_sha256: str
    width: int
    height: int
    has_depth: bool = False


class VisionInference(Timestamped):
    inference_id: UUID = Field(default_factory=uuid4)
    frame_id: UUID
    model_version: str = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)
    features: PhenotypeFeatures


class HumanLabel(Timestamped):
    """Operator correction; drives the active-learning queue."""

    label_id: UUID = Field(default_factory=uuid4)
    frame_id: UUID
    original_inference_id: Optional[UUID] = None
    corrected_features: PhenotypeFeatures
    labeler: str = Field(min_length=1)
    reason: str = ""
