"""
api/auth/repository.py
----------------------
Cypher gateway for User / RefreshToken / VerificationToken.

All Cypher leads with a unique prefix so the test stub (tests/api/conftest.py)
can route queries to its in-memory store without executing them.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from api.neo4j_client import run_query


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _user_from_row(row: dict[str, Any]) -> dict[str, Any]:
    u = row["u"] if "u" in row else row
    created = u.get("created_at")
    last = u.get("last_login_at")
    return {
        "id": u["id"],
        "email": u["email"],
        "password_hash": u.get("password_hash") or "",
        "name": u.get("name") or "",
        "role": u.get("role") or "user",
        "email_verified": bool(u.get("email_verified", False)),
        "enabled": bool(u.get("enabled", True)),
        "created_at": created.iso_format() if hasattr(created, "iso_format") else str(created or ""),
        "last_login_at": last.iso_format() if hasattr(last, "iso_format") else (str(last) if last else None),
    }


# ── Users ───────────────────────────────────────────────────────────────────
def create_user(email: str, password_hash: str, name: str) -> dict[str, Any] | None:
    rows = run_query(
        """
        CREATE (u:User {
          id: $id, email: toLower($email), password_hash: $hash, name: $name,
          role: 'user', email_verified: false, enabled: true,
          created_at: datetime($now)
        })
        RETURN u { .* } AS u
        """,
        {
            "id": str(uuid.uuid4()),
            "email": email,
            "hash": password_hash,
            "name": name,
            "now": _utcnow_iso(),
        },
    )
    return _user_from_row(rows[0]) if rows else None


def get_user_by_email(email: str) -> dict[str, Any] | None:
    rows = run_query(
        "MATCH (u:User {email: toLower($email)}) RETURN u { .* } AS u",
        {"email": email},
    )
    return _user_from_row(rows[0]) if rows else None


def get_user_by_id(user_id: str) -> dict[str, Any] | None:
    rows = run_query(
        "MATCH (u:User {id: $id}) RETURN u { .* } AS u",
        {"id": user_id},
    )
    return _user_from_row(rows[0]) if rows else None


def update_last_login(user_id: str) -> None:
    run_query(
        "MATCH (u:User {id: $id}) SET u.last_login_at = datetime($now)",
        {"id": user_id, "now": _utcnow_iso()},
    )


def set_email_verified(user_id: str) -> None:
    run_query(
        "MATCH (u:User {id: $id}) SET u.email_verified = true",
        {"id": user_id},
    )


def set_password_hash(user_id: str, password_hash: str) -> None:
    run_query(
        "MATCH (u:User {id: $id}) SET u.password_hash = $hash",
        {"id": user_id, "hash": password_hash},
    )


def patch_user_self(user_id: str, name: str | None) -> dict[str, Any] | None:
    if name is None:
        return get_user_by_id(user_id)
    rows = run_query(
        "MATCH (u:User {id: $id}) SET u.name = $name RETURN u { .* } AS u",
        {"id": user_id, "name": name},
    )
    return _user_from_row(rows[0]) if rows else None


def admin_list_users(limit: int = 200) -> list[dict[str, Any]]:
    rows = run_query(
        """
        MATCH (u:User)
        RETURN u { .* } AS u
        ORDER BY u.created_at DESC
        LIMIT $limit
        """,
        {"limit": limit},
    )
    return [_user_from_row(r) for r in rows]


def admin_patch_user(user_id: str, role: str | None, enabled: bool | None) -> dict[str, Any] | None:
    rows = run_query(
        """
        MATCH (u:User {id: $id})
        SET u.role = coalesce($role, u.role),
            u.enabled = coalesce($enabled, u.enabled)
        RETURN u { .* } AS u
        """,
        {"id": user_id, "role": role, "enabled": enabled},
    )
    return _user_from_row(rows[0]) if rows else None


# ── Refresh tokens ──────────────────────────────────────────────────────────
def store_refresh_token(
    jti: str, user_id: str, expires_at: datetime,
    user_agent: str | None = None, ip: str | None = None,
) -> None:
    run_query(
        """
        CREATE (r:RefreshToken {
          jti: $jti, user_id: $uid, issued_at: datetime($now),
          expires_at: datetime($exp), user_agent: $ua, ip: $ip
        })
        """,
        {
            "jti": jti, "uid": user_id,
            "now": _utcnow_iso(),
            "exp": expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "ua": user_agent, "ip": ip,
        },
    )


def is_refresh_token_active(jti: str) -> bool:
    rows = run_query(
        """
        MATCH (r:RefreshToken {jti: $jti})
        WHERE r.revoked_at IS NULL AND r.expires_at > datetime()
        RETURN r.user_id AS user_id
        """,
        {"jti": jti},
    )
    return bool(rows)


def revoke_refresh_token(jti: str) -> None:
    run_query(
        "MATCH (r:RefreshToken {jti: $jti}) SET r.revoked_at = datetime($now)",
        {"jti": jti, "now": _utcnow_iso()},
    )


# ── Verification tokens (verify email + reset password) ─────────────────────
def store_verification_token(token: str, user_id: str, purpose: str, expires_at: datetime) -> None:
    run_query(
        """
        CREATE (v:VerificationToken {
          token: $t, user_id: $uid, purpose: $p,
          expires_at: datetime($exp)
        })
        """,
        {
            "t": token, "uid": user_id, "p": purpose,
            "exp": expires_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
    )


def consume_verification_token(token: str, purpose: str) -> str | None:
    """Returns user_id if token is valid-and-unused, atomically marking it used."""
    rows = run_query(
        """
        MATCH (v:VerificationToken {token: $t, purpose: $p})
        WHERE v.used_at IS NULL AND v.expires_at > datetime()
        SET v.used_at = datetime($now)
        RETURN v.user_id AS user_id
        """,
        {"t": token, "p": purpose, "now": _utcnow_iso()},
    )
    return rows[0]["user_id"] if rows else None
