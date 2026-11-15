"""Gateway module (Phase 2): reliable, observable, idempotent device commands.

Guarantees proven by tests/test_command_bus.py:
  * a command is persisted before it is published (no phantom acks)
  * ``idempotency_key`` replays return the *same* command, never a second actuation
  * reusing a key with a different payload is a 409, not a silent overwrite
  * acks are journalled append-only: sent -> received -> applied | failed
  * retries republish the *same* command_id so the device can dedupe
  * TTL expiry and heartbeat silence are both explicit, audited states

A safety veto is a *successful evaluation* with ``safety.ok == false``: the
attempt is still persisted and audited. Malformed contracts are the 422s.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import Principal, require_operator
from ..bus import Message, MessageBus
from ..config import Settings, get_settings
from ..db import get_session, session_scope
from ..models import Command, CommandAckRow, Device
from ..safety import load_envelope, validate_setpoints
from ..schemas import (
    CLOSED_COMMAND_STATES,
    AckState,
    CommandAck,
    CommandCreate,
    CommandKind,
    CommandRead,
    CommandState,
    DeviceHealth,
    DeviceStatus,
    DeviceStatusMessage,
    DispatchResult,
    SafetyVerdict,
    Telemetry,
    as_utc,
    utcnow,
)
from .. import topics
from . import audit, devices

log = logging.getLogger(__name__)
router = APIRouter(tags=["gateway"])


class CommandDispatcher:
    def __init__(self, bus: MessageBus, settings: Settings) -> None:
        self.bus = bus
        self.settings = settings

    # -- outbound ----------------------------------------------------------
    def dispatch(self, session: Session, device_id: str, payload: CommandCreate, actor: str) -> DispatchResult:
        devices.get_or_404(session, device_id)

        existing = session.scalars(
            select(Command).where(Command.device_id == device_id, Command.idempotency_key == payload.idempotency_key)
        ).first()
        if existing is not None:
            if _fingerprint(existing.payload) != _fingerprint(payload.payload) or existing.kind != payload.kind.value:
                raise HTTPException(status.HTTP_409_CONFLICT,
                                    "idempotency_key already used with a different payload")
            read = CommandRead.model_validate(existing)
            read.idempotent_replay = True
            return DispatchResult(command=read, safety=SafetyVerdict(ok=True), idempotent_replay=True)

        verdict = self._validate_safety(payload)
        now = utcnow()
        cmd = Command(
            device_id=device_id, kind=payload.kind.value, payload=payload.payload,
            idempotency_key=payload.idempotency_key, issued_by=actor, issued_at=now,
            expires_at=now + timedelta(seconds=payload.ttl_s or self.settings.command_ttl_s),
            state=CommandState.QUEUED.value,
        )
        session.add(cmd)
        session.flush()

        if not verdict.ok:
            cmd.state = CommandState.REJECTED_SAFETY.value
            cmd.closed_at = now
            cmd.last_error = "; ".join(v.message for v in verdict.violations)
            audit.record(session, "safety", "command.vetoed", cmd.command_id,
                         device_id=device_id, kind=cmd.kind, violations=[v.model_dump() for v in verdict.violations])
            session.commit()
            return DispatchResult(command=CommandRead.model_validate(cmd), safety=verdict)

        audit.record(session, actor, "command.dispatch", cmd.command_id,
                     device_id=device_id, kind=cmd.kind, payload=cmd.payload, ttl_s=payload.ttl_s)
        cmd.attempts = 1
        cmd.sent_at = now
        cmd.next_retry_at = now + timedelta(seconds=self.settings.command_retry_backoff_s)
        session.commit()  # persist before publish: the device may ack immediately

        delivered = self.bus.publish_json(topics.commands(device_id), self._wire(cmd))
        session.refresh(cmd)
        # An inline bus can already have applied the command; never clobber that.
        if cmd.state == CommandState.QUEUED.value:
            cmd.state = CommandState.SENT.value if delivered else CommandState.QUEUED.value
        if not delivered:
            cmd.last_error = "transport refused (device partitioned or broker unavailable)"
            audit.record(session, "gateway", "command.transport_failed", cmd.command_id, device_id=device_id)
        session.commit()
        return DispatchResult(command=CommandRead.model_validate(cmd), safety=verdict)

    def _validate_safety(self, payload: CommandCreate) -> SafetyVerdict:
        env = load_envelope()
        if payload.kind in (CommandKind.SETPOINT, CommandKind.RECIPE):
            sp = payload.payload if payload.kind == CommandKind.SETPOINT else payload.payload.get("setpoints", {})
            return validate_setpoints(sp, env)
        return SafetyVerdict(ok=True)

    @staticmethod
    def _wire(cmd: Command) -> dict[str, Any]:
        return {
            "command_id": cmd.command_id,
            "device_id": cmd.device_id,
            "kind": cmd.kind,
            "payload": cmd.payload,
            "idempotency_key": cmd.idempotency_key,
            "issued_at": cmd.issued_at.isoformat(),
            "expires_at": as_utc(cmd.expires_at).isoformat(),
            "attempt": cmd.attempts,
        }

    # -- inbound -----------------------------------------------------------
    def handle_ack(self, session: Session, ack: CommandAck) -> Command | None:
        cmd = session.get(Command, ack.command_id)
        if cmd is None:
            audit.record(session, "gateway", "command.ack_unknown", ack.command_id, device_id=ack.device_id)
            return None
        if cmd.device_id != ack.device_id:
            audit.record(session, "gateway", "command.ack_device_mismatch", ack.command_id,
                         expected=cmd.device_id, got=ack.device_id)
            return None

        already_closed = CommandState(cmd.state) in CLOSED_COMMAND_STATES
        session.add(CommandAckRow(command_id=cmd.command_id, device_id=ack.device_id, state=ack.state.value,
                                  detail=ack.detail, duplicate=already_closed or ack.duplicate, ts=ack.ts))
        if already_closed:
            session.flush()
            return cmd

        if ack.state == AckState.RECEIVED:
            cmd.state = CommandState.RECEIVED.value
        elif ack.state == AckState.APPLIED:
            cmd.state = CommandState.APPLIED.value
            cmd.closed_at = utcnow()
            cmd.last_error = None
        elif ack.state == AckState.FAILED:
            cmd.state = CommandState.FAILED.value
            cmd.closed_at = utcnow()
            cmd.last_error = ack.detail or "device reported failure"
        audit.record(session, "device", f"command.{ack.state.value}", cmd.command_id,
                     device_id=cmd.device_id, detail=ack.detail)
        session.flush()
        return cmd

    # -- sweeps ------------------------------------------------------------
    def retry_due(self, session: Session) -> list[str]:
        now = utcnow()
        due = session.scalars(
            select(Command).where(
                Command.state.in_([CommandState.QUEUED.value, CommandState.SENT.value, CommandState.RECEIVED.value]),
                Command.attempts < self.settings.command_max_attempts,
                Command.next_retry_at.is_not(None),
            )
        ).all()
        retried: list[str] = []
        for cmd in due:
            if as_utc(cmd.expires_at) <= now or as_utc(cmd.next_retry_at) > now:
                continue
            device = session.get(Device, cmd.device_id)
            if device is not None and device.status == DeviceStatus.OFFLINE.value:
                continue  # do not shout into the void; wait for the heartbeat
            cmd.attempts += 1
            cmd.next_retry_at = now + timedelta(seconds=self.settings.command_retry_backoff_s * cmd.attempts)
            session.commit()
            delivered = self.bus.publish_json(topics.commands(cmd.device_id), self._wire(cmd))
            session.refresh(cmd)
            if cmd.state == CommandState.QUEUED.value and delivered:
                cmd.state = CommandState.SENT.value
            audit.record(session, "gateway", "command.retry", cmd.command_id,
                         attempt=cmd.attempts, delivered=delivered)
            retried.append(cmd.command_id)
        session.commit()
        return retried

    def expire(self, session: Session) -> list[str]:
        now = utcnow()
        open_cmds = session.scalars(
            select(Command).where(Command.state.in_([
                CommandState.QUEUED.value, CommandState.SENT.value, CommandState.RECEIVED.value]))
        ).all()
        expired: list[str] = []
        for cmd in open_cmds:
            if as_utc(cmd.expires_at) <= now:
                cmd.state = CommandState.EXPIRED.value
                cmd.closed_at = now
                cmd.last_error = cmd.last_error or "ttl expired without acknowledgement"
                audit.record(session, "gateway", "command.expired", cmd.command_id,
                             device_id=cmd.device_id, attempts=cmd.attempts)
                expired.append(cmd.command_id)
        session.commit()
        return expired


def _fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, default=str)


class Gateway:
    """Owns bus subscriptions and the periodic reliability sweep."""

    def __init__(self, bus: MessageBus, settings: Settings) -> None:
        self.bus = bus
        self.settings = settings
        self.dispatcher = CommandDispatcher(bus, settings)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        self.bus.subscribe(topics.SUB_TELEMETRY, self._on_telemetry)
        self.bus.subscribe(topics.SUB_ACK, self._on_ack)
        self.bus.subscribe(topics.SUB_STATUS, self._on_status)
        self.bus.subscribe(topics.SUB_HEALTH, self._on_health)
        self.bus.start()
        if self.settings.background_workers and self._thread is None:
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="gateway-sweep", daemon=True)
            self._thread.start()
        log.info("gateway started on %s bus", self.bus.name)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
        self.bus.stop()

    def _loop(self) -> None:
        while not self._stop.wait(self.settings.sweep_interval_s):
            try:
                self.sweep()
            except Exception:
                log.exception("gateway sweep failed")

    def sweep(self) -> dict[str, Any]:
        with session_scope() as session:
            retried = self.dispatcher.retry_due(session)
            expired = self.dispatcher.expire(session)
            offline = devices.mark_offline(session, self.settings.heartbeat_timeout_s)
        return {"retried": retried, "expired": expired, "offline": offline}

    # -- handlers (each owns its own session) -------------------------------
    def _on_telemetry(self, msg: Message) -> None:
        from .ingestion import ingest_one  # lazy: ingestion imports biology

        try:
            t = Telemetry.model_validate(msg.json())
        except ValidationError as exc:
            self._invalid(msg, exc)
            return
        with session_scope() as session:
            ingest_one(session, t, source="mqtt")

    def _on_ack(self, msg: Message) -> None:
        try:
            ack = CommandAck.model_validate(msg.json())
        except ValidationError as exc:
            self._invalid(msg, exc)
            return
        with session_scope() as session:
            self.dispatcher.handle_ack(session, ack)

    def _on_status(self, msg: Message) -> None:
        try:
            status_msg = DeviceStatusMessage.model_validate(msg.json())
        except ValidationError as exc:
            self._invalid(msg, exc)
            return
        with session_scope() as session:
            device = session.get(Device, status_msg.device_id)
            if device is None:
                return
            if status_msg.state == "online":
                devices.touch(session, status_msg.device_id, firmware=status_msg.firmware)
            else:
                device.status = DeviceStatus.OFFLINE.value
                audit.record(session, "device", "device.offline", device.device_id,
                             reason=status_msg.reason or "last will")

    def _on_health(self, msg: Message) -> None:
        try:
            health = DeviceHealth.model_validate(msg.json())
        except ValidationError as exc:
            self._invalid(msg, exc)
            return
        with session_scope() as session:
            devices.record_health(session, health)

    def _invalid(self, msg: Message, exc: Exception) -> None:
        log.warning("invalid payload on %s: %s", msg.topic, exc)
        try:
            with session_scope() as session:
                audit.record(session, "gateway", "bus.invalid_payload", msg.topic, error=str(exc)[:500])
        except Exception:
            log.exception("could not audit invalid payload")


_gateway: Gateway | None = None


def set_gateway(gw: Gateway | None) -> None:
    global _gateway
    _gateway = gw


def get_gateway() -> Gateway:
    if _gateway is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "gateway not started")
    return _gateway


# ---------------------------------------------------------------------------
# routes
# ---------------------------------------------------------------------------
@router.post("/devices/{device_id}/commands", response_model=DispatchResult,
             status_code=status.HTTP_201_CREATED)
def dispatch_command(
    device_id: str,
    payload: CommandCreate,
    session: Session = Depends(get_session),
    principal: Principal = Depends(require_operator),
    gw: Gateway = Depends(get_gateway),
) -> DispatchResult:
    return gw.dispatcher.dispatch(session, device_id, payload, principal.actor)


@router.get("/devices/{device_id}/commands", response_model=list[CommandRead])
def list_commands(
    device_id: str,
    state: CommandState | None = None,
    limit: int = Query(default=50, ge=1, le=500),
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> list[Command]:
    stmt = select(Command).where(Command.device_id == device_id).order_by(Command.issued_at.desc()).limit(limit)
    if state:
        stmt = stmt.where(Command.state == state.value)
    return list(session.scalars(stmt))


@router.get("/commands/{command_id}")
def command_detail(
    command_id: str,
    session: Session = Depends(get_session),
    _: Principal = Depends(require_operator),
) -> dict[str, Any]:
    cmd = session.get(Command, command_id)
    if cmd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown command")
    acks = list(session.scalars(
        select(CommandAckRow).where(CommandAckRow.command_id == command_id).order_by(CommandAckRow.id)
    ))
    return {
        "command": CommandRead.model_validate(cmd).model_dump(),
        "acks": [{"state": a.state, "ts": a.ts, "detail": a.detail, "duplicate": a.duplicate} for a in acks],
    }


@router.post("/gateway/sweep")
def sweep_now(_: Principal = Depends(require_operator), gw: Gateway = Depends(get_gateway)) -> dict[str, Any]:
    """Run the reliability sweep on demand (also used by the demo and tests)."""
    return gw.sweep()
