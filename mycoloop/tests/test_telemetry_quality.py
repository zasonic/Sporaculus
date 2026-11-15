"""Phase 3 exit criteria: trustworthy telemetry, or the optimizer does not train."""
from __future__ import annotations

from datetime import timedelta

from conftest import OPERATOR, advance_to

from mycoloop.schemas import QualityFlags, utcnow


def post_batch(client, token, items):
    return client.post("/telemetry", params={"device_id": "chamber-02"},
                       headers={"X-Device-Token": token}, json={"items": items})


def reading(seq, ts=None, **kw):
    base = {"device_id": "chamber-02", "seq": seq, "ts": (ts or utcnow()).isoformat(),
            "temperature_c": 19.5, "humidity_pct": 91.0, "co2_ppm": 900,
            "actuators": {"heater": False, "humidifier": True, "fan": True, "light": True},
            "fan_pct": 45, "light_pct": 60}
    base.update(kw)
    return base


def test_duplicate_seq_is_rejected(client, token):
    r = post_batch(client, token, [reading(1), reading(1)])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["accepted"] == 1
    assert body["duplicates"] == 1


def test_out_of_range_co2_is_quarantined(client, token):
    r = post_batch(client, token, [reading(10, co2_ppm=6000)])
    assert r.status_code == 201
    detail = r.json()["details"][0]
    assert QualityFlags.OUT_OF_RANGE in detail["flags"]
    assert detail["values"]["co2_ppm"] is None
    q = client.get("/telemetry/quarantine", headers=OPERATOR, params={"device_id": "chamber-02"}).json()
    assert len(q) == 1
    assert any("co2_ppm" in reason for reason in q[0]["reasons"])


def test_implausible_jump_is_quarantined(client, token):
    post_batch(client, token, [reading(20, co2_ppm=900)])
    r = post_batch(client, token, [reading(21, co2_ppm=3000)])
    detail = r.json()["details"][0]
    assert QualityFlags.PLAUSIBILITY_JUMP in detail["flags"]
    assert detail["values"]["co2_ppm"] is None


def test_humidity_saturation_is_detected_after_streak(client, token):
    post_batch(client, token, [reading(30, humidity_pct=99.9), reading(31, humidity_pct=99.9)])
    r = post_batch(client, token, [reading(32, humidity_pct=99.9)])
    detail = r.json()["details"][0]
    assert QualityFlags.SENSOR_SATURATED in detail["flags"]
    assert detail["values"]["humidity_pct"] is None
    drift = client.get("/telemetry/quality/drift", headers=OPERATOR, params={"device_id": "chamber-02"}).json()
    humidity = next(d for d in drift if d["sensor"] == "humidity")
    assert humidity["health_score"] < 1.0
    assert humidity["invalid_ratio"] > 0


def test_missing_sensor_is_flagged_and_scored(client, token):
    r = post_batch(client, token, [reading(40, temperature_c=None)])
    detail = r.json()["details"][0]
    assert QualityFlags.SENSOR_MISSING in detail["flags"]
    assert detail["quality_score"] < 1.0


def test_calibration_offset_is_applied(client, token):
    r = client.patch("/devices/chamber-02/sensors/temperature", headers=OPERATOR,
                     json={"offset": -1.5, "scale": 1.0})
    assert r.status_code == 200, r.text
    post_batch(client, token, [reading(50, temperature_c=20.0)])
    rows = client.get("/telemetry/clean", headers=OPERATOR, params={"device_id": "chamber-02"}).json()
    assert rows[0]["temperature_c"] == 18.5
    assert QualityFlags.CALIBRATED in rows[0]["quality_flags"]


def test_timestamp_skew_is_flagged(client, token):
    r = post_batch(client, token, [reading(60, ts=utcnow() - timedelta(hours=3))])
    assert QualityFlags.TS_SKEW in r.json()["details"][0]["flags"]


def test_clean_rows_only_contain_validated_data(client, token):
    post_batch(client, token, [reading(70), reading(71, co2_ppm=6000)])
    rows = client.get("/telemetry/clean", headers=OPERATOR, params={"device_id": "chamber-02"}).json()
    assert len(rows) == 2
    assert all(r["validated"] for r in rows)
    assert rows[0]["co2_ppm"] is None  # the bad field never became a number


def test_stage_summary_and_pinning_co2_answer_phase3_questions(client, device, biology):
    cycle_id = biology["cycle_id"]
    advance_to(client, cycle_id, "PINNING_ACTIVE")
    # telemetry from the live simulated chamber is attributed to cycle + stage
    for _ in range(6):
        device.tick()
    rows = client.get("/telemetry/clean", headers=OPERATOR, params={"cycle_id": cycle_id}).json()
    assert len(rows) >= 6
    assert all(r["stage"] == "PINNING_ACTIVE" for r in rows)

    summary = client.get("/telemetry/stage-summary", headers=OPERATOR, params={"cycle_id": cycle_id}).json()
    pinning = next(s for s in summary if s["stage"] == "PINNING_ACTIVE")
    assert pinning["samples"] >= 6
    assert pinning["avg_temp_c"] is not None

    co2 = client.get("/telemetry/pinning-co2", headers=OPERATOR, params={"cycle_id": cycle_id}).json()
    assert co2["found"] is True
    assert co2["stage"] == "PINNING_ACTIVE"
    assert co2["samples"] >= 6
    assert co2["validated_only"] is True
    assert co2["avg_co2_ppm"] > 400


def test_humidity_excursions_are_queryable(client, token):
    post_batch(client, token, [reading(80, humidity_pct=60.0), reading(81, humidity_pct=91.0)])
    r = client.get("/telemetry/quality/excursions", headers=OPERATOR,
                   params={"device_id": "chamber-02", "field": "humidity_pct", "low": 85, "high": 99}).json()
    assert r["count"] == 1
    assert r["excursions"][0]["value"] == 60.0


def test_quality_summary_reports_flagged_ratio(client, token):
    post_batch(client, token, [reading(90), reading(91, co2_ppm=6000)])
    r = client.get("/telemetry/quality/summary", headers=OPERATOR, params={"device_id": "chamber-02"}).json()
    assert r["samples"] == 2
    assert r["flagged"] == 1
    assert r["quarantined"] == 1
