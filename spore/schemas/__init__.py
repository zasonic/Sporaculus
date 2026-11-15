"""Spore Phase 0 schemas — the contract layer every module builds against.

Modules (Phase 1+) import these instead of defining their own shapes:
    ingestion, gateway, control, biology, vision, optimizer, audit.
"""
from .common import Metric, Unit, QualityFlag, SporeModel, utcnow
from .stages import Stage, STAGE_TRANSITIONS, allowed_transitions
from .telemetry import Reading, TelemetryBatch, SensorHealth, DeviceHealthMessage
from .commands import CommandAction, CommandCreate, CommandMessage, AckMessage, CommandStatus, CommandRecord
from .devices import Device, DeviceRegisterRequest, DeviceKind, EdgeStack, ActuatorKind, ActuatorState
from .cycles import GrowCycle, GrowCycleCreate, StageEvent, StageEventCreate, Harvest, HarvestCreate
from .recipes import Recipe, RecipeCreate, RecipeStage, Setpoints
from .vision import PhenotypeFeatures, VisionFrame, FrameCreate, VisionInference, HumanLabel
from .optimizer import OptimizerTrial, OptimizerTrialCreate, Recommendation, ApprovalDecision
from .safety import SafetyEnvelope, load_envelope, validate_setpoints, validate_command_payload, SafetyViolation

__all__ = [n for n in dir() if not n.startswith("_")]
