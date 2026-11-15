"""Devices module (Phase 2): registry, tokens, sensors, heartbeat, offline detection."""
from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, mint_device_token, hash_token, require_admin, require_operator
from ..config import Settings, get_settings
from ..db import get_session
from ..models import Actuator, Device, DeviceHealthRow, Sensor
from ..schemas import (
    CalibrationUpdate,
    DeviceCreate,
    DeviceHealth,
    DeviceRead,
    DeviceStatus,
    DeviceToken,
    SensorRead,
    as_utc,
    utcnow,
)
from . import audit

router = APIRouter(prefix="/devices", tags=["devices"])

SENSOR_UNITS = {"temperature": "C", "humidity": "%", "co2": "ppm", "substrate_moisture": "%"}
SENSOR_FIELD = {
    "temperature": "temperature_c",
    "humidity": "humidity_pct",
    "co2": "co2_ppm",
    "substrate_moisture": "substrate_moisture_pct",
}


def register(session: Session, payload: DeviceCreate, actor: str, secret: str) -> tuple[Device, str]:
    if session.get(Device, payload.device_id) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"device {payload.device_id} already registered")
    token = mint_device_token()
    device = Device(
        device_id=payload.device_id,
        name=payload.name,
        edge_stack=payload.edge_stack,
        firmware=payload.firmware,
        token_hash=hash_token(token, secret),
        status=DeviceStatus.UNKNOWN.value,
        meta={"sensors": payload.sensors, "actuators": payload.actuators},
    )
    session.add(device)
    for kind in payload.sensors:
        session.add(Sensor(
            sensor_id=f"{payload.device_id}:{kind}", device_id=payload.device_id, kind=kind,
            unit=SENSOR_UNITS.get(kind, ""),
        ))
    for kind in payload.actuators:
        session.add(Actuator(actuator_id=f"{payload.device_id}:{kind}", device_id=payload.device_id, kind=kind))
    audit.record(session, actor, "device.register", payload.device_id, edge_stack=payload.edge_stack)
    session.flush()
    return device, token


def get_or_404(session: Session, device_id: str) -> Device:
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device {device_id}")
    return device


def touch(session: Session, device_id: str, *, firmware: str | None = None) -> Device | None:
    device = session.get(Device, device_id)
    if device is None:
        return None
    device.last_seen_at = utcnow()
    if device.status in (DeviceStatus.UNKNOWN.value, DeviceStatus.OFFLINE.value):
        device.status = DeviceStatus.ONLINE.value
        audit.record(session, "gateway", "device.online", device_id)
    if firmware:
        device.firmware = firmware
    return device


def record_health(session: Session, health: DeviceHealth) -> DeviceHealthRow:
    row = DeviceHealthRow(
        device_id=health.device_id, ts=health.ts, uptime_s=health.uptime_s, firmware=health.firmware,
        sensor_health=health.sensor_health, watchdog_state=health.watchdog_state,
        watchdog_remaining_s=health.watchdog_remaining_s, safe_state=health.safe_state, faults=health.faults,
    )
    session.add(row)
    device = touch(session, health.device_id, firmware=health.firmware)
    if device is not None:
        prev = device.watchdog_state
        device.watchdog_state = health.watchdog_state
        if health.watchdog_state == "fail_safe":
            device.status = DeviceStatus.FAIL_SAFE.value
            if prev != "fail_safe":
                audit.record(session, "device", "device.fail_safe", health.device_id,
                             faults=health.faults, safe_state=health.safe_state)
        elif device.status == DeviceStatus.FAIL_SAFE.value:
            device.status = DeviceStatus.ONLINE.value
            audit.record(session, "device", "device.recovered", health.device_id)
    for kind, score in (health.sensor_health or {}).items():
        sensor = session.get(Sensor, f"{health.device_id}:{kind}")
        if sensor is not None:
            sensor.health_score = float(score)
            sensor.last_seen_at = utcnow()
    return row


def mark_offline(session: Session, timeout_s: int) -> list[str]:
    """Heartbeat sweep: silence is a failure signal, not an absence of one."""
    cutoff = utcnow() - timedelta(seconds=timeout_s)
    stale = session.scalars(
        select(Device).where(
            Device.status.in_([DeviceStatus.ONLINE.value, DeviceStatus.FAIL_SAFE.value]),
            Device.last_seen_at.is_not(None),
        )
    ).all()
    changed: list[str] = []
    for d in stale:
        seen = as_utc(d.last_seen_at)
        if seen and seen < cutoff and d.status != DeviceStatus.OFFLINE.value:
            d.status = DeviceStatus.OFFLINE.value
            audit.record(session, "gateway", "device.offline", d.device_id,
                         last_seen_at=seen.isoformat(), timeout_s=timeout_s)
            changed.append(d.device_id)
    return changed


def latest_health(session: Session, device_id: str) -> DeviceHealthRow | None:
    return session.scalars(
        select(DeviceHealthRow).where(DeviceHealthRow.device_id == device_id)
        .order_by(DeviceHealthRow.ts.desc(), DeviceHealthRow.id.desc()).limit(1)
    ).first()


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("", response_model=DeviceToken, status_code=status.HTTP_201_CREATED)
def register_device(
    payload: DeviceCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_operator),
    settings: Settings = Depends(get_settings),
) -> DeviceToken:
    device, token = register(session, payload, principal.actor, settings.device_token_secret)
    return DeviceToken(device_id=device.device_id, token=token)


@router.get("", response_model=list[DeviceRead])
def list_devices(
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[Device]:
    return list(session.scalars(select(Device).order_by(Device.registered_at)))


@router.get("/{device_id}", response_model=DeviceRead)
def get_device(
    device_id: str,
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> Device:
    return get_or_404(session, device_id)


@router.post("/{device_id}/token", response_model=DeviceToken)
def rotate_token(
    device_id: str,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings),
) -> DeviceToken:
    device = get_or_404(session, device_id)
    token = mint_device_token()
    device.token_hash = hash_token(token, settings.device_token_secret)
    audit.record(session, principal.actor, "device.token_rotate", device_id)
    return DeviceToken(device_id=device_id, token=token)


@router.get("/{device_id}/sensors", response_model=list[SensorRead])
def list_sensors(
    device_id: str,
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[Sensor]:
    get_or_404(session, device_id)
    return list(session.scalars(select(Sensor).where(Sensor.device_id == device_id).order_by(Sensor.kind)))


@router.patch("/{device_id}/sensors/{kind}", response_model=SensorRead)
def calibrate(
    device_id: str,
    kind: str,
    payload: CalibrationUpdate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_operator),
) -> Sensor:
    sensor = session.get(Sensor, f"{device_id}:{kind}")
    if sensor is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown sensor {device_id}:{kind}")
    sensor.calibration_offset = payload.offset
    sensor.calibration_scale = payload.scale
    audit.record(session, principal.actor, "sensor.calibrate", sensor.sensor_id,
                 offset=payload.offset, scale=payload.scale)
    return sensor


@router.get("/{device_id}/health", response_model=dict[str, Any])
def device_health(
    device_id: str,
    limit: int = Query(default=1, ge=1, le=200),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> dict[str, Any]:
    device = get_or_404(session, device_id)
    rows = list(session.scalars(
        select(DeviceHealthRow).where(DeviceHealthRow.device_id == device_id)
        .order_by(DeviceHealthRow.ts.desc(), DeviceHealthRow.id.desc()).limit(limit)
    ))
    return {
        "device_id": device_id,
        "status": device.status,
        "watchdog_state": device.watchdog_state,
        "last_seen_at": device.last_seen_at,
        "samples": [
            {
                "ts": r.ts, "uptime_s": r.uptime_s, "firmware": r.firmware,
                "sensor_health": r.sensor_health, "watchdog_state": r.watchdog_state,
                "watchdog_remaining_s": r.watchdog_remaining_s, "safe_state": r.safe_state, "faults": r.faults,
            }
            for r in rows
        ],
    }
