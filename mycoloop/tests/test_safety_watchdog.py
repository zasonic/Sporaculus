"""Phase 0/4 safety: the watchdog, interlocks and dry-run must hold without the backend."""
from __future__ import annotations

from conftest import OPERATOR, setpoint_command

from mycoloop.safety import ActuatorRuntime, evaluate_actuators, fail_safe_state, load_envelope, validate_setpoints
from mycoloop.schemas import Setpoints


def test_envelope_vetoes_unsafe_setpoints():
    env = load_envelope()
    assert validate_setpoints(Setpoints(temp_c=19, humidity_pct=92, co2_ceiling_ppm=900,
                                        fan_pct=45, light_pct=60), env).ok
    bad = validate_setpoints({"temp_c": 34, "humidity_pct": 99.5, "co2_ceiling_ppm": 4000,
                              "fan_pct": 2, "light_pct": 0}, env)
    assert not bad.ok
    assert {v.rule for v in bad.violations} == {"temp_range", "humidity_range", "co2_ceiling", "min_fae"}


def test_invalid_sensors_force_fail_safe_actuation():
    allowed, vetoes = evaluate_actuators(
        {"heater": True, "humidifier": True, "fan": False, "light": True, "fan_pct": 0, "light_pct": 80},
        temp_c=None, humidity_pct=None, co2_ppm=None, sensors_valid=False,
        runtime=ActuatorRuntime(), now_s=1000.0,
    )
    assert vetoes == ["sensor_invalid_fail_safe"]
    assert allowed["heater"] is False and allowed["humidifier"] is False
    assert allowed["fan_pct"] == 100
    assert allowed == fail_safe_state()


def test_interlocks_override_the_proposal():
    env = load_envelope()
    runtime = ActuatorRuntime()
    runtime.runtime_s["heater"] = env.max_heater_runtime_min * 60 + 1
    allowed, vetoes = evaluate_actuators(
        {"heater": True, "humidifier": True, "fan": True, "light": True, "fan_pct": 40, "light_pct": 50},
        temp_c=env.max_temp_c + 0.5, humidity_pct=env.max_humidity_pct, co2_ppm=env.max_co2_ppm + 100,
        sensors_valid=True, runtime=runtime, now_s=1000.0,
    )
    assert allowed["heater"] is False
    assert allowed["humidifier"] is False
    assert allowed["fan_pct"] == 100
    assert {"over_temp_interlock", "over_humidity_interlock", "co2_purge", "heater_runtime_limit"} <= set(vetoes)


def test_anti_flapping_cooldown_holds_previous_state():
    runtime = ActuatorRuntime()
    runtime.observe({"heater": False}, now_s=0.0, dt_s=1.0)
    runtime.observe({"heater": True}, now_s=1.0, dt_s=1.0)  # changed 1s ago
    allowed, vetoes = evaluate_actuators(
        {"heater": False, "humidifier": False, "fan": True, "light": False, "fan_pct": 40, "light_pct": 0},
        temp_c=18.0, humidity_pct=90.0, co2_ppm=800.0, sensors_valid=True, runtime=runtime, now_s=2.0,
    )
    assert allowed["heater"] is True  # cooldown refused the flap
    assert "heater_cooldown" in vetoes


def test_hardware_watchdog_fires_when_backend_goes_silent(device):
    device.tick()
    assert device.chamber.state.fail_safe is False
    for _ in range(61):
        device.tick()
    s = device.chamber.state
    assert s.fail_safe is True
    assert s.heater is False and s.humidifier is False
    assert s.fan_pct == 100 and s.light_pct == 0
    assert "watchdog_timeout_fail_safe" in s.veto_log


def test_watchdog_rearms_on_backend_contact(device):
    for _ in range(61):
        device.tick()
    assert device.chamber.state.fail_safe is True
    device.chamber.backend_contact()  # a command or backend heartbeat re-arms it
    device.tick()
    assert device.chamber.state.fail_safe is False
    assert device.chamber.state.watchdog_remaining_s > 0


def test_fail_safe_cuts_power_to_a_welded_relay(device):
    device.set_fault("heater_stuck")
    device.tick()
    assert device.chamber.state.heater is True
    for _ in range(61):
        device.tick()
    # even a stuck relay is de-energised by the fail-safe supply cut
    assert device.chamber.state.heater is False
    assert device.chamber.state.fail_safe is True


def test_hardware_interlock_independent_of_backend(device):
    env = load_envelope()
    device.chamber.state.temp_c = env.max_temp_c + 1
    device.chamber.state.heater = True
    device.tick()
    assert device.chamber.state.heater is False
    assert "hw_over_temp" in device.chamber.state.veto_log


def test_backend_crash_partition_leaves_device_safe(client, device):
    device.tick()
    device.set_fault("backend_crash")
    for _ in range(61):
        device.tick()
    assert device.chamber.state.fail_safe is True
    # the backend cannot even reach it, so no unsafe command can be delivered
    r = client.post("/devices/chamber-01/commands", headers=OPERATOR, json=setpoint_command(key="crash-cmd-001"))
    assert r.json()["command"]["state"] == "queued"
    device.set_fault("backend_crash", False)
    device.tick()
    assert device.chamber.state.fail_safe is False


def test_power_loss_creates_a_heartbeat_gap(client, device):
    device.tick()
    before = len([m for m in device.bus.published if m.topic.endswith("/telemetry")])
    device.set_fault("power_loss")
    for _ in range(5):
        device.tick()
    after = len([m for m in device.bus.published if m.topic.endswith("/telemetry")])
    assert after == before


def test_dry_run_computes_but_never_dispatches(client, device, biology, settings):
    settings.dry_run = True
    for _ in range(3):
        device.tick()
    before = len(device.received_commands)
    r = client.post("/devices/chamber-01/control/evaluate", headers=OPERATOR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["dispatched"] == []
    assert len(device.received_commands) == before
    settings.dry_run = False


def test_control_engine_dispatches_through_the_safety_layer(client, device, biology):
    device.chamber.state.temp_c = 15.0
    device.chamber.state.humidity_pct = 80.0
    device.tick()
    r = client.post("/devices/chamber-01/control/evaluate", headers=OPERATOR)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sensors_valid"] is True
    assert body["proposed"]["heater"] is True
    assert all(d["safety_ok"] for d in body["dispatched"])
    state = client.get("/devices/chamber-01/control/state", headers=OPERATOR).json()
    assert state["setpoints"]["temp_c"] == 19


def test_manual_override_is_audited_and_interlocked(client, device):
    device.chamber.state.temp_c = load_envelope().max_temp_c + 1
    device.tick()
    r = client.post("/devices/chamber-01/override", headers=OPERATOR,
                    params={"actuator": "heater", "state": True}).json()
    assert r["applied"] is False
    assert r["veto"] == "over_temp_interlock"

    device.chamber.state.temp_c = 18.0
    device.tick()
    r = client.post("/devices/chamber-01/override", headers=OPERATOR,
                    params={"actuator": "humidifier", "state": True}).json()
    assert r["applied"] is True
    overrides = client.get("/audit", headers=OPERATOR, params={"action": "control.manual_override"}).json()
    assert len(overrides) == 1  # the vetoed attempt is not an override, it is a veto
    assert overrides[0]["actor"] == "operator"
    vetoes = client.get("/audit", headers=OPERATOR, params={"action": "override.vetoed"}).json()
    assert len(vetoes) == 1 and vetoes[0]["actor"] == "safety"


def test_unsafe_recipe_authoring_is_refused_and_audited(client, device):
    r = client.post("/recipes", headers=OPERATOR, json={
        "recipe_id": "R-BAD", "name": "Too hot", "stage": "PINNING_ACTIVE",
        "setpoints": {"temp_c": 34, "humidity_pct": 92, "co2_ceiling_ppm": 900, "fan_pct": 45, "light_pct": 60},
    })
    assert r.status_code == 422
    assert r.json()["detail"]["violations"][0]["rule"] == "temp_range"
    audit = client.get("/audit", headers=OPERATOR, params={"action": "recipe.vetoed"}).json()
    assert len(audit) == 1
