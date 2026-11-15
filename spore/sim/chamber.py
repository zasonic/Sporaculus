"""Simulated grow chamber + edge device (Phase 0 exit criterion).

Physically-planted first-order models let every future module be built and
tested without hardware:

    temperature' = k_heater*(heater_on) - k_loss*(T - T_ambient)
    humidity'    = k_humid*(humidifier_on) + k_mist - k_vent*(RH - RH_ambient) - drying(T)
    co2'         = respiration(mycelium load, stage) - k_fan*FAE*(CO2 - CO2_ambient)

The SimulatedDevice speaks the exact MQTT payload contracts from
spore/schemas (telemetry batches, health heartbeats, command handling with
local re-validation — defense in depth), so Phase 2's gateway can drive it
unchanged once a real broker exists. It also supports fault injection for
every row of docs/failure_modes.md.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from spore.schemas.commands import AckMessage, CommandAction, CommandMessage
from spore.schemas.safety import SafetyEnvelope, load_envelope, validate_command_payload
from spore.schemas.telemetry import ActuatorHealth, DeviceHealthMessage, SensorHealth, TelemetryBatch


@dataclass
class ChamberState:
    temp_c: float = 21.0
    rh: float = 65.0
    co2_ppm: float = 600.0
    mycelium_load: float = 0.0   # 0..1 colonization fraction
    pin_factor: float = 1.0      # respiration multiplier during fruiting


@dataclass
class Faults:
    """Fault injection switches (all default off)."""

    temp_sensor_disconnected: bool = False
    co2_sensor_stuck_value: Optional[float] = None       # e.g. NaN or 99999
    humidity_sensor_saturated: bool = False              # pinned at 100%
    heater_stuck_on: bool = False
    humidifier_stuck_on: bool = False
    fan_failed: bool = False
    offline: bool = False            # simulates network loss (no heartbeats)
    backend_crash: bool = False      # controller loop stops; watchdog must catch it
    ack_drop_rate: float = 0.0       # flaky device simulation
    latency_ms: float = 0.0


@dataclass
class SimulatedChamber:
    """One chamber with actuators and sensors. Pure physics; no I/O."""

    state: ChamberState = field(default_factory=ChamberState)
    ambient_temp_c: float = 18.0
    ambient_rh: float = 45.0
    ambient_co2_ppm: float = 420.0
    actuators: dict[str, bool] = field(
        default_factory=lambda: {k: False for k in
                                 ("heater", "humidifier", "fan", "dehumidifier", "light", "misting")}
    )
    fae: float = 2.0                    # fresh-air exchanges/hour achieved by fan profile
    faults: Faults = field(default_factory=Faults)
    runtime_s: dict[str, float] = field(default_factory=dict)
    envelope: SafetyEnvelope = field(default_factory=load_envelope)

    # thermal/moisture/gas coefficients (tunable per device profile)
    k_heater: float = 0.35
    k_loss: float = 0.02
    k_humid: float = 0.9
    k_mist: float = 0.4
    k_vent_rh: float = 0.05
    k_dry: float = 0.004
    resp_base: float = 6.0              # ppm CO2 produced per minute at full colonization
    k_fan: float = 0.03

    def step(self, dt_minutes: float = 1.0) -> None:
        if self.faults.offline and not self.faults.backend_crash:
            pass  # physics continues even when unreachable
        a = self.actuators
        heater_on = a["heater"] or self.faults.heater_stuck_on
        humid_on = a["humidifier"] or self.faults.humidifier_stuck_on
        fan_on = a["fan"] and not self.faults.fan_failed
        fae = self.fae if fan_on else 0.3

        s = self.state
        d_t = (self.k_heater * heater_on - self.k_loss * (s.temp_c - self.ambient_temp_c)) * dt_minutes
        s.temp_c += d_t
        d_rh = (self.k_humid * humid_on + self.k_mist * a["misting"]
                - self.k_vent_rh * fae * (s.rh - self.ambient_rh) / 10.0
                - self.k_dry * max(s.temp_c - self.ambient_temp_c, 0) * dt_minutes)
        s.rh = min(100.0, max(0.0, s.rh + d_rh))
        resp = self.resp_base * (0.2 + s.mycelium_load) * s.pin_factor
        d_co2 = (resp - self.k_fan * fae * (s.co2_ppm - self.ambient_co2_ppm) / 10.0) * dt_minutes
        s.co2_ppm = max(self.ambient_co2_ppm, s.co2_ppm + d_co2)

        for name, on in a.items():
            eff = on or (name == "heater" and self.faults.heater_stuck_on) or \
                  (name == "humidifier" and self.faults.humidifier_stuck_on)
            if eff:
                self.runtime_s[name] = self.runtime_s.get(name, 0.0) + dt_minutes * 60

    # --- sensor readings with fault behavior -------------------------------
    def read_sensors(self) -> dict[str, float]:
        f, s = self.faults, self.state
        temp = float("nan") if f.temp_sensor_disconnected else s.temp_c
        rh = 100.0 if f.humidity_sensor_saturated else s.rh
        co2 = f.co2_sensor_stuck_value if f.co2_sensor_stuck_value is not None else s.co2_ppm
        return {"temperature": temp, "humidity": rh, "co2": co2}


class SimulatedDevice:
    """Edge agent speaking the AsyncAPI contract payloads against a local bus.

    `bus` is any object with publish(topic: str, payload: dict); tests use an
    in-memory recorder, Phase 2 swaps in paho-mqtt without touching this class.
    """

    def __init__(self, device_id: str, chamber: SimulatedChamber, bus: "Recorder",
                 clock: Optional[Callable[[], datetime]] = None):
        self.device_id = device_id
        self.chamber = chamber
        self.bus = bus
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.fail_safe_active = False
        self._last_seen: dict[str, datetime] = {}
        self._seen_seq: dict[tuple, int] = {}
        self.local_seq = 0

    # ---- inbound ----------------------------------------------------------
    def handle_command(self, msg: CommandMessage) -> Optional[AckMessage]:
        if self.chamber.faults.offline or self.chamber.faults.backend_crash:
            return None  # no ack -> gateway marks TIMED_OUT -> watchdog path
        if msg.command_id in self._last_seen:
            dup = self._last_seen[msg.command_id]
            # idempotent: re-ack same result, never double-execute
            return AckMessage(command_id=msg.command_id, success=True,
                              reason="IDEMPOTENT_DUPLICATE", executed_at=self.clock())
        age = (self.clock() - msg.ts).total_seconds()
        if age > msg.ttl_seconds:
            return self._ack(msg, success=False, reason="STALE_COMMAND")
        # defense in depth: edge re-validates against its own envelope copy
        violations = validate_command_payload(msg.action.value, msg.payload, self.chamber.envelope)
        if violations:
            return self._ack(msg, success=False, reason="SAFETY_LOCAL_VETO",
                             resulting_state={"violations": [v.model_dump() for v in violations]})
        self._execute(msg)
        self._last_seen[msg.command_id] = self.clock()
        if random.random() < self.chamber.faults.ack_drop_rate:
            return None
        return self._ack(msg, success=True)

    def _execute(self, msg: CommandMessage) -> None:
        act, p = msg.action, msg.payload
        c = self.chamber
        if act == CommandAction.SET_SETPOINT:
            c.setpoints = getattr(c, "setpoints", {})
            c.setpoints[p["metric"]] = p.get("value", p.get("value_c", p.get("value_ppm")))
        elif act == CommandAction.SET_ACTUATOR:
            c.actuators[p["actuator"]] = p["state"] == "on"
        elif act in (CommandAction.STOP_ALL, CommandAction.ENTER_FAIL_SAFE):
            for k in c.actuators:
                if k == "fan":
                    continue  # fan handled below (fail-open vs stop semantics)
                c.actuators[k] = False
            if act == CommandAction.ENTER_FAIL_SAFE:
                c.actuators["fan"] = True   # fans fail open per envelope
                self.fail_safe_active = True
            else:
                c.actuators["fan"] = False
        elif act == CommandAction.CLEAR_FAIL_SAFE:
            self.fail_safe_active = False

    def _ack(self, msg: CommandMessage, success: bool, reason: str = "",
             resulting_state: Optional[dict] = None) -> AckMessage:
        ack = AckMessage(command_id=msg.command_id, success=success, reason=reason or None,
                         executed_at=self.clock(), resulting_state=resulting_state or {})
        self.bus.publish(f"farm/devices/{self.device_id}/commands/{msg.command_id}/ack",
                         ack.model_dump(mode="json"))
        return ack

    # ---- outbound ---------------------------------------------------------
    def tick(self, dt_minutes: float = 1.0) -> None:
        if self.chamber.faults.offline or self.chamber.faults.backend_crash:
            return
        self.chamber.step(dt_minutes)
        now = self.clock()
        self.local_seq += 1
        readings = []
        vals = self.chamber.read_sensors()
        units = {"temperature": "C", "humidity": "percent", "co2": "ppm"}
        for metric, value in vals.items():
            readings.append({"sensor_id": f"{metric}-0", "metric": metric,
                             "value": value, "unit": units[metric], "local_seq": self.local_seq})
        batch = TelemetryBatch(device_id=self.device_id, ts=now, readings=readings)
        self.bus.publish(f"farm/devices/{self.device_id}/telemetry", batch.model_dump(mode="json"))
        health = DeviceHealthMessage(
            device_id=self.device_id, ts=now, uptime_s=int(now.timestamp()),
            fail_safe_active=self.fail_safe_active,
            sensors=[SensorHealth(sensor_id=f"{m}-0", ok=not (math.isnan(v)))
                     for m, v in vals.items()],
            actuators=[ActuatorHealth(actuator_id=f"{k}-rly", kind=k,
                                      state="fault" if (k == "heater" and self.chamber.faults.heater_stuck_on
                                                        or k == "humidifier" and self.chamber.faults.humidifier_stuck_on
                                                        or k == "fan" and self.chamber.faults.fan_failed and self.chamber.actuators["fan"])
                                      else ("on" if self.chamber.actuators[k] else "off"),
                                      runtime_total_s=int(self.chamber.runtime_s.get(k, 0)))
                       for k in self.chamber.actuators],
        )
        self.bus.publish(f"farm/devices/{self.device_id}/health", health.model_dump(mode="json"))


class Recorder:
    """In-memory MQTT stand-in: records (topic, payload) pairs."""

    def __init__(self) -> None:
        self.messages: list[tuple[str, dict[str, Any]]] = []

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self.messages.append((topic, payload))

    def topics(self) -> set[str]:
        return {t for t, _ in self.messages}

    def last(self, topic_suffix: str) -> dict[str, Any]:
        for t, p in reversed(self.messages):
            if t.endswith(topic_suffix):
                return p
        raise KeyError(topic_suffix)
