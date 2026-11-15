"""Phase 3 data quality: raw telemetry is never trusted, clean telemetry is earned.

The optimizer may only read ``telemetry_clean``. A field only reaches that table
if it survives calibration, range checks, plausibility checks and saturation
detection. Everything rejected is kept in ``telemetry_quarantine`` with reasons,
because "we threw it away" must itself be auditable.

Drift detection here is an explicit *heuristic* (sustained deviation from the
device's own baseline) meant to raise a human review flag — it is not a
calibrated drift estimator, and it never silently rewrites data.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .schemas import QualityFlags, Telemetry, as_utc, utcnow

# plausibility windows (physical, not biological — the envelope handles biology)
RANGES: dict[str, tuple[float, float]] = {
    "temperature_c": (-5.0, 60.0),
    "humidity_pct": (0.0, 100.0),
    "co2_ppm": (350.0, 5000.0),
    "substrate_moisture_pct": (0.0, 100.0),
}
MAX_JUMP: dict[str, float] = {"temperature_c": 8.0, "humidity_pct": 25.0, "co2_ppm": 1500.0}
SATURATION_LIMIT = 99.5
SATURATION_STREAK = 3
DRIFT_WINDOW = 20
DRIFT_BASELINE = 60
DRIFT_THRESHOLD = {"temperature_c": 3.0, "humidity_pct": 8.0, "co2_ppm": 600.0}
TS_SKEW_S = 120
CORE_FIELDS = ("temperature_c", "humidity_pct", "co2_ppm")


@dataclass
class Calibration:
    offset: float = 0.0
    scale: float = 1.0


@dataclass
class CleanResult:
    values: dict[str, float | None]
    flags: list[str]
    reasons: list[str]
    quality_score: float
    field_flags: dict[str, list[str]] = field(default_factory=dict)


class DeviceQualityTracker:
    """Per-device rolling state. Lives in the gateway process, rebuilt on boot."""

    def __init__(self) -> None:
        self.last: dict[str, float] = {}
        self.streak: dict[str, int] = {}
        self.window: dict[str, deque[float]] = {}
        self.baseline: dict[str, deque[float]] = {}
        self.high_seq: int = -1

    def _push(self, name: str, value: float) -> None:
        self.window.setdefault(name, deque(maxlen=DRIFT_WINDOW)).append(value)
        self.baseline.setdefault(name, deque(maxlen=DRIFT_BASELINE)).append(value)

    def drift_suspected(self, name: str) -> bool:
        w, b = self.window.get(name), self.baseline.get(name)
        if not w or not b or len(w) < DRIFT_WINDOW or len(b) < DRIFT_BASELINE:
            return False
        recent = sum(w) / len(w)
        base = sum(b) / len(b)
        return abs(recent - base) > DRIFT_THRESHOLD.get(name, 1e9)


def clean_reading(
    t: Telemetry,
    *,
    calibrations: dict[str, Calibration] | None = None,
    tracker: DeviceQualityTracker | None = None,
    now: datetime | None = None,
) -> CleanResult:
    calibrations = calibrations or {}
    tracker = tracker or DeviceQualityTracker()
    now = as_utc(now or utcnow())
    flags: list[str] = list(t.quality_flags)
    reasons: list[str] = []
    values: dict[str, float | None] = {}
    field_flags: dict[str, list[str]] = {}

    ts = as_utc(t.ts)
    if ts and abs((ts - now).total_seconds()) > TS_SKEW_S:
        flags.append(QualityFlags.TS_SKEW)
        reasons.append(f"timestamp skew {round((ts - now).total_seconds())}s")

    if t.seq <= tracker.high_seq:
        flags.append(QualityFlags.DUPLICATE)
        reasons.append(f"seq {t.seq} <= high water mark {tracker.high_seq}")
    tracker.high_seq = max(tracker.high_seq, t.seq)

    for name in RANGES:
        raw = getattr(t, name, None)
        local: list[str] = []
        if raw is None:
            # Only a *core* sensor going missing is a quality event. An optional
            # sensor that was never installed must not poison every reading.
            if name in CORE_FIELDS:
                local.append(QualityFlags.SENSOR_MISSING)
                reasons.append(f"{name} missing")
            values[name] = None
            field_flags[name] = local
            continue

        cal = calibrations.get(name, Calibration())
        value = float(raw) * cal.scale + cal.offset
        if cal.offset or cal.scale != 1.0:
            local.append(QualityFlags.CALIBRATED)

        lo, hi = RANGES[name]
        if not (lo <= value <= hi):
            local.append(QualityFlags.OUT_OF_RANGE)
            reasons.append(f"{name}={round(value, 2)} outside plausible [{lo}, {hi}]")
            values[name] = None
            field_flags[name] = local
            continue

        if name == "humidity_pct" and value >= SATURATION_LIMIT:
            tracker.streak[name] = tracker.streak.get(name, 0) + 1
            if tracker.streak[name] >= SATURATION_STREAK:
                local.append(QualityFlags.SENSOR_SATURATED)
                reasons.append(f"{name} pinned at {value}% for {tracker.streak[name]} samples")
                values[name] = None
                field_flags[name] = local
                continue
        else:
            tracker.streak[name] = 0

        prev = tracker.last.get(name)
        if prev is not None and name in MAX_JUMP and abs(value - prev) > MAX_JUMP[name]:
            local.append(QualityFlags.PLAUSIBILITY_JUMP)
            reasons.append(f"{name} jumped {round(value - prev, 2)} in one sample")
            values[name] = None
            field_flags[name] = local
            # do not poison the baseline with the rejected spike
            continue

        tracker.last[name] = value
        tracker._push(name, value)
        if tracker.drift_suspected(name):
            local.append(QualityFlags.DRIFT_SUSPECTED)
            reasons.append(f"{name} sustained deviation from device baseline")

        values[name] = round(value, 3)
        field_flags[name] = local

    for name, fl in field_flags.items():
        for f in fl:
            if f not in flags:
                flags.append(f)

    good = sum(1 for n in CORE_FIELDS if values.get(n) is not None)
    score = round(good / len(CORE_FIELDS), 3)
    return CleanResult(values=values, flags=flags, reasons=reasons, quality_score=score, field_flags=field_flags)


def sensors_valid(result: CleanResult) -> bool:
    """Control-engine gate: may we actuate on this reading at all?"""
    return all(result.values.get(n) is not None for n in CORE_FIELDS)


def summarize_quality(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows) or 1
    flagged = sum(1 for r in rows if r.get("quality_flags"))
    return {
        "samples": len(rows),
        "flagged": flagged,
        "flagged_ratio": round(flagged / total, 4),
        "avg_quality_score": round(sum(r.get("quality_score", 0.0) for r in rows) / total, 4),
    }
