"""Biology module (Phase 5): the lifecycle is a first-class backend concept.

The stage machine is explicit and strict. Illegal transitions are a 409, not a
silent write, because the optimizer's entire training signal depends on stage
labels being true. Every transition appends to ``stage_events`` (append-only,
DB-enforced), which is what makes a cycle reconstructable months later.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, require_operator
from ..db import get_session
from ..models import (
    Annotation,
    ContaminationEvent,
    Device,
    GrowCycle,
    Harvest,
    RecipeVersion,
    StageEvent,
    Strain,
    SubstrateBatch,
)
from ..schemas import (
    AnnotationCreate,
    ContaminationCreate,
    CycleTimeline,
    GrowCycleCreate,
    GrowCycleRead,
    HarvestCreate,
    HarvestRead,
    Stage,
    StageEventRead,
    StageTransition,
    StrainCreate,
    StrainRead,
    SubstrateBatchCreate,
    SubstrateBatchRead,
    TERMINAL_STAGES,
    as_utc,
    utcnow,
)
from . import audit

router = APIRouter(tags=["biology"])

# --- the state machine (contracts/state-machine.md renders this) -----------
FORWARD: dict[Stage, Stage] = {
    Stage.INOCULATED: Stage.COLONIZING,
    Stage.COLONIZING: Stage.COLONIZATION_COMPLETE,
    Stage.COLONIZATION_COMPLETE: Stage.PINNING_INITIATED,
    Stage.PINNING_INITIATED: Stage.PINNING_ACTIVE,
    Stage.PINNING_ACTIVE: Stage.MATURATION,
    Stage.MATURATION: Stage.HARVEST_READY,
    Stage.HARVEST_READY: Stage.HARVESTED,
    Stage.HARVESTED: Stage.FLUSH_COMPLETE,
    Stage.FLUSH_COMPLETE: Stage.PINNING_INITIATED,  # next flush
}
LATERAL = {Stage.CONTAMINATED, Stage.ABORTED}
HARVESTABLE = {Stage.HARVEST_READY, Stage.MATURATION}


def allowed_transitions(stage: Stage) -> set[Stage]:
    if stage in TERMINAL_STAGES:
        return set()
    out = set(LATERAL)
    nxt = FORWARD.get(stage)
    if nxt:
        out.add(nxt)
    return out


def next_stage(stage: Stage) -> Stage | None:
    return FORWARD.get(stage)


def active_cycle_for_device(session: Session, device_id: str) -> GrowCycle | None:
    return session.scalars(
        select(GrowCycle).where(
            GrowCycle.device_id == device_id,
            GrowCycle.stage.not_in([s.value for s in TERMINAL_STAGES]),
        ).order_by(GrowCycle.started_at.desc())
    ).first()


def stage_windows(session: Session, cycle_id: str) -> list[tuple[str, datetime, datetime | None]]:
    """[(stage, from_ts, to_ts)] — the join key between biology and telemetry."""
    events = list(session.scalars(
        select(StageEvent).where(StageEvent.cycle_id == cycle_id).order_by(StageEvent.ts, StageEvent.event_id)
    ))
    out: list[tuple[str, datetime, datetime | None]] = []
    for i, ev in enumerate(events):
        start = as_utc(ev.ts)
        end = as_utc(events[i + 1].ts) if i + 1 < len(events) else None
        out.append((ev.to_stage, start, end))
    return out


def get_cycle(session: Session, cycle_id: str) -> GrowCycle:
    cycle = session.get(GrowCycle, cycle_id)
    if cycle is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown cycle {cycle_id}")
    return cycle


def transition(session: Session, cycle: GrowCycle, to_stage: Stage, actor: str, reason: str) -> StageEvent:
    current = Stage(cycle.stage)
    if to_stage not in allowed_transitions(current):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"illegal transition {current.value} -> {to_stage.value}; "
            f"allowed: {sorted(s.value for s in allowed_transitions(current)) or 'none (terminal)'}",
        )
    if current == Stage.FLUSH_COMPLETE and to_stage == Stage.PINNING_INITIATED:
        cycle.flush += 1
    event = StageEvent(cycle_id=cycle.cycle_id, from_stage=current.value, to_stage=to_stage.value,
                       actor=actor, reason=reason)
    session.add(event)
    cycle.stage = to_stage.value
    if to_stage in TERMINAL_STAGES:
        cycle.ended_at = utcnow()
    audit.record(session, actor, "cycle.stage", cycle.cycle_id,
                 from_stage=current.value, to_stage=to_stage.value, reason=reason)
    session.flush()
    return event


# ---------------------------------------------------------------------------
# reference data
# ---------------------------------------------------------------------------
@router.post("/strains", response_model=StrainRead, status_code=status.HTTP_201_CREATED)
def create_strain(payload: StrainCreate, session: Session = Depends(get_session),
                  principal: Principal = Depends(require_operator)) -> Strain:
    if session.get(Strain, payload.strain_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "strain exists")
    strain = Strain(**payload.model_dump())
    session.add(strain)
    audit.record(session, principal.actor, "strain.create", payload.strain_id, name=payload.name)
    session.flush()
    return strain


@router.get("/strains", response_model=list[StrainRead])
def list_strains(session: Session = Depends(get_session), _: Principal = Depends(require_operator)) -> list[Strain]:
    return list(session.scalars(select(Strain).order_by(Strain.strain_id)))


@router.post("/substrate-batches", response_model=SubstrateBatchRead, status_code=status.HTTP_201_CREATED)
def create_batch(payload: SubstrateBatchCreate, session: Session = Depends(get_session),
                 principal: Principal = Depends(require_operator)) -> SubstrateBatch:
    if session.get(SubstrateBatch, payload.batch_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "batch exists")
    batch = SubstrateBatch(**payload.model_dump())
    session.add(batch)
    audit.record(session, principal.actor, "substrate.create", payload.batch_id, mass_kg=payload.mass_kg)
    session.flush()
    return batch


@router.get("/substrate-batches", response_model=list[SubstrateBatchRead])
def list_batches(session: Session = Depends(get_session), _: Principal = Depends(require_operator)) -> list[SubstrateBatch]:
    return list(session.scalars(select(SubstrateBatch).order_by(SubstrateBatch.prepared_at.desc())))


# ---------------------------------------------------------------------------
# cycles
# ---------------------------------------------------------------------------
@router.post("/grow-cycles", response_model=GrowCycleRead, status_code=status.HTTP_201_CREATED)
def create_cycle(payload: GrowCycleCreate, session: Session = Depends(get_session),
                 principal: Principal = Depends(require_operator)) -> GrowCycle:
    for model, key, label in (
        (Strain, payload.strain_id, "strain"),
        (SubstrateBatch, payload.substrate_batch_id, "substrate batch"),
        (Device, payload.device_id, "device"),
        (RecipeVersion, payload.recipe_version_id, "recipe version"),
    ):
        if session.get(model, key) is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"unknown {label} {key}")
    cycle_id = payload.cycle_id or f"GC-{utcnow().year}-{int(utcnow().timestamp()) % 100000:05d}"
    if session.get(GrowCycle, cycle_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "cycle exists")
    cycle = GrowCycle(
        cycle_id=cycle_id, strain_id=payload.strain_id, substrate_batch_id=payload.substrate_batch_id,
        device_id=payload.device_id, recipe_version_id=payload.recipe_version_id,
        stage=Stage.INOCULATED.value, notes=payload.notes,
    )
    session.add(cycle)
    session.flush()
    session.add(StageEvent(cycle_id=cycle_id, from_stage=None, to_stage=Stage.INOCULATED.value,
                           actor=principal.actor, reason="cycle created"))
    audit.record(session, principal.actor, "cycle.create", cycle_id,
                 strain=payload.strain_id, recipe_version=payload.recipe_version_id)
    session.flush()
    return cycle


@router.get("/grow-cycles", response_model=list[GrowCycleRead])
def list_cycles(device_id: str | None = None, session: Session = Depends(get_session),
                _: Principal = Depends(require_operator)) -> list[GrowCycle]:
    stmt = select(GrowCycle).order_by(GrowCycle.started_at.desc())
    if device_id:
        stmt = stmt.where(GrowCycle.device_id == device_id)
    return list(session.scalars(stmt))


@router.get("/grow-cycles/{cycle_id}", response_model=GrowCycleRead)
def read_cycle(cycle_id: str, session: Session = Depends(get_session),
               _: Principal = Depends(require_operator)) -> GrowCycle:
    return get_cycle(session, cycle_id)


@router.get("/grow-cycles/{cycle_id}/transitions")
def read_transitions(cycle_id: str, session: Session = Depends(get_session),
                     _: Principal = Depends(require_operator)) -> dict[str, Any]:
    cycle = get_cycle(session, cycle_id)
    current = Stage(cycle.stage)
    return {
        "cycle_id": cycle_id, "current_stage": current.value,
        "next_stage": next_stage(current).value if next_stage(current) else None,
        "allowed": sorted(s.value for s in allowed_transitions(current)),
        "terminal": current in TERMINAL_STAGES,
    }


@router.post("/grow-cycles/{cycle_id}/transitions", response_model=StageEventRead)
def advance(cycle_id: str, payload: StageTransition, session: Session = Depends(get_session),
            principal: Principal = Depends(require_operator)) -> StageEvent:
    cycle = get_cycle(session, cycle_id)
    return transition(session, cycle, payload.to_stage, principal.actor, payload.reason)


@router.get("/grow-cycles/{cycle_id}/events", response_model=list[StageEventRead])
def cycle_events(cycle_id: str, session: Session = Depends(get_session),
                 _: Principal = Depends(require_operator)) -> list[StageEvent]:
    get_cycle(session, cycle_id)
    return list(session.scalars(
        select(StageEvent).where(StageEvent.cycle_id == cycle_id).order_by(StageEvent.ts, StageEvent.event_id)
    ))


@router.post("/grow-cycles/{cycle_id}/harvests", response_model=HarvestRead, status_code=status.HTTP_201_CREATED)
def log_harvest(cycle_id: str, payload: HarvestCreate, session: Session = Depends(get_session),
                principal: Principal = Depends(require_operator)) -> Harvest:
    cycle = get_cycle(session, cycle_id)
    if Stage(cycle.stage) not in HARVESTABLE:
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"cannot harvest from {cycle.stage}; advance to HARVEST_READY first")
    harvest = Harvest(cycle_id=cycle_id, grams=payload.grams, flush=payload.flush,
                      quality=payload.quality, notes=payload.notes, actor=principal.actor)
    session.add(harvest)
    cycle.yield_g = round(cycle.yield_g + payload.grams, 3)
    if Stage(cycle.stage) != Stage.HARVESTED:
        transition(session, cycle, Stage.HARVESTED, principal.actor, f"harvest {payload.grams} g")
    audit.record(session, principal.actor, "cycle.harvest", cycle_id, grams=payload.grams, flush=payload.flush)
    session.flush()
    return harvest


@router.get("/grow-cycles/{cycle_id}/harvests", response_model=list[HarvestRead])
def list_harvests(cycle_id: str, session: Session = Depends(get_session),
                  _: Principal = Depends(require_operator)) -> list[Harvest]:
    get_cycle(session, cycle_id)
    return list(session.scalars(select(Harvest).where(Harvest.cycle_id == cycle_id).order_by(Harvest.ts)))


@router.post("/grow-cycles/{cycle_id}/contamination", response_model=StageEventRead)
def log_contamination(cycle_id: str, payload: ContaminationCreate, session: Session = Depends(get_session),
                      principal: Principal = Depends(require_operator)) -> StageEvent:
    cycle = get_cycle(session, cycle_id)
    session.add(ContaminationEvent(cycle_id=cycle_id, kind=payload.kind, severity=payload.severity.value,
                                   notes=payload.notes, actor=principal.actor))
    cycle.contaminated = True
    event = transition(session, cycle, Stage.CONTAMINATED, principal.actor,
                       f"{payload.kind} / {payload.severity.value}")
    audit.record(session, principal.actor, "cycle.contamination", cycle_id,
                 kind=payload.kind, severity=payload.severity.value)
    return event


@router.post("/grow-cycles/{cycle_id}/annotations", response_model=dict[str, Any],
             status_code=status.HTTP_201_CREATED)
def annotate(cycle_id: str, payload: AnnotationCreate, session: Session = Depends(get_session),
             principal: Principal = Depends(require_operator)) -> dict[str, Any]:
    get_cycle(session, cycle_id)
    note = Annotation(cycle_id=cycle_id, body=payload.body, actor=principal.actor)
    session.add(note)
    audit.record(session, principal.actor, "cycle.annotate", cycle_id, body=payload.body[:200])
    session.flush()
    return {"annotation_id": note.annotation_id, "ts": note.ts, "body": note.body, "actor": note.actor}


@router.get("/grow-cycles/{cycle_id}/timeline", response_model=CycleTimeline)
def timeline(cycle_id: str, session: Session = Depends(get_session),
             _: Principal = Depends(require_operator)) -> CycleTimeline:
    """Reconstruct the whole cycle: biology + environment at every moment."""
    from .ingestion import environment_by_stage  # lazy: avoids a module cycle

    cycle = get_cycle(session, cycle_id)
    events: list[dict[str, Any]] = [
        {"kind": "stage", "ts": e.ts, "actor": e.actor,
         "detail": f"{e.from_stage or 'START'} -> {e.to_stage}", "reason": e.reason}
        for e in session.scalars(select(StageEvent).where(StageEvent.cycle_id == cycle_id))
    ]
    events += [
        {"kind": "harvest", "ts": h.ts, "actor": h.actor, "detail": f"{h.grams} g (flush {h.flush})",
         "reason": h.notes or ""}
        for h in session.scalars(select(Harvest).where(Harvest.cycle_id == cycle_id))
    ]
    events += [
        {"kind": "contamination", "ts": c.ts, "actor": c.actor, "detail": f"{c.kind} / {c.severity}",
         "reason": c.notes or ""}
        for c in session.scalars(select(ContaminationEvent).where(ContaminationEvent.cycle_id == cycle_id))
    ]
    events += [
        {"kind": "annotation", "ts": a.ts, "actor": a.actor, "detail": a.body, "reason": ""}
        for a in session.scalars(select(Annotation).where(Annotation.cycle_id == cycle_id))
    ]
    events.sort(key=lambda e: as_utc(e["ts"]))
    harvests = list(session.scalars(select(Harvest).where(Harvest.cycle_id == cycle_id).order_by(Harvest.ts)))
    return CycleTimeline(
        cycle=GrowCycleRead.model_validate(cycle),
        events=events,
        environment_by_stage=environment_by_stage(session, cycle_id),
        harvests=[HarvestRead.model_validate(h) for h in harvests],
    )
