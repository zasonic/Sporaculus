"""Simulated chamber physics + independent hardware safety (Phase 0 deliverable).

Deliberately models the things that make hardware dangerous, not just the things
that make dashboards pretty:

* welded relays (``heater_stuck``) that ignore commands
* sensors that lie plausibly (saturation, invalid CO2) instead of going null
* a hardware watchdog that fires when the *backend* goes silent
* a fail-safe that cuts the relay board supply — the only thing that can defeat
  a welded relay

The chamber enforces its own copy of the envelope. It never asks the backend
whether an action is safe.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from ..safety import fail_safe_state, load_envelope
from ..schemas import SafetyEnvelope, Setpoints

FAULTS = (
    "sensor_disconnect", "co2_invalid", "rh_saturation",
    "heater_stuck", "humidifier_stuck", "fan_failure",
    "power_loss", "backend_crash",
)


@dataclass
class ChamberState:
    temp_c: float = 21.4
    humidity_pct: float = 91.0
    co2_ppm: float = 940.0
    heater: bool = False
    humidifier: bool = False
    fan: bool = True
    light: bool = True
    fan_pct: int = 45
    light_pct: int = 60
    uptime_s: float = 0.0
    seq: int = 0
    heater_runtime_s: float = 0.0
    humidifier_runtime_s: float = 0.0
    watchdog_remaining_s: float = 60.0
    fail_safe: bool = False
    veto_log: list[str] = field(default_factory=list)


class SimChamber:
    def __init__(self, envelope: SafetyEnvelope | None = None, *, watchdog_timeout_s: float = 60.0,
                 noise: float = 0.4, seed: int | None = None) -> None:
        self.env = envelope or load_envelope()
        self.watchdog_timeout_s = watchdog_timeout_s
        self.noise = noise
        self.rng = random.Random(seed)
        self.state = ChamberState(watchdog_remaining_s=watchdog_timeout_s)
        self.faults: set[str] = set()
        self.setpoints = Setpoints(temp_c=19, humidity_pct=92, co2_ceiling_ppm=900, fan_pct=45, light_pct=60)
        self._backend_silence_s = 0.0

    # -- external inputs ---------------------------------------------------
    def backend_contact(self) -> None:
        """Any command or backend heartbeat re-arms the watchdog."""
        self._backend_silence_s = 0.0
        self.state.watchdog_remaining_s = self.watchdog_timeout_s
        if self.state.fail_safe:
            self.state.fail_safe = False
            self.state.veto_log.append("watchdog_rearmed")

    def apply_setpoints(self, sp: Setpoints) -> None:
        self.setpoints = sp
        self.state.fan_pct = sp.fan_pct
        self.state.light_pct = sp.light_pct
        self.state.fan = sp.fan_pct > 0
        self.state.light = sp.light_pct > 0

    def set_actuator(self, name: str, on: bool) -> tuple[bool, str | None]:
        """Returns (ok, error). A welded relay refuses to open."""
        if name in ("heater", "humidifier") and not on and f"{name}_stuck" in self.faults:
            return False, f"{name} relay feedback mismatch (stuck on)"
        if name == "fan" and "fan_failure" in self.faults:
            return False, "fan tachometer reports 0 rpm"
        setattr(self.state, name, bool(on))
        return True, None

    def set_fault(self, name: str, enabled: bool = True) -> None:
        if name not in FAULTS:
            raise ValueError(f"unknown fault {name}; known: {FAULTS}")
        if enabled:
            self.faults.add(name)
        else:
            self.faults.discard(name)

    # -- physics -----------------------------------------------------------
    def tick(self, dt: float = 1.0) -> ChamberState:
        s, env = self.state, self.env
        s.uptime_s += dt
        s.seq += 1

        # watchdog: silence from the backend is a failure, not an absence
        self._backend_silence_s += dt
        s.watchdog_remaining_s = max(0.0, self.watchdog_timeout_s - self._backend_silence_s)
        if s.watchdog_remaining_s <= 0 and not s.fail_safe:
            s.fail_safe = True
            s.veto_log.append("watchdog_timeout_fail_safe")

        if s.fail_safe:
            # Cut the relay board supply: overrides even welded relays.
            safe = fail_safe_state()
            s.heater, s.humidifier = False, False
            s.fan, s.light = True, False
            s.fan_pct, s.light_pct = safe["fan_pct"], safe["light_pct"]
        else:
            if "heater_stuck" in self.faults:
                s.heater = True
            if "humidifier_stuck" in self.faults:
                s.humidifier = True
            if "fan_failure" in self.faults:
                s.fan, s.fan_pct = False, 0

            # hardware interlocks, independent of the backend
            if s.temp_c >= env.max_temp_c and s.heater:
                s.heater = False
                s.veto_log.append("hw_over_temp")
            if s.humidity_pct >= env.max_humidity_pct and s.humidifier:
                s.humidifier = False
                s.veto_log.append("hw_over_humidity")
            if s.co2_ppm > env.max_co2_ppm:
                s.fan, s.fan_pct = True, 100
                s.veto_log.append("hw_co2_purge")

        s.heater_runtime_s = s.heater_runtime_s + dt if s.heater else 0.0
        s.humidifier_runtime_s = s.humidifier_runtime_s + dt if s.humidifier else 0.0
        for name, limit in (("heater", env.max_heater_runtime_min * 60),
                            ("humidifier", env.max_humidifier_runtime_min * 60)):
            if getattr(s, f"{name}_runtime_s") >= limit and getattr(s, name):
                setattr(s, name, False)
                s.veto_log.append(f"hw_{name}_runtime_limit")

        n = self.noise
        s.temp_c += ((0.18 if s.heater else -0.05) - s.fan_pct * 0.0008) * dt + self.rng.uniform(-n, n) * 0.1
        s.humidity_pct += ((0.35 if s.humidifier else -0.12) - s.fan_pct * 0.004) * dt + self.rng.uniform(-n, n) * 0.3
        s.co2_ppm += (14 - s.fan_pct * 0.55) * dt + self.rng.uniform(-n, n) * 8
        s.temp_c = min(max(s.temp_c, 5.0), 45.0)
        s.humidity_pct = min(max(s.humidity_pct, 20.0), 100.0)
        s.co2_ppm = min(max(s.co2_ppm, 400.0), 6000.0)
        if len(s.veto_log) > 50:
            del s.veto_log[:-50]
        return s

    # -- sensors -----------------------------------------------------------
    def reading(self) -> dict[str, Any]:
        s = self.state
        temp: float | None = round(s.temp_c, 2)
        rh: float | None = round(s.humidity_pct, 1)
        co2: int | None = int(round(s.co2_ppm))
        flags: list[str] = []
        if "sensor_disconnect" in self.faults:
            temp, rh = None, None
            flags.append("device_sensor_disconnect")
        if "rh_saturation" in self.faults:
            rh = 99.9
            flags.append("device_rh_saturated")
        if "co2_invalid" in self.faults:
            co2 = int(max(0, co2 + self.rng.randint(-900, 2500)))
            flags.append("device_co2_implausible")
        return {
            "temperature_c": temp, "humidity_pct": rh, "co2_ppm": co2,
            "fan_pct": s.fan_pct, "light_pct": s.light_pct,
            "actuators": {"heater": s.heater, "humidifier": s.humidifier, "fan": s.fan, "light": s.light},
            "quality_flags": flags,
        }

    def sensor_health(self) -> dict[str, float]:
        return {
            "temperature": 0.0 if "sensor_disconnect" in self.faults else 1.0,
            "humidity": 0.2 if "rh_saturation" in self.faults else (0.0 if "sensor_disconnect" in self.faults else 1.0),
            "co2": 0.1 if "co2_invalid" in self.faults else 1.0,
        }
