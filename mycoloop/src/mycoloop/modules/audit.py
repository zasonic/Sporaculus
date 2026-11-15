"""Audit module — append-only record of everything that matters.

Phase 8 exit criteria ("no unlogged optimizer recommendation, no unlogged manual
override") is only achievable if logging is a *side effect of the write path*,
not something callers remember to do. Every mutating service calls ``record``
inside the same transaction as the change it describes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, require_operator
from ..db import get_session
from ..models import AuditLog
from ..schemas import AuditRead

router = APIRouter(prefix="/audit", tags=["audit"])


def record(
    session: Session,
    actor: str,
    action: str,
    subject: str | None = None,
    **detail: Any,
) -> AuditLog:
    entry = AuditLog(actor=actor, action=action, subject=subject, detail=detail)
    session.add(entry)
    return entry


@router.get("", response_model=list[AuditRead])
def list_audit(
    action: str | None = None,
    subject: str | None = None,
    actor: str | None = None,
    limit: int = Query(default=100, ge=1, le=1000),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[AuditLog]:
    stmt = select(AuditLog).order_by(AuditLog.ts.desc(), AuditLog.audit_id.desc()).limit(limit)
    if action:
        stmt = stmt.where(AuditLog.action == action)
    if subject:
        stmt = stmt.where(AuditLog.subject == subject)
    if actor:
        stmt = stmt.where(AuditLog.actor == actor)
    return list(session.scalars(stmt))
