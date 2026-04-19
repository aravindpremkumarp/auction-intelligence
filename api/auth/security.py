"""
api/auth/security.py
--------------------
Password hashing (bcrypt) and JWT creation/validation (HS256).

Env:
    JWT_SECRET             required in prod; a dev-only fallback is used if unset
    JWT_ACCESS_TTL_MIN     default 15
    JWT_REFRESH_TTL_DAYS   default 7
"""
from __future__ import annotations

import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

import bcrypt
import jwt

_ALGO = "HS256"
# bcrypt truncates inputs past 72 bytes; we pre-truncate to be explicit.
_BCRYPT_MAX = 72


def _secret() -> str:
    s = os.environ.get("JWT_SECRET", "")
    if s:
        return s
    if os.environ.get("APP_ENV", "").lower() in {"dev", "test"}:
        return "dev-insecure-secret-change-me"
    raise RuntimeError("JWT_SECRET is not set")


def _access_ttl() -> timedelta:
    return timedelta(minutes=int(os.environ.get("JWT_ACCESS_TTL_MIN", "15")))


def _refresh_ttl() -> timedelta:
    return timedelta(days=int(os.environ.get("JWT_REFRESH_TTL_DAYS", "7")))


# ── passwords ───────────────────────────────────────────────────────────────
def _as_bytes(raw: str) -> bytes:
    return raw.encode("utf-8")[:_BCRYPT_MAX]


def hash_password(raw: str) -> str:
    return bcrypt.hashpw(_as_bytes(raw), bcrypt.gensalt()).decode("ascii")


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_as_bytes(raw), hashed.encode("ascii"))
    except Exception:
        return False


# ── JWTs ────────────────────────────────────────────────────────────────────
def _now() -> datetime:
    return datetime.now(timezone.utc)


def create_access_token(user_id: str, role: str) -> str:
    now = _now()
    payload = {
        "sub": user_id,
        "role": role,
        "type": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + _access_ttl()).timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO)


def create_refresh_token(user_id: str) -> tuple[str, str, datetime]:
    """Returns (token, jti, expires_at). Caller persists the jti for revocation."""
    now = _now()
    jti = str(uuid.uuid4())
    exp = now + _refresh_ttl()
    payload = {
        "sub": user_id,
        "type": "refresh",
        "jti": jti,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGO), jti, exp


def decode_token(token: str, expected_type: Literal["access", "refresh"]) -> dict[str, Any]:
    payload = jwt.decode(
        token,
        _secret(),
        algorithms=[_ALGO],
        options={"require": ["exp", "sub"]},
    )
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError(f"expected {expected_type} token")
    return payload


# ── misc ────────────────────────────────────────────────────────────────────
def generate_url_safe_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)
