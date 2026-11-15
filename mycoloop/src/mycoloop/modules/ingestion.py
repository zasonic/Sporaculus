"""Ingestion module (Phase 3): raw -> validated -> clean, with quarantine.

Answers the Phase 3 exit questions directly:
  * CO2 during the last pinning event      -> /telemetry/pinning-co2
  * which sensors drifted                  -> /telemetry/quality/drift
  * when humidity left the safe range      -> /telemetry/quality/excursions
  * which readings were invalid            -> /telemetry/quarantine
  * average temperature by stage           -> /telemetry/stage-summary
"""
from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth import Principal, require_device, require_operator
from ..db import get_session
from ..models import (
    Device,
    GrowCycle,
    Sensor,
    TelemetryClean,
    TelemetryQuarantine,
    TelemetryRaw,
)
from ..quality import (
    CORE_FIELDS,
    Calibration,
    CleanResult,
    DeviceQualityTracker,
    clean_reading,
)
from ..schemas import (
    DriftReport,
    IngestResult,
    QuarantineRead,
    QualityFlags,
    Stage,
    StageEnvironment,
    Telemetry,
    TelemetryBatch,
    TelemetryCleanRead,
    as_utc,
    utcnow,
)
from . import audit, devices
from .biology import active_cycle_for_device, stage_windows

router = APIRouter(prefix="/telemetry", tags=["telemetry"])

_trackers: dict[str, DeviceQualityTracker] = {}


def tracker_for(device_id: str) -> DeviceQualityTracker:
    return _trackers.setdefault(device_id, DeviceQualityTracker())


def reset_trackers() -> None:
    _trackers.clear()


def calibrations_for(session: Session, device_id: str) -> dict[str, Calibration]:
    mapping = {
        "temperature": "temperature_c",
        "humidity": "humidity_pct",
        "co2": "co2_ppm",
        "substrate_moisture": "substrate_moisture_pct",
    }
    out: dict[str, Calibration] = {}
    for sensor in session.scalars(select(Sensor).where(Sensor.device_id == device_id)):
        field = mapping.get(sensor.kind)
        if field:
            out[field] = Calibration(offset=sensor.calibration_offset, scale=sensor.calibration_scale)
    return out


def _update_sensor_health(session: Session, device_id: str, result: CleanResult) -> None:
    mapping = {
        "temperature_c": "temperature",
        "humidity_pct": "humidity",
        "co2_ppm": "co2",
        "substrate_moisture_pct": "substrate_moisture",
    }
    for field, kind in mapping.items():
        if field not in result.field_flags:
            continue
        sensor = session.get(Sensor, f"{device_id}:{kind}")
        if sensor is None:
            continue
        bad = bool(set(result.field_flags[field]) & QualityFlags.BLOCKING)
        if bad:
            sensor.invalid_count += 1
        else:
            sensor.valid_count += 1
        total = sensor.valid_count + sensor.invalid_count
        # EMA so a single glitch does not destroy a good sensor's reputation
        instant = 0.0 if bad else 1.0
        sensor.health_score = round(0.9 * sensor.health_score + 0.1 * instant, 4) if total > 1 else instant
        sensor.last_seen_at = utcnow()


def ingest_one(session: Session, t: Telemetry, *, source: str = "mqtt") -> dict[str, Any]:
    device = session.get(Device, t.device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device {t.device_id}")

    duplicate = session.scalar(
        select(TelemetryRaw.id).where(TelemetryRaw.device_id == t.device_id, TelemetryRaw.seq == t.seq)
    ) is not None

    tracker = tracker_for(t.device_id)
    result = clean_reading(t, calibrations=calibrations_for(session, t.device_id), tracker=tracker)
    if duplicate and QualityFlags.DUPLICATE not in result.flags:
        result.flags.append(QualityFlags.DUPLICATE)
        result.reasons.append("duplicate (device_id, seq) already ingested")

    blocking = sorted(set(result.flags) & QualityFlags.BLOCKING)
    usable = any(result.values.get(f) is not None for f in CORE_FIELDS)
    accepted = not duplicate and usable

    if duplicate:
        # Append-only raw table: a duplicate is *not* re-inserted, it is counted.
        return {"status": "duplicate", "seq": t.seq, "flags": result.flags}

    raw = TelemetryRaw(
        device_id=t.device_id, seq=t.seq, ts=t.ts, payload=t.model_dump(mode="json"),
        quality_flags=result.flags, accepted=accepted, source=source,
    )
    session.add(raw)
    session.flush()

    if blocking:
        session.add(TelemetryQuarantine(
            device_id=t.device_id, seq=t.seq, ts=t.ts, reasons=result.reasons or blocking,
            payload=t.model_dump(mode="json"),
        ))

    cycle = active_cycle_for_device(session, t.device_id)
    if accepted:
        session.add(TelemetryClean(
            device_id=t.device_id, raw_id=raw.id, ts=t.ts,
            temperature_c=result.values.get("temperature_c"),
            humidity_pct=result.values.get("humidity_pct"),
            co2_ppm=result.values.get("co2_ppm"),
            substrate_moisture_pct=result.values.get("substrate_moisture_pct"),
            fan_pct=t.fan_pct, light_pct=t.light_pct, actuators=t.actuators,
            quality_flags=result.flags, quality_score=result.quality_score, validated=True,
            cycle_id=cycle.cycle_id if cycle else None, stage=cycle.stage if cycle else None,
        ))

    _update_sensor_health(session, t.device_id, result)
    devices.touch(session, t.device_id)

    if not accepted:
        audit.record(session, "ingestion", "telemetry.rejected", t.device_id,
                     seq=t.seq, reasons=result.reasons)

    return {
        "status": "accepted" if accepted else "quarantined",
        "seq": t.seq,
        "flags": result.flags,
        "reasons": result.reasons,
        "quality_score": result.quality_score,
        "values": result.values,
    }


def ingest_batch(session: Session, batch: TelemetryBatch, *, source: str = "http") -> IngestResult:
    details = [ingest_one(session, t, source=source) for t in batch.items]
    return IngestResult(
        accepted=sum(1 for d in details if d["status"] == "accepted"),
        duplicates=sum(1 for d in details if d["status"] == "duplicate"),
        quarantined=sum(1 for d in details if d["status"] == "quarantined"),
        flagged=sum(1 for d in details if d.get("flags")),
        details=details,
    )


def environment_by_stage(session: Session, cycle_id: str) -> list[StageEnvironment]:
    cycle = session.get(GrowCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown cycle {cycle_id}")
    out: list[StageEnvironment] = []
    for stage, start, end in stage_windows(session, cycle_id):
        stmt = select(
            func.count(TelemetryClean.id), func.avg(TelemetryClean.temperature_c),
            func.avg(TelemetryClean.humidity_pct), func.avg(TelemetryClean.co2_ppm),
            func.min(TelemetryClean.co2_ppm), func.max(TelemetryClean.co2_ppm),
        ).where(TelemetryClean.cycle_id == cycle_id, TelemetryClean.stage == stage,
                TelemetryClean.ts >= start)
        if end is not None:
            stmt = stmt.where(TelemetryClean.ts < end)
        n, t, h, c, cmin, cmax = session.execute(stmt).one()
        out.append(StageEnvironment(
            stage=Stage(stage), samples=int(n or 0),
            avg_temp_c=round(t, 2) if t is not None else None,
            avg_humidity_pct=round(h, 2) if h is not None else None,
            avg_co2_ppm=round(c, 1) if c is not None else None,
            min_co2_ppm=cmin, max_co2_ppm=cmax,
            from_ts=start, to_ts=end,
        ))
    return out


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("", response_model=IngestResult, status_code=status.HTTP_201_CREATED)
def ingest_http(
    payload: TelemetryBatch,
    device_id: str = Query(description="device the batch belongs to"),
    device: Device = Depends(require_device),
    session: Session = Depends(get_session),
) -> IngestResult:
    for item in payload.items:
        if item.device_id != device.device_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "batch device_id mismatch")
    return ingest_batch(session, payload, source="http")


@router.get("/clean", response_model=list[TelemetryCleanRead])
def clean_rows(
    device_id: str | None = None,
    cycle_id: str | None = None,
    stage: Stage | None = None,
    limit: int = Query(default=200, ge=1, le=2000),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[TelemetryClean]:
    stmt = select(TelemetryClean).order_by(TelemetryClean.ts.desc(), TelemetryClean.id.desc()).limit(limit)
    if device_id:
        stmt = stmt.where(TelemetryClean.device_id == device_id)
    if cycle_id:
        stmt = stmt.where(TelemetryClean.cycle_id == cycle_id)
    if stage:
        stmt = stmt.where(TelemetryClean.stage == stage.value)
    return list(session.scalars(stmt))


@router.get("/quarantine", response_model=list[QuarantineRead])
def quarantine_rows(
    device_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[TelemetryQuarantine]:
    stmt = select(TelemetryQuarantine).order_by(TelemetryQuarantine.id.desc()).limit(limit)
    if device_id:
        stmt = stmt.where(TelemetryQuarantine.device_id == device_id)
    return list(session.scalars(stmt))


@router.get("/quality/drift", response_model=list[DriftReport])
def drift_report(
    device_id: str,
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[DriftReport]:
    devices.get_or_404(session, device_id)
    reports: list[DriftReport] = []
    tracker = tracker_for(device_id)
    for sensor in session.scalars(select(Sensor).where(Sensor.device_id == device_id).order_by(Sensor.kind)):
        field = {"temperature": "temperature_c", "humidity": "humidity_pct", "co2": "co2_ppm"}.get(sensor.kind)
        if field is None:
            continue
        values = [v for v in (tracker.baseline.get(field) or [])]
        total = sensor.valid_count + sensor.invalid_count
        reports.append(DriftReport(
            device_id=device_id, sensor=sensor.kind, health_score=round(sensor.health_score, 4),
            samples=total, invalid_ratio=round(sensor.invalid_count / total, 4) if total else 0.0,
            drift_suspected=tracker.drift_suspected(field),
            mean=round(statistics.fmean(values), 3) if values else None,
            stddev=round(statistics.pstdev(values), 3) if len(values) > 1 else None,
        ))
    return reports


@router.get("/quality/excursions")
def excursions(
    device_id: str,
    field: str = Query(default="humidity_pct", pattern="^(temperature_c|humidity_pct|co2_ppm)$"),
    low: float | None = None,
    high: float | None = None,
    limit: int = Query(default=200, ge=1, le=2000),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> dict[str, Any]:
    from ..safety import load_envelope

    env = load_envelope()
    defaults = {
        "temperature_c": (env.min_temp_c, env.max_temp_c),
        "humidity_pct": (env.min_humidity_pct, env.max_humidity_pct),
        "co2_ppm": (0, env.max_co2_ppm),
    }
    lo = low if low is not None else defaults[field][0]
    hi = high if high is not None else defaults[field][1]
    col = getattr(TelemetryClean, field)
    rows = list(session.scalars(
        select(TelemetryClean).where(
            TelemetryClean.device_id == device_id, col.is_not(None), (col < lo) | (col > hi)
        ).order_by(TelemetryClean.ts.desc()).limit(limit)
    ))
    return {
        "device_id": device_id, "field": field, "safe_range": [lo, hi], "count": len(rows),
        "excursions": [{"ts": r.ts, "value": getattr(r, field), "stage": r.stage, "cycle_id": r.cycle_id} for r in rows],
    }


@router.get("/stage-summary", response_model=list[StageEnvironment])
def stage_summary(
    cycle_id: str,
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[StageEnvironment]:
    return environment_by_stage(session, cycle_id)


@router.get("/pinning-co2")
def pinning_co2(
    cycle_id: str,
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> dict[str, Any]:
    """'What was the CO2 level during the last pinning event?' — Phase 3 exit test."""
    windows = [w for w in stage_windows(session, cycle_id)
               if w[0] in (Stage.PINNING_INITIATED.value, Stage.PINNING_ACTIVE.value)]
    if not windows:
        return {"cycle_id": cycle_id, "found": False, "reason": "no pinning stage recorded yet"}
    stage, start, end = windows[-1]
    stmt = select(TelemetryClean).where(
        TelemetryClean.cycle_id == cycle_id, TelemetryClean.stage == stage,
        TelemetryClean.ts >= start, TelemetryClean.co2_ppm.is_not(None),
    )
    if end is not None:
        stmt = stmt.where(TelemetryClean.ts < end)
    rows = list(session.scalars(stmt.order_by(TelemetryClean.ts)))
    values = [r.co2_ppm for r in rows if r.co2_ppm is not None]
    return {
        "cycle_id": cycle_id, "found": True, "stage": stage,
        "from_ts": start, "to_ts": end, "samples": len(values),
        "avg_co2_ppm": round(statistics.fmean(values), 1) if values else None,
        "min_co2_ppm": min(values) if values else None,
        "max_co2_ppm": max(values) if values else None,
        "validated_only": True,
    }


@router.get("/quality/summary")
def quality_summary(
    device_id: str,
    minutes: int = Query(default=60, ge=1, le=10080),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> dict[str, Any]:
    since = utcnow() - timedelta(minutes=minutes)
    rows = list(session.scalars(
        select(TelemetryClean).where(TelemetryClean.device_id == device_id, TelemetryClean.ts >= since)
    ))
    quarantined = session.scalar(
        select(func.count(TelemetryQuarantine.id)).where(
            TelemetryQuarantine.device_id == device_id, TelemetryQuarantine.quarantined_at >= since)
    ) or 0
    from ..quality import summarize_quality

    return {
        "device_id": device_id, "window_minutes": minutes, "quarantined": int(quarantined),
        **summarize_quality([{"quality_flags": r.quality_flags, "quality_score": r.quality_score} for r in rows]),
    }
