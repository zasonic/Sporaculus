"""Deterministic safety layer (Phase 0 envelope + Phase 4 interlocks).

Chain of authority — never bypassed:

    optimizer recommends -> safety validates -> human approves
    -> control engine executes -> device hardware watchdog protects

Nothing in this module performs I/O. It is pure, deterministic and unit-tested,
which is what makes it trustworthy enough to sit between an optimizer and a
relay.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from .config import get_settings
from .schemas import SafetyEnvelope, SafetyVerdict, Setpoints, Violation

ACTUATORS = ("heater", "humidifier", "fan", "light")


@lru_cache
def load_envelope(path: str | None = None) -> SafetyEnvelope:
    p = Path(path) if path else get_settings().envelope_path
    if p.exists():
        return SafetyEnvelope.model_validate(yaml.safe_load(p.read_text()) or {})
    return SafetyEnvelope()


def fail_safe_state() -> dict[str, Any]:
    """The state hardware falls back to when sensors or backend are untrusted."""
    return {"heater": False, "humidifier": False, "fan": True, "light": False, "fan_pct": 100, "light_pct": 0}


def validate_setpoints(sp: Setpoints | dict[str, Any], env: SafetyEnvelope | None = None) -> SafetyVerdict:
    """Biological safety validation. This is what may veto the optimizer."""
    env = env or load_envelope()
    sp = sp if isinstance(sp, Setpoints) else Setpoints.model_validate(sp)
    v: list[Violation] = []

    if not (env.min_temp_c <= sp.temp_c <= env.max_temp_c):
        v.append(Violation(field="temp_c", value=sp.temp_c, rule="temp_range",
                           message=f"temperature {sp.temp_c}C outside [{env.min_temp_c}, {env.max_temp_c}]"))
    if not (env.min_humidity_pct <= sp.humidity_pct <= env.max_humidity_pct):
        v.append(Violation(field="humidity_pct", value=sp.humidity_pct, rule="humidity_range",
                           message=f"humidity {sp.humidity_pct}% outside [{env.min_humidity_pct}, {env.max_humidity_pct}]"))
    if sp.co2_ceiling_ppm > env.max_co2_ppm:
        v.append(Violation(field="co2_ceiling_ppm", value=sp.co2_ceiling_ppm, rule="co2_ceiling",
                           message=f"CO2 ceiling {sp.co2_ceiling_ppm}ppm above {env.max_co2_ppm}ppm"))
    if sp.fan_pct < env.min_fae_pct:
        v.append(Violation(field="fan_pct", value=sp.fan_pct, rule="min_fae",
                           message=f"fresh air exchange {sp.fan_pct}% below minimum {env.min_fae_pct}%"))
    return SafetyVerdict(ok=not v, violations=v)


@dataclass
class ActuatorRuntime:
    """Per-device runtime/cooldown bookkeeping used by the interlock layer."""

    on_since_s: dict[str, float] = field(default_factory=dict)
    runtime_s: dict[str, float] = field(default_factory=dict)
    last_change_s: dict[str, float] = field(default_factory=dict)
    last_state: dict[str, bool] = field(default_factory=dict)

    def observe(self, state: dict[str, bool], now_s: float, dt_s: float) -> None:
        for name in ACTUATORS:
            on = bool(state.get(name, False))
            prev = self.last_state.get(name, False)
            if on:
                self.runtime_s[name] = self.runtime_s.get(name, 0.0) + dt_s
            elif prev:
                self.runtime_s[name] = 0.0
            if on != prev:
                self.last_change_s[name] = now_s
            self.last_state[name] = on


def evaluate_actuators(
    proposed: dict[str, Any],
    *,
    temp_c: float | None,
    humidity_pct: float | None,
    co2_ppm: float | None,
    sensors_valid: bool,
    runtime: ActuatorRuntime,
    now_s: float,
    env: SafetyEnvelope | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Apply interlocks to a proposed actuator state.

    Returns ``(allowed_state, vetoes)``. A non-empty ``vetoes`` list means the
    safety layer overrode the proposal — that is a *successful* intervention,
    not an unsafe event. Unsafe actuation is only when a veto fails to hold.
    """
    env = env or load_envelope()
    state = {name: bool(proposed.get(name, False)) for name in ACTUATORS}
    fan_pct = int(proposed.get("fan_pct", 0))
    light_pct = int(proposed.get("light_pct", 0))
    vetoes: list[str] = []

    # 1. Untrusted sensors -> fail safe, always.
    if not sensors_valid:
        safe = fail_safe_state()
        return {**safe}, ["sensor_invalid_fail_safe"]

    # 2. Over-temperature / over-humidity interlocks (independent of setpoints).
    if temp_c is not None and temp_c >= env.max_temp_c and state["heater"]:
        state["heater"] = False
        vetoes.append("over_temp_interlock")
    if humidity_pct is not None and humidity_pct >= env.max_humidity_pct and state["humidifier"]:
        state["humidifier"] = False
        vetoes.append("over_humidity_interlock")

    # 3. CO2 purge overrides the recipe.
    if co2_ppm is not None and co2_ppm > env.max_co2_ppm:
        state["fan"] = True
        fan_pct = 100
        vetoes.append("co2_purge")

    # 4. Minimum fresh air exchange.
    if fan_pct < env.min_fae_pct:
        fan_pct = env.min_fae_pct
        state["fan"] = True
        vetoes.append("min_fae_enforced")

    # 5. Continuous runtime limits (stuck-actuator protection).
    limits = {"heater": env.max_heater_runtime_min * 60, "humidifier": env.max_humidifier_runtime_min * 60}
    for name, limit in limits.items():
        # judged against the *proposal*, so an audit shows every reason at once
        if proposed.get(name) and runtime.runtime_s.get(name, 0.0) >= limit:
            state[name] = False
            vetoes.append(f"{name}_runtime_limit")

    # 6. Anti-flapping / cooldown.
    for name in ACTUATORS:
        last = runtime.last_change_s.get(name)
        if last is None:
            continue
        if state[name] != runtime.last_state.get(name, False) and (now_s - last) < env.actuator_cooldown_s:
            state[name] = runtime.last_state.get(name, False)
            vetoes.append(f"{name}_cooldown")

    return {**state, "fan_pct": fan_pct, "light_pct": light_pct}, vetoes
