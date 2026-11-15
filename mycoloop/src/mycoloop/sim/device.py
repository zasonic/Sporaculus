"""Simulated device client — speaks the real AsyncAPI contract over the bus.

This is the Phase 0 "hardware-in-the-loop without hardware" deliverable. It is a
*separate actor*: it owns its own idempotency cache, acks received/applied/failed,
publishes retained status with a last-will equivalent, and keeps running (and
goes fail-safe) when the backend disappears.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from ..bus import Message, MessageBus
from ..schemas import Setpoints, as_utc, utcnow
from .. import topics
from .chamber import SimChamber

log = logging.getLogger(__name__)


class SimDevice:
    def __init__(self, bus: MessageBus, device_id: str, token: str, *,
                 chamber: SimChamber | None = None, firmware: str = "sim-1.0.0",
                 health_every: int = 5) -> None:
        self.bus = bus
        self.device_id = device_id
        self.token = token
        self.firmware = firmware
        self.chamber = chamber or SimChamber(seed=hash(device_id) % 10000)
        self.health_every = health_every
        self.applied: dict[str, dict[str, Any]] = {}  # command_id -> result (idempotency)
        self.received_commands: list[str] = []
        self._ticks = 0
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.online = False

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        self.bus.subscribe(topics.commands(self.device_id), self._on_command)
        self.online = True
        self._publish_status("online")

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        if self.online:
            self._publish_status("offline", reason="device shutdown")
        self.online = False

    def run(self, speed: float = 1.0) -> None:
        """Background loop: 1 simulated second per (1/speed) wall seconds."""
        self.start()

        def loop() -> None:
            while not self._stop.wait(max(0.01, 1.0 / max(speed, 0.1))):
                try:
                    self.tick()
                except Exception:
                    log.exception("sim device tick failed")

        self._thread = threading.Thread(target=loop, name=f"sim-{self.device_id}", daemon=True)
        self._thread.start()

    # -- publishing --------------------------------------------------------
    def _powered(self) -> bool:
        return "power_loss" not in self.chamber.faults

    def _publish_status(self, state: str, reason: str | None = None) -> None:
        self.bus.publish_json(topics.status(self.device_id), {
            "device_id": self.device_id, "ts": utcnow().isoformat(), "state": state,
            "firmware": self.firmware, "reason": reason,
        }, retain=True)

    def tick(self, dt: float = 1.0) -> None:
        self.chamber.tick(dt)
        self._ticks += 1
        if not self._powered():
            return  # a device with no power publishes nothing (heartbeat gap)
        s = self.chamber.state
        payload = {
            "device_id": self.device_id, "seq": s.seq, "ts": utcnow().isoformat(),
            **self.chamber.reading(),
        }
        self.bus.publish_json(topics.telemetry(self.device_id), payload)
        if self._ticks % self.health_every == 0:
            self.bus.publish_json(topics.health(self.device_id), {
                "device_id": self.device_id, "ts": utcnow().isoformat(), "uptime_s": int(s.uptime_s),
                "firmware": self.firmware, "sensor_health": self.chamber.sensor_health(),
                "watchdog_state": "fail_safe" if s.fail_safe else "armed",
                "watchdog_remaining_s": int(s.watchdog_remaining_s),
                "safe_state": {"heater": s.heater, "humidifier": s.humidifier,
                               "fan_pct": s.fan_pct, "light_pct": s.light_pct},
                "faults": sorted(self.chamber.faults),
            })

    # -- command handling --------------------------------------------------
    def _ack(self, command_id: str, state: str, detail: str | None = None, duplicate: bool = False) -> None:
        self.bus.publish_json(topics.ack(self.device_id, command_id), {
            "command_id": command_id, "device_id": self.device_id, "state": state,
            "ts": utcnow().isoformat(), "detail": detail, "duplicate": duplicate,
        })

    def _on_command(self, msg: Message) -> None:
        if not self._powered():
            return
        try:
            data = msg.json()
        except Exception:
            return
        command_id = data.get("command_id")
        if not command_id:
            return
        self.chamber.backend_contact()
        self.received_commands.append(command_id)

        if command_id in self.applied:
            # Idempotency: a retry must never actuate twice.
            prev = self.applied[command_id]
            self._ack(command_id, prev["state"], f"idempotent replay: {prev.get('detail') or ''}".strip(), duplicate=True)
            return

        self._ack(command_id, "received")
        expires = data.get("expires_at")
        if expires:
            try:
                if as_utc(_parse(expires)) < utcnow():
                    self.applied[command_id] = {"state": "failed", "detail": "command expired before application"}
                    self._ack(command_id, "failed", "command expired before application")
                    return
            except Exception:
                pass

        kind, payload = data.get("kind"), data.get("payload") or {}
        state, detail = "applied", None
        try:
            if kind == "setpoint":
                self.chamber.apply_setpoints(Setpoints.model_validate(payload))
            elif kind == "recipe":
                self.chamber.apply_setpoints(Setpoints.model_validate(payload.get("setpoints", {})))
            elif kind == "actuator":
                ok, err = self.chamber.set_actuator(payload["actuator"], bool(payload["state"]))
                if not ok:
                    state, detail = "failed", err
            elif kind == "reboot":
                self.chamber.state.uptime_s = 0.0
                self.chamber.backend_contact()
            else:
                state, detail = "failed", f"unsupported kind {kind}"
        except Exception as exc:
            state, detail = "failed", f"{type(exc).__name__}: {exc}"

        self.applied[command_id] = {"state": state, "detail": detail}
        self._ack(command_id, state, detail)

    # -- faults ------------------------------------------------------------
    def set_fault(self, name: str, enabled: bool = True) -> None:
        if name == "backend_crash":
            # Partition the link: the device keeps running, the backend goes silent.
            if hasattr(self.bus, "set_partition"):
                self.bus.set_partition(self.device_id, enabled)  # type: ignore[attr-defined]
            if enabled:
                self.chamber.faults.add(name)
            else:
                self.chamber.faults.discard(name)
                self.chamber.backend_contact()
            return
        self.chamber.set_fault(name, enabled)

    def snapshot(self) -> dict[str, Any]:
        s = self.chamber.state
        return {
            "device_id": self.device_id, "online": self.online, "uptime_s": round(s.uptime_s, 1),
            "temp_c": round(s.temp_c, 2), "humidity_pct": round(s.humidity_pct, 1), "co2_ppm": int(s.co2_ppm),
            "actuators": {"heater": s.heater, "humidifier": s.humidifier, "fan": s.fan, "light": s.light},
            "fan_pct": s.fan_pct, "light_pct": s.light_pct,
            "watchdog_remaining_s": round(s.watchdog_remaining_s, 1), "fail_safe": s.fail_safe,
            "faults": sorted(self.chamber.faults), "commands_received": len(self.received_commands),
            "vetoes": s.veto_log[-8:],
        }


def _parse(value: str):
    from datetime import datetime

    return datetime.fromisoformat(value)
