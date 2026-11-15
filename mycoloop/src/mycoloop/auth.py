"""Auth: operator API keys + per-device tokens.

Deliberately small (Phase 1 "basic auth/API keys"), but the *shape* is final:
every write is attributed to a principal, and that principal is what lands in
the append-only audit log. Swapping in OAuth/OIDC later touches only this file.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from .config import Settings, get_settings
from .db import get_session
from .models import Device


def hash_token(token: str, secret: str) -> str:
    return hashlib.sha256(f"{secret}:{token}".encode()).hexdigest()


def mint_device_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass(frozen=True)
class Principal:
    actor: str
    role: str  # operator | admin | device


def _key_principal(api_key: str | None, settings: Settings) -> Principal:
    if not api_key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing X-API-Key")
    if hmac.compare_digest(api_key, settings.admin_api_key):
        return Principal(actor="admin", role="admin")
    if hmac.compare_digest(api_key, settings.operator_api_key):
        return Principal(actor="operator", role="operator")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")


def require_operator(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Principal:
    return _key_principal(x_api_key, settings)


def require_admin(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> Principal:
    p = _key_principal(x_api_key, settings)
    if p.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin key required")
    return p


def authenticate_device(session: Session, device_id: str, token: str | None, settings: Settings) -> Device:
    device = session.get(Device, device_id)
    if device is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"unknown device {device_id}")
    if not token or not hmac.compare_digest(device.token_hash, hash_token(token, settings.device_token_secret)):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid device token")
    return device


def require_device(
    device_id: str,
    x_device_token: str | None = Header(default=None),
    session: Session = Depends(get_session),
    settings: Settings = Depends(get_settings),
) -> Device:
    return authenticate_device(session, device_id, x_device_token, settings)
