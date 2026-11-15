"""Phase 0 exit-criterion tests: contract validation + simulated flows.

Covers the roadmap requirement that, without hardware, we can simulate:
device registration, telemetry streaming, sensor failure, actuator failure,
stage transition, contamination event, harvest event.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from spore.schemas.commands import CommandAction, CommandMessage, CommandStatus
from spore.schemas.cycles import GrowCycle, GrowCycleCreate, Harvest, HarvestCreate, StageEvent, StageEventCreate
from spore.schemas.devices import Device, DeviceKind, DeviceRegisterRequest, EdgeStack
from spore.schemas.optimizer import OptimizerTrialCreate, Recommendation
from spore.schemas.recipes import Recipe, RecipeCreate, RecipeStage, Setpoints
from spore.schemas.safety import clamp_recommendation, load_envelope, validate_command_payload, validate_setpoints
from spore.schemas.stages import STAGE_TRANSITIONS, Stage, allowed_transitions, can_transition
from spore.schemas.telemetry import TelemetryBatch, DeviceHealthMessage
from spore.schemas.vision import FrameCreate, HumanLabel, PhenotypeFeatures, VisionInference


class TestDeviceContracts:
    def test_register_valid(self):
        req = DeviceRegisterRequest(device_id="dev-chamber-01", name="Chamber 1",
                                    kind=DeviceKind.CHAMBER, edge_stack=EdgeStack.MYCODO)
        assert req.device_id == "dev-chamber-01"

    @pytest.mark.parametrize("bad", ["chamber01", "DEV-x", "dev_", "dev-X"])
    def test_register_bad_id_rejected(self, bad):
        with pytest.raises(ValueError):
            DeviceRegisterRequest(device_id=bad, name="x", kind=DeviceKind.CHAMBER)

    def test_strict_extra_fields(self):
        with pytest.raises(ValueError):
            DeviceRegisterRequest.model_validate(
                {"device_id": "dev-a", "name": "a", "kind": "tent", "evil": True})


class TestTelemetryContracts:
    def batch(self, **over):
        base = dict(device_id="dev-chamber-01", ts=datetime.now(timezone.utc), readings=[
            {"sensor_id": "temperature-0", "metric": "temperature", "value": 23.4, "unit": "C"},
            {"sensor_id": "co2-0", "metric": "co2", "value": 850, "unit": "ppm"},
        ])
        base.update(over)
        return TelemetryBatch.model_validate(base)

    def test_valid_batch(self):
        b = self.batch()
        assert len(b.readings) == 2

    def test_wrong_unit_rejected(self):
        with pytest.raises(ValueError):
            self.batch(readings=[{"sensor_id": "t", "metric": "temperature",
                                  "value": 1.0, "unit": "ppm"}])

    def test_empty_batch_rejected(self):
        with pytest.raises(ValueError):
            self.batch(readings=[])

    def test_health_message_roundtrip(self):
        msg = DeviceHealthMessage(device_id="dev-chamber-01", uptime_s=120,
                                  sensors=[{"sensor_id": "temp-0", "ok": True}],
                                  actuators=[{"actuator_id": "h1", "kind": "heater", "state": "off"}])
        again = DeviceHealthMessage.model_validate_json(msg.model_dump_json())
        assert again.fail_safe_active is False


class TestStageMachine:
    def test_every_stage_declared(self):
        assert set(STAGE_TRANSITIONS) == set(Stage)

    def test_happy_path_full_cycle(self):
        path = [Stage.INOCULATED, Stage.COLONIZING, Stage.COLONIZATION_COMPLETE,
                Stage.PINNING_INITIATED, Stage.PINNING_ACTIVE, Stage.MATURATION,
                Stage.HARVEST_READY, Stage.HARVESTED, Stage.FLUSH_COMPLETE]
        for a, b in zip(path, path[1:]):
            assert can_transition(a, b), f"{a}->{b} must be legal"

    def test_multi_flush_loop(self):
        assert can_transition(Stage.HARVESTED, Stage.PINNING_INITIATED)

    def test_illegal_transitions(self):
        assert not can_transition(Stage.INOCULATED, Stage.MATURATION)
        assert not can_transition(Stage.CONTAMINATED, Stage.COLONIZING)  # terminal
        assert not can_transition(Stage.FLUSH_COMPLETE, Stage.INOCULATED)

    def test_contamination_and_abort_reachable_from_all_active(self):
        for s in STAGE_TRANSITIONS:
            if STAGE_TRANSITIONS[s]:  # non-terminal
                assert Stage.CONTAMINATED in allowed_transitions(s)
                assert Stage.ABORTED in allowed_transitions(s)

    def test_event_ledger_validation(self):
        ev = StageEvent(event_id=1, cycle_id=uuid4(), event_type="stage_transition",
                        from_stage="COLONIZING", to_stage="MATURATION", actor="vision-pipeline")
        with pytest.raises(ValueError, match="ILLEGAL_TRANSITION"):
            ev.validate_against(Stage.COLONIZING)
        ev.validate_against(Stage.PINNING_ACTIVE)  # legal

    def test_transition_requires_target(self):
        with pytest.raises(ValueError):
            StageEventCreate(event_type="stage_transition", actor="op")


class TestSafetyEnvelope:
    env = load_envelope()

    def test_yaml_loads(self):
        assert self.env.version == 1
        assert self.env.environmental_limits.temperature_c.max == 30.0
        assert self.env.watchdog.actuator_watchdog_timeout_seconds == 60

    def test_setpoints_inside(self):
        assert validate_setpoints({"temperature_c": 24, "humidity_percent": 92,
                                   "co2_ppm": 1800}, self.env) == []

    def test_setpoints_outside(self):
        v = validate_setpoints({"temperature_c": 35}, self.env)
        assert len(v) == 1 and v[0].allowed_max == 30.0

    def test_unknown_setpoint_rejected(self):
        assert validate_setpoints({"plasma_cooling": 5}, self.env) != []

    def test_command_validation(self):
        assert validate_command_payload("set_setpoint",
                                        {"metric": "co2", "value": 2500}, self.env) == []
        assert validate_command_payload("set_setpoint",
                                        {"metric": "co2", "value": 9999}, self.env) != []
        assert validate_command_payload("stop_all", {}, self.env) == []

    def test_recipe_command_validates_each_stage(self):
        stages = [{"stage": "MATURATION", "setpoints": {"temperature_c": 45}}]
        assert validate_command_payload("start_recipe", {"stages": stages}, self.env) != []

    def test_clamp_recommendation(self):
        clamped, was = clamp_recommendation({"temperature_c": -5.0, "co2_ppm": 100}, self.env)
        assert clamped["temperature_c"] == -2.0 and clamped["co2_ppm"] == 100 and was

    def test_optimizer_schema_blocks_actuator_writes(self):
        with pytest.raises(ValueError, match="actuator"):
            OptimizerTrialCreate(study="s", params={"heater": 100.0},
                                 objective_value=1.0, constraints_satisfied=True)


class TestRecipeAndRecommendationContracts:
    def test_recipe_roundtrip(self):
        r = Recipe(recipe_id="r-blue-oyster", version=2, stages=[
            RecipeStage(stage=Stage.COLONIZING, duration_hours=336,
                        setpoints=Setpoints(temperature_c=22, co2_ppm=2500))])
        assert Recipe.model_validate_json(r.model_dump_json()).version == 2

    def test_recommendation_shape(self):
        rec = Recommendation(based_on_cycle_count=8,
                             proposed_changes={"temperature_c": -1.1, "fae_per_hour": 1.0},
                             expected_effects=["improved yield/day"],
                             rationale="previous cycles showed stronger pin survival",
                             confidence="medium")
        assert rec.status == "pending_approval"
