"""Simulation control surface (explicitly *not* a production API).

Exposed only when ``MYCOLOOP_SIM_ENABLED=1``. Lets the operator console inject
faults, step the chamber and watch the hardware watchdog fire — the Phase 0
hardware-in-the-loop plan, executable without hardware.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import Principal, require_operator
from ..config import Settings, get_settings
from ..db import session_scope
from ..schemas import DeviceCreate
from ..sim import FAULTS, SimDevice
from . import devices

router = APIRouter(prefix="/sim", tags=["simulation"])


def _devices(request: Request) -> dict[str, SimDevice]:
    return getattr(request.app.state, "sim_devices", {})


def _get(request: Request, device_id: str) -> SimDevice:
    dev = _devices(request).get(device_id)
    if dev is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no simulated device {device_id}")
    return dev


def spawn(request: Request, settings: Settings, device_id: str, *, speed: float | None = None) -> SimDevice:
    """Register the device in the registry (if needed) and attach a sim client."""
    registry = _devices(request)
    if device_id in registry:
        return registry[device_id]
    with session_scope() as session:
        existing = session.get(devices.Device, device_id)
        if existing is None:
            _, token = devices.register(
                session,
                DeviceCreate(device_id=device_id, name=f"Simulated chamber {device_id}", edge_stack="simulated",
                             firmware="sim-1.0.0"),
                "simulator", settings.device_token_secret,
            )
        else:
            token = "sim-internal"  # loopback bus does not authenticate; MQTT does
    dev = SimDevice(request.app.state.bus, device_id, token)
    dev.run(speed=speed if speed is not None else settings.sim_speed)
    registry[device_id] = dev
    return dev


@router.get("/devices")
def list_sim(request: Request, _: Principal = Depends(require_operator)) -> dict[str, Any]:
    return {"faults_available": list(FAULTS), "devices": [d.snapshot() for d in _devices(request).values()]}


@router.post("/devices/{device_id}")
def spawn_sim(device_id: str, request: Request, speed: float | None = None,
              _: Principal = Depends(require_operator),
              settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    return spawn(request, settings, device_id, speed=speed).snapshot()


@router.get("/devices/{device_id}")
def sim_snapshot(device_id: str, request: Request, _: Principal = Depends(require_operator)) -> dict[str, Any]:
    return _get(request, device_id).snapshot()


@router.post("/devices/{device_id}/faults")
def set_fault(device_id: str, name: str, enabled: bool = True, request: Request = None,
              _: Principal = Depends(require_operator)) -> dict[str, Any]:
    dev = _get(request, device_id)
    if name not in FAULTS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown fault; choose from {list(FAULTS)}")
    dev.set_fault(name, enabled)
    return dev.snapshot()


@router.post("/devices/{device_id}/tick")
def tick(device_id: str, n: int = 1, request: Request = None,
         _: Principal = Depends(require_operator)) -> dict[str, Any]:
    dev = _get(request, device_id)
    for _ in range(max(1, min(n, 600))):
        dev.tick()
    return dev.snapshot()


@router.post("/devices/{device_id}/stop")
def stop_sim(device_id: str, request: Request, _: Principal = Depends(require_operator)) -> dict[str, Any]:
    dev = _get(request, device_id)
    dev.stop()
    return dev.snapshot()
