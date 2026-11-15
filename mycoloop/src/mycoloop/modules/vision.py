"""Vision module (Phase 6): images become optimizer-ready biological features.

Bootstrap strategy (no manual labelling from scratch): M18K RGB-D + the Nature
mycelium segmentation dataset -> Grounded-SAM / SAM 2 weak labels -> compact
YOLO/RT-DETR edge model on ONNX Runtime / TensorRT -> chamber-specific human
corrections feed the active-learning queue.

This module owns ingestion, the model registry, corrections and the feature
queries. It does not run inference itself: the edge does, and posts results.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, require_operator
from ..db import get_session
from ..models import HumanLabel, VisionFrame, VisionInference, VisionModel
from ..schemas import (
    HumanLabelCreate,
    ModelRegister,
    Stage,
    VisionFrameCreate,
    VisionInferenceRead,
    utcnow,
)
from . import audit, biology

router = APIRouter(prefix="/vision", tags=["vision"])

ACTIVE_LEARNING_CONFIDENCE = 0.75


@router.post("/models", response_model=dict[str, Any], status_code=status.HTTP_201_CREATED)
def register_model(payload: ModelRegister, session: Session = Depends(get_session),
                   principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    model = session.get(VisionModel, payload.model_version)
    if model is None:
        model = VisionModel(**payload.model_dump())
        session.add(model)
    else:
        model.metrics = payload.metrics
        model.dataset = payload.dataset
    audit.record(session, principal.actor, "vision.model_register", payload.model_version,
                 family=payload.family, dataset=payload.dataset)
    session.flush()
    return {"model_version": model.model_version, "family": model.family, "registered_at": model.registered_at}


@router.get("/models")
def list_models(session: Session = Depends(get_session), _: Principal = Depends(require_operator)) -> list[dict[str, Any]]:
    return [
        {"model_version": m.model_version, "family": m.family, "dataset": m.dataset,
         "metrics": m.metrics, "edge_runtime": m.edge_runtime, "registered_at": m.registered_at}
        for m in session.scalars(select(VisionModel).order_by(VisionModel.registered_at.desc()))
    ]


@router.post("/frames", response_model=VisionInferenceRead, status_code=status.HTTP_201_CREATED)
def ingest_frame(payload: VisionFrameCreate, session: Session = Depends(get_session),
                 principal: Principal = Depends(require_operator)) -> VisionInference:
    cycle = biology.get_cycle(session, payload.cycle_id)
    if session.get(VisionModel, payload.model_version) is None:
        session.add(VisionModel(model_version=payload.model_version))
    frame = VisionFrame(
        frame_id=payload.frame_id or None, cycle_id=cycle.cycle_id, device_id=payload.device_id,
        stage=cycle.stage, storage_uri=payload.storage_uri, ts=payload.ts or utcnow(),
    )
    session.add(frame)
    session.flush()
    inference = VisionInference(
        frame_id=frame.frame_id, cycle_id=cycle.cycle_id, stage=cycle.stage,
        model_version=payload.model_version, features=payload.features.model_dump(),
        confidence=payload.confidence, ts=frame.ts,
    )
    session.add(inference)
    audit.record(session, principal.actor, "vision.inference", frame.frame_id,
                 cycle_id=cycle.cycle_id, model=payload.model_version, confidence=payload.confidence)
    if payload.features.contamination_probability > 0.7:
        audit.record(session, "vision", "vision.contamination_alert", cycle.cycle_id,
                     frame_id=frame.frame_id, probability=payload.features.contamination_probability,
                     kind=payload.features.contamination_type)
    session.flush()
    return inference


def _with_jump(session: Session, rows: list[VisionInference]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        prev = session.scalars(
            select(VisionInference).where(
                VisionInference.cycle_id == row.cycle_id, VisionInference.ts < row.ts
            ).order_by(VisionInference.ts.desc()).limit(1)
        ).first()
        prev_count = (prev.features or {}).get("pin_count") if prev else None
        jump = None
        if prev_count:
            jump = round((row.features.get("pin_count", 0) - prev_count) / prev_count * 100, 2)
        out.append({
            "inference_id": row.inference_id, "frame_id": row.frame_id, "cycle_id": row.cycle_id,
            "stage": row.stage, "ts": row.ts, "model_version": row.model_version,
            "features": row.features, "confidence": row.confidence, "human_corrected": row.human_corrected,
            "pin_count_jump_pct": jump,
        })
    return out


@router.get("/frames")
def query_frames(
    cycle_id: str | None = None,
    stage: Stage | None = None,
    min_contamination: float | None = Query(default=None, ge=0, le=1),
    corrected: bool | None = None,
    pin_jump_pct: float | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[dict[str, Any]]:
    stmt = select(VisionInference).order_by(VisionInference.ts.desc()).limit(limit)
    if cycle_id:
        stmt = stmt.where(VisionInference.cycle_id == cycle_id)
    if stage:
        stmt = stmt.where(VisionInference.stage == stage.value)
    if corrected is not None:
        stmt = stmt.where(VisionInference.human_corrected.is_(corrected))
    rows = list(session.scalars(stmt))
    enriched = _with_jump(session, rows)
    if min_contamination is not None:
        enriched = [r for r in enriched if r["features"].get("contamination_probability", 0) > min_contamination]
    if pin_jump_pct is not None:
        enriched = [r for r in enriched if r["pin_count_jump_pct"] is not None and r["pin_count_jump_pct"] > pin_jump_pct]
    return enriched


@router.post("/inferences/{inference_id}/labels", response_model=VisionInferenceRead)
def correct(inference_id: str, payload: HumanLabelCreate, session: Session = Depends(get_session),
            principal: Principal = Depends(require_operator)) -> VisionInference:
    row = session.get(VisionInference, inference_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown inference")
    before = dict(row.features or {})
    session.add(HumanLabel(inference_id=inference_id, corrected_features=payload.features.model_dump(),
                           note=payload.note, actor=principal.actor))
    row.features = payload.features.model_dump()
    row.human_corrected = True
    row.confidence = 1.0
    changed = {k: {"before": before.get(k), "after": v} for k, v in row.features.items() if before.get(k) != v}
    audit.record(session, principal.actor, "vision.correction", inference_id, changed=changed, note=payload.note)
    session.flush()
    return row


@router.get("/active-learning")
def active_learning(session: Session = Depends(get_session),
                    _: Principal = Depends(require_operator)) -> dict[str, Any]:
    rows = list(session.scalars(
        select(VisionInference).where(
            VisionInference.human_corrected.is_(False),
            VisionInference.confidence < ACTIVE_LEARNING_CONFIDENCE,
        ).order_by(VisionInference.confidence).limit(200)
    ))
    return {
        "threshold": ACTIVE_LEARNING_CONFIDENCE, "queued": len(rows),
        "items": [{"inference_id": r.inference_id, "cycle_id": r.cycle_id, "confidence": r.confidence,
                   "contamination_probability": r.features.get("contamination_probability")} for r in rows],
    }
