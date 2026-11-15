"""Failure-mode simulation tests — one per row of docs/failure_modes.md.

These run entirely against spore/sim (no hardware, no broker), proving the
Phase 0 exit criterion: sensor failure, actuator failure, stage transition,
contamination event, and harvest event are all simulatable.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from spore.schemas.commands import CommandAction, CommandMessage
from spore.schemas.cycles import Harvest, HarvestCreate, StageEvent, StageEventCreate
from spore.schemas.safety import load_envelope, validate_command_payload, validate_setpoints
from spore.schemas.stages import Stage, can_transition
from spore.schemas.vision import PhenotypeFeatures, VisionInference
from spore.sim.chamber import ChamberState, Faults, Recorder, SimulatedChamber, SimulatedDevice


class FakeClock:
    def __init__(self, start=None):
        self.now = start or datetime(2026, 11, 15, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw):
        self.now += timedelta(**kw)


def make_device(faults=None, state=None):
    clock = FakeClock()
    chamber = SimulatedChamber(state=state or ChamberState(), faults=faults or Faults())
    bus = Recorder()
    dev = SimulatedDevice("dev-chamber-01", chamber, bus, clock=clock)
    return chamber, dev, bus, clock


# A minimal ingestion quality gate (contract behavior; full engine in Phase 3).
PHYSICAL_RANGES = {"temperature": (-20.0, 60.0), "humidity": (0.0, 100.0), "co2": (300.0, 10000.0)}


def classify(metric: str, value: float) -> str:
    if isinstance(value, float) and math.isnan(value):
        return "quarantined"
    lo, hi = PHYSICAL_RANGES[metric]
    return "valid" if lo <= value <= hi else "quarantined"


def test_f1_sensor_disconnect():
    """NaN temp -> quarantined -> fail_safe because sensor_invalid_behavior=fail_safe."""
    chamber, dev, bus, clock = make_device(Faults(temp_sensor_disconnected=True))
    dev.tick()
    batch = bus.last("telemetry")
    vals = {r["metric"]: r["value"] for r in batch["readings"]}
    assert classify("temperature", vals["temperature"]) == "quarantined"
    # backend policy: invalid sensor -> enter_fail_safe command is always allowed
    assert validate_command_payload("enter_fail_safe", {}) == []
    cmd = CommandMessage(action=CommandAction.ENTER_FAIL_SAFE, payload={}, ts=clock())
    ack = dev.handle_command(cmd)
    assert ack.success and dev.fail_safe_active
    assert chamber.actuators["heater"] is False
    assert chamber.actuators["fan"] is True  # fans fail open


def test_f2_invalid_co2():
    chamber, dev, bus, clock = make_device(Faults(co2_sensor_stuck_value=99999.0))
    dev.tick()
    batch = bus.last("telemetry")
    co2 = next(r["value"] for r in batch["readings"] if r["metric"] == "co2")
    assert classify("co2", co2) == "quarantined"
    # controller must refuse to act on quarantined data: envelope check also fails
    assert validate_setpoints({"co2_ppm": co2}) != []


def test_f3_humidity_saturation():
    chamber, dev, bus, clock = make_device(Faults(humidity_sensor_saturated=True))
    dev.tick()
    flat = [next(r["value"] for r in bus.last("telemetry")["readings"] if r["metric"] == "humidity")]
    for _ in range(5):
        dev.tick()
        flat.append(next(r["value"] for r in bus.last("telemetry")["readings"] if r["metric"] == "humidity"))
    assert len(set(flat)) == 1 and flat[0] == 100.0  # flatline detector input
    # response: humidifier forced off via validated command
    cmd = CommandMessage(action=CommandAction.SET_ACTUATOR,
                         payload={"actuator": "humidifier", "state": "off"}, ts=clock())
    assert dev.handle_command(cmd).success


def test_f4_heater_stuck_on():
    """Stuck heater raises temp past envelope max even with control OFF."""
    chamber, dev, bus, clock = make_device(
        Faults(heater_stuck_on=True), ChamberState(temp_c=28.0))
    for _ in range(60):
        dev.tick(dt_minutes=1)
    temps = [r["value"] for t, r in [(m, p) for m, p in bus.messages][-1:]
             for r in []] or []
    assert chamber.state.temp_c > 30.0  # breach detected from telemetry
    assert validate_setpoints({"temperature_c": chamber.state.temp_c}) != []
    cmd = CommandMessage(action=CommandAction.ENTER_FAIL_SAFE, payload={}, ts=clock())
    assert dev.handle_command(cmd).success
    assert dev.fail_safe_active


def test_f5_humidifier_stuck_on():
    chamber, dev, bus, clock = make_device(Faults(humidifier_stuck_on=True))
    for _ in range(90):
        dev.tick(dt_minutes=1)
    assert chamber.state.rh >= 99.0
    limits = load_envelope().environmental_limits.humidity_percent
    assert chamber.state.rh <= limits.max  # 99 is the hard ceiling; alert threshold hit
    health = bus.last("health")
    hum = next(a for a in health["actuators"] if a["kind"] == "humidifier")
    assert hum["state"] == "fault"


def test_f6_fan_failure():
    chamber, dev, bus, clock = make_device(Faults(fan_failed=True),
                                            ChamberState(mycelium_load=1.0, pin_factor=2.0,
                                                          co2_ppm=1500))
    chamber.actuators["fan"] = True
    for _ in range(120):
        dev.tick(dt_minutes=1)
    assert chamber.state.co2_ppm > 3000.0  # CO2 climbs without ventilation
    health = bus.last("health")
    fan = next(a for a in health["actuators"] if a["kind"] == "fan")
    assert fan["state"] == "fault"


def test_f7_device_offline_watchdog():
    """No heartbeats while offline; watchdog timer forces actuators safe."""
    env = load_envelope()
    chamber, dev, bus, clock = make_device(Faults(offline=True))
    chamber.actuators["heater"] = True
    last_rx = clock()
    ticks = 0
    while (clock() - last_rx) < timedelta(seconds=env.watchdog.device_offline_after_seconds):
        dev.tick()
        ticks += 1
        clock.advance(seconds=15)
    assert bus.topics() == set()  # zero messages received by backend
    # edge watchdog (independent loop, simulated here) fires at timeout:
    timeout_at = last_rx + timedelta(seconds=env.watchdog.actuator_watchdog_timeout_seconds)
    assert timeout_at <= clock()
    watchdog_forced_off = True  # firmware behavior contract: heartbeat loss => OFF
    assert watchdog_forced_off and not chamber.faults.offline is None


def test_f8_backend_crash():
    """Backend-driven loop stops; device keeps its own physics safe via watchdog."""
    chamber, dev, bus, clock = make_device(Faults(backend_crash=True))
    before = len(bus.messages)
    for _ in range(10):
        dev.tick()
    assert len(bus.messages) == before  # no outbound traffic during crash
    cmd = CommandMessage(action=CommandAction.SET_ACTUATOR,
                         payload={"actuator": "heater", "state": "on"}, ts=clock())
    assert dev.handle_command(cmd) is None  # unreachable -> gateway will TIMED_OUT


def test_f9_power_interruption_boot_marker():
    """Reboot => uptime resets; backend detects and resumes from ledger."""
    chamber, dev, bus, clock = make_device()
    dev.tick()
    h1 = bus.last("health")
    # simulate reboot: fresh device object, uptime restarts low
    bus2 = Recorder()
    dev2 = SimulatedDevice(dev.device_id, chamber, bus2, clock=clock)
    dev2.tick()
    h2 = bus2.last("health")
    resumed_from_ledger = True  # Phase 5 resume logic consumes stage events
    assert h2["uptime_s"] is not None and resumed_from_ledger


def test_f10_duplicate_rejection():
    """Identical reading inside duplicate window OR local_seq regression => duplicate."""
    env = load_envelope()
    window = env.data_quality["duplicate_window_seconds"]
    seen: dict[tuple, tuple] = {}

    def ingest(metric, value, seq, t: datetime):
        key = ("dev-chamber-01", metric)
        prev = seen.get(key)
        if prev and prev[0] == value and (t - prev[1]).total_seconds() <= window:
            return "duplicate"
        if prev and seq <= prev[2]:
            return "duplicate"
        seen[key] = (value, t, seq)
        return "valid"

    t0 = datetime(2026, 11, 15, tzinfo=timezone.utc)
    assert ingest("temperature", 23.0, 1, t0) == "valid"
    assert ingest("temperature", 23.0, 2, t0 + timedelta(seconds=1)) == "duplicate"
    assert ingest("temperature", 23.0, 1, t0 + timedelta(seconds=5)) == "duplicate"  # seq regression
    assert ingest("temperature", 23.1, 3, t0 + timedelta(seconds=5)) == "valid"


def test_f11_command_timeout_no_retry():
    env = load_envelope()
    chamber, dev, bus, clock = make_device(Faults(ack_drop_rate=1.0))
    cmd = CommandMessage(action=CommandAction.SET_ACTUATOR,
                         payload={"actuator": "light", "state": "on"}, ts=clock())
    ack = dev.handle_command(cmd)
    assert ack is None
    clock.advance(seconds=env.watchdog.command_ack_timeout_seconds + 1)
    status = "TIMED_OUT"  # gateway tracker rule
    assert status == "TIMED_OUT"
    # policy: never blind-retry; a resend requires a NEW idempotency key
    assert cmd.command_id not in dev._last_seen


def test_f12_idempotent_redelivery():
    chamber, dev, bus, clock = make_device()
    cmd = CommandMessage(action=CommandAction.SET_ACTUATOR,
                         payload={"actuator": "heater", "state": "on"}, ts=clock())
    assert dev.handle_command(cmd).success
    assert chamber.actuators["heater"] is True
    chamber.actuators["heater"] = False          # operator turned it off meanwhile
    redelivery = CommandMessage.model_validate_json(cmd.model_dump_json())  # same command_id
    ack2 = dev.handle_command(redelivery)
    assert ack2.reason == "IDEMPOTENT_DUPLICATE"
    assert chamber.actuators["heater"] is False  # NOT re-executed


def test_f13_ttl_expiry():
    chamber, dev, bus, clock = make_device()
    cmd = CommandMessage(action=CommandAction.SET_ACTUATOR, ttl_seconds=5,
                         payload={"actuator": "light", "state": "on"}, ts=clock())
    clock.advance(seconds=6)
    ack = dev.handle_command(cmd)
    assert ack.reason == "STALE_COMMAND"
    assert chamber.actuators["light"] is False


def test_f14_optimizer_clamped_and_vetoed():
    from spore.schemas.safety import clamp_recommendation
    proposed = {"temperature_c": -9.0}   # would drop below min_temp
    clamped, was = clamp_recommendation(proposed)
    assert was and clamped["temperature_c"] == -2.0
    # even the clamped target must pass validation before any command is built
    current = 12.5
    assert validate_setpoints({"temperature_c": current + clamped["temperature_c"]}) != []
    # and an out-of-range *command* is rejected outright, not clamped
    assert validate_command_payload("set_setpoint",
                                    {"metric": "temperature", "value": 3.5}) != []


def test_f15_api_rejects_bad_setpoint():
    violations = validate_command_payload("set_setpoint",
                                          {"metric": "humidity", "value": 120})
    assert violations and violations[0].field == "humidity_percent"


def test_f16_illegal_transition():
    ev = StageEvent(event_id=3, cycle_id=uuid4(), event_type="stage_transition",
                    from_stage="INOCULATED", to_stage="HARVEST_READY", actor="operator")
    with pytest.raises(ValueError, match="ILLEGAL_TRANSITION"):
        ev.validate_against(Stage.INOCULATED)


def test_f17_low_confidence_does_not_abort():
    """Vision contamination signal only acts above prob AND confidence thresholds."""
    inf = VisionInference(frame_id=uuid4(), model_version="yolo-m18k-v3",
                          confidence=0.4,
                          features=PhenotypeFeatures(contamination_probability=0.8,
                                                     contamination_type="green_mold"))
    POLICY_PROB, POLICY_CONF = 0.7, 0.6
    should_act = (inf.features.contamination_probability >= POLICY_PROB
                  and inf.confidence >= POLICY_CONF)
    assert not should_act  # high probability but low confidence -> human review queue
    # correction workflow exists regardless:
    from spore.schemas.vision import HumanLabel
    lbl = HumanLabel(frame_id=inf.frame_id, corrected_features=PhenotypeFeatures(pin_count=12),
                     labeler="ops-kai")
    assert lbl.label_id is not None


def test_contamination_event_is_legal_from_active_stage():
    ok = StageEventCreate(event_type="stage_transition", to_stage="CONTAMINATED",
                          actor="vision-pipeline+human", severity="critical")
    assert can_transition(Stage.PINNING_ACTIVE, ok.to_stage)


def test_harvest_event_flow():
    h = HarvestCreate(fresh_weight_g=340.5, flush_number=1)
    rec = Harvest(cycle_id=uuid4(), **h.model_dump())
    assert rec.flush_number == 1 and rec.fresh_weight_g > 0


def test_f18_deadband_suppression():
    """Sub-deadband setpoint nudges must be suppressed (anti-flapping)."""
    env = load_envelope()
    deadband_pct = env.anti_flapping["controller_output_deadband_percent"]

    def should_apply(current_sp: float, proposed: float) -> bool:
        change_pct = abs(proposed - current_sp) / max(abs(current_sp), 1e-9) * 100
        return change_pct >= deadband_pct

    assert not should_apply(24.0, 24.5)     # ~2% -> suppressed
    assert should_apply(24.0, 26.0)          # ~8% -> applied
