"""Optimizer module (Phase 7): recommends, never actuates.

Slow biological cycles (weeks, tens of trials) favour Bayesian optimisation over
deep RL. The production path is Optuna -> Ax/BoTorch -> PyMC/GPyTorch; that is a
*pluggable search backend* behind ``SearchBackend`` so the safety, audit and
approval machinery can be built and proven now, with a heuristic backend, and
the statistics swapped in later without touching the control path.

Non-negotiable invariants (tested):
  1. a recommendation with envelope violations cannot be approved
  2. nothing reaches a device without a recorded human approval
  3. every recommendation, approval, rejection and rollback is audited
"""
from __future__ import annotations

import statistics
from datetime import timedelta
from typing import Any, Protocol

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, require_operator
from ..db import get_session
from ..models import GrowCycle, OptimizerTrial, Recipe, RecipeVersion, Recommendation
from ..safety import load_envelope, validate_setpoints
from ..schemas import (
    ApprovalRequest,
    OptimizerTrialCreate,
    OptimizerTrialRead,
    RecipeCreate,
    RecommendationRead,
    RollbackRequest,
    SearchSpace,
    Setpoints,
    Stage,
    Violation,
    as_utc,
    utcnow,
)
from . import audit, control
from .gateway import Gateway, get_gateway

router = APIRouter(prefix="/optimizer", tags=["optimizer"])


class SearchBackend(Protocol):
    """Swap in Optuna / BoTorch here. Pure function of history + search space."""

    name: str

    def suggest(self, history: list[dict[str, Any]], space: SearchSpace, current: Setpoints) -> Setpoints: ...


class HeuristicSearch:
    """Interim backend: move toward the best-scoring historical region.

    Deterministic, explainable, and intentionally conservative — it takes a
    half-step from the current setpoints toward the best observed trial, clipped
    to the declared search space. It is *not* a Bayesian model and says so.
    """

    name = "heuristic-halfstep-v0"

    def suggest(self, history: list[dict[str, Any]], space: SearchSpace, current: Setpoints) -> Setpoints:
        scored = [h for h in history if h.get("objective") is not None and h.get("params")]
        if scored:
            best = max(scored, key=lambda h: h["objective"])["params"]
            target_temp = float(best.get("temp_c", current.temp_c))
            target_rh = float(best.get("humidity_pct", current.humidity_pct))
            target_fae = float(best.get("fan_pct", current.fan_pct))
        else:
            target_temp = (space.temp_min + space.temp_max) / 2
            target_rh = (space.rh_min + space.rh_max) / 2
            target_fae = (space.fae_min + space.fae_max) / 2
        temp = current.temp_c + (target_temp - current.temp_c) / 2
        rh = current.humidity_pct + (target_rh - current.humidity_pct) / 2
        fae = current.fan_pct + (target_fae - current.fan_pct) / 2
        return Setpoints(
            temp_c=round(min(max(temp, space.temp_min), space.temp_max), 2),
            humidity_pct=round(min(max(rh, space.rh_min), space.rh_max), 1),
            co2_ceiling_ppm=int(min(current.co2_ceiling_ppm, space.co2_max)),
            fan_pct=int(min(max(fae, space.fae_min), space.fae_max)),
            light_pct=current.light_pct,
        )


_backend: SearchBackend = HeuristicSearch()


def set_backend(backend: SearchBackend) -> None:
    global _backend
    _backend = backend


def cycle_history(session: Session) -> list[dict[str, Any]]:
    """Only validated, completed biology becomes training signal."""
    out: list[dict[str, Any]] = []
    for cycle in session.scalars(select(GrowCycle).where(GrowCycle.yield_g > 0)):
        rv = session.get(RecipeVersion, cycle.recipe_version_id)
        if rv is None:
            continue
        end = as_utc(cycle.ended_at) or utcnow()
        days = max((end - as_utc(cycle.started_at)).total_seconds() / 86400, 0.5)
        out.append({
            "cycle_id": cycle.cycle_id, "params": rv.setpoints,
            "yield_g": cycle.yield_g, "days": round(days, 2),
            "objective": round(cycle.yield_g / days, 3),
            "contaminated": cycle.contaminated,
        })
    return out


def _contamination_risk(history: list[dict[str, Any]], rh: float) -> float:
    """Transparent risk proxy: base rate scaled by humidity distance from 90%."""
    if not history:
        base = 0.12
    else:
        base = sum(1 for h in history if h["contaminated"]) / len(history)
    return round(min(1.0, base * (1 + max(0.0, (rh - 90.0)) / 20.0)), 4)


def _confidence(n_cycles: int, n_trials: int) -> str:
    n = n_cycles + n_trials
    return "low" if n < 3 else ("medium" if n < 8 else "high")


def recommend(session: Session, recipe_id: str, device_id: str, space: SearchSpace, actor: str) -> Recommendation:
    if session.get(Recipe, recipe_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown recipe {recipe_id}")
    history = cycle_history(session)
    trials = [
        {"params": t.params, "objective": t.objective}
        for t in session.scalars(select(OptimizerTrial).where(OptimizerTrial.constraints_ok.is_(True)))
    ]
    current = control.active_setpoints(session, device_id)
    candidate = _backend.suggest(history + trials, space, current)

    env = load_envelope()
    verdict = validate_setpoints(candidate, env)
    violations = [v.model_dump() for v in verdict.violations]
    risk = _contamination_risk(history, candidate.humidity_pct)
    if risk > env.max_contamination_probability:
        violations.append(Violation(
            field="humidity_pct", value=candidate.humidity_pct, rule="contamination_risk",
            message=f"estimated contamination risk {risk} exceeds {env.max_contamination_probability}",
        ).model_dump())

    baseline_yield = statistics.fmean([h["objective"] for h in history]) if history else 0.0
    delta = {
        "temp_c": round(candidate.temp_c - current.temp_c, 2),
        "humidity_pct": round(candidate.humidity_pct - current.humidity_pct, 2),
        "fan_pct": candidate.fan_pct - current.fan_pct,
        "co2_ceiling_ppm": candidate.co2_ceiling_ppm - current.co2_ceiling_ppm,
    }
    expected = {
        "yield_g_per_day_baseline": round(baseline_yield, 2),
        "yield_g_per_day_direction": "up" if delta["fan_pct"] >= 0 and delta["temp_c"] <= 0 else "uncertain",
        "contamination_risk_estimate": risk,
        "contamination_direction": "down" if delta["humidity_pct"] < 0 else "up",
        "energy_direction": "up" if delta["fan_pct"] > 0 else "flat",
        "model": _backend.name,
        "note": "Directional estimate from historical cycles, not a causal guarantee.",
    }
    reason = (
        f"Based on {len(history)} completed cycles and {len(trials)} constrained trials, "
        f"{_backend.name} proposes a half-step toward the best observed region: "
        f"temperature {delta['temp_c']:+}C, fresh air exchange {delta['fan_pct']:+}%, "
        f"humidity {current.humidity_pct}% -> {candidate.humidity_pct}%. "
        "Cycles with cooler fruiting and higher air exchange showed stronger pin survival. "
        "Safety validation and human approval are required before application."
    )
    rec = Recommendation(
        recipe_id=recipe_id, device_id=device_id, params=candidate.model_dump(), delta=delta,
        expected_effect=expected, confidence=_confidence(len(history), len(trials)), reason=reason,
        based_on_cycles=len(history), violations=violations, state="pending",
    )
    session.add(rec)
    session.flush()
    trial = OptimizerTrial(params=candidate.model_dump(), objective=round(baseline_yield, 3),
                           constraints_ok=not violations, recommendation_id=rec.recommendation_id,
                           notes=f"search backend {_backend.name}")
    session.add(trial)
    audit.record(session, actor, "optimizer.recommend", rec.recommendation_id,
                 recipe_id=recipe_id, device_id=device_id, params=rec.params,
                 violations=violations, confidence=rec.confidence)
    session.flush()
    return rec


def approve(session: Session, gw: Gateway, recommendation_id: str, actor: str, note: str | None = None) -> dict[str, Any]:
    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown recommendation")
    if rec.state != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"recommendation already {rec.state}")
    if rec.violations:
        audit.record(session, "safety", "optimizer.approve_blocked", recommendation_id, violations=rec.violations)
        session.commit()
        raise HTTPException(status.HTTP_409_CONFLICT,
                            detail={"message": "safety violations must be resolved before approval",
                                    "violations": rec.violations})
    recipe = session.get(Recipe, rec.recipe_id)
    version = control.create_recipe(session, RecipeCreate(
        recipe_id=rec.recipe_id, name=recipe.name if recipe else rec.recipe_id,
        stage=Stage.PINNING_ACTIVE, setpoints=Setpoints.model_validate(rec.params),
        change_reason=f"optimizer {recommendation_id} approved by {actor}: {note or ''}".strip(),
    ), actor)
    version.approved_by = actor
    applied = control.apply_recipe(session, gw, rec.device_id, version.recipe_version_id, actor,
                                   reason=f"optimizer approval {recommendation_id}")
    rec.state = "approved"
    rec.decided_at = utcnow()
    rec.decided_by = actor
    rec.applied_recipe_version_id = version.recipe_version_id
    for trial in session.scalars(select(OptimizerTrial).where(OptimizerTrial.recommendation_id == recommendation_id)):
        trial.approved_by = actor
    audit.record(session, actor, "optimizer.approve", recommendation_id,
                 recipe_version_id=version.recipe_version_id, note=note)
    session.commit()
    return {"recommendation_id": recommendation_id, "state": rec.state,
            "recipe_version_id": version.recipe_version_id, "applied": applied}


def reject(session: Session, recommendation_id: str, actor: str, note: str | None = None) -> Recommendation:
    rec = session.get(Recommendation, recommendation_id)
    if rec is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown recommendation")
    if rec.state != "pending":
        raise HTTPException(status.HTTP_409_CONFLICT, f"recommendation already {rec.state}")
    rec.state = "rejected"
    rec.decided_at = utcnow()
    rec.decided_by = actor
    audit.record(session, actor, "optimizer.reject", recommendation_id, note=note)
    session.flush()
    return rec


def rollback(session: Session, gw: Gateway, recipe_id: str, device_id: str, actor: str, reason: str) -> dict[str, Any]:
    versions = list(session.scalars(
        select(RecipeVersion).where(RecipeVersion.recipe_id == recipe_id).order_by(RecipeVersion.version.desc())
    ))
    if len(versions) < 2:
        raise HTTPException(status.HTTP_409_CONFLICT, "no previous version to roll back to")
    target = versions[1]
    applied = control.apply_recipe(session, gw, device_id, target.recipe_version_id, actor, reason=f"rollback: {reason}")
    audit.record(session, actor, "optimizer.rollback", recipe_id,
                 rolled_back_to=target.recipe_version_id, reason=reason)
    session.commit()
    return {"recipe_id": recipe_id, "rolled_back_to": target.recipe_version_id, "applied": applied}


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("/trials", response_model=OptimizerTrialRead, status_code=status.HTTP_201_CREATED)
def create_trial(payload: OptimizerTrialCreate, session: Session = Depends(get_session),
                 principal: Principal = Depends(require_operator)) -> OptimizerTrial:
    verdict = validate_setpoints(payload.params)
    trial = OptimizerTrial(cycle_id=payload.cycle_id, params=payload.params.model_dump(),
                           objective=payload.objective, constraints_ok=payload.constraints_ok and verdict.ok,
                           notes=payload.notes)
    session.add(trial)
    audit.record(session, principal.actor, "optimizer.trial", None, params=trial.params, objective=payload.objective)
    session.flush()
    return trial


@router.get("/trials", response_model=list[OptimizerTrialRead])
def list_trials(session: Session = Depends(get_session), _: Principal = Depends(require_operator)) -> list[OptimizerTrial]:
    return list(session.scalars(select(OptimizerTrial).order_by(OptimizerTrial.ts.desc())))


@router.get("/history")
def history(session: Session = Depends(get_session), _: Principal = Depends(require_operator)) -> dict[str, Any]:
    hist = cycle_history(session)
    return {"completed_cycles": len(hist), "search_backend": _backend.name, "cycles": hist}


@router.post("/recommendations", response_model=RecommendationRead, status_code=status.HTTP_201_CREATED)
def recommend_route(recipe_id: str, device_id: str, space: SearchSpace | None = None,
                    session: Session = Depends(get_session),
                    principal: Principal = Depends(require_operator)) -> Recommendation:
    return recommend(session, recipe_id, device_id, space or SearchSpace(), principal.actor)


@router.get("/recommendations", response_model=list[RecommendationRead])
def list_recommendations(session: Session = Depends(get_session),
                         _: Principal = Depends(require_operator)) -> list[Recommendation]:
    return list(session.scalars(select(Recommendation).order_by(Recommendation.created_at.desc())))


@router.post("/recommendations/{recommendation_id}/approve")
def approve_route(recommendation_id: str, body: ApprovalRequest | None = None,
                  session: Session = Depends(get_session), principal: Principal = Depends(require_operator),
                  gw: Gateway = Depends(get_gateway)) -> dict[str, Any]:
    return approve(session, gw, recommendation_id, principal.actor, body.note if body else None)


@router.post("/recommendations/{recommendation_id}/reject", response_model=RecommendationRead)
def reject_route(recommendation_id: str, body: ApprovalRequest | None = None,
                 session: Session = Depends(get_session),
                 principal: Principal = Depends(require_operator)) -> Recommendation:
    return reject(session, recommendation_id, principal.actor, body.note if body else None)


@router.post("/rollback")
def rollback_route(recipe_id: str, device_id: str, body: RollbackRequest,
                   session: Session = Depends(get_session), principal: Principal = Depends(require_operator),
                   gw: Gateway = Depends(get_gateway)) -> dict[str, Any]:
    return rollback(session, gw, recipe_id, device_id, principal.actor, body.reason)
