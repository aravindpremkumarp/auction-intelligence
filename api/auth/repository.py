"""
api/auth/repository.py
----------------------
Cypher gateway for the Neo4j `:User` profile mirror.

Identity lives in Supabase; Neo4j keeps a profile row keyed by `supabase_id`
so auction graph edges (stars, feedback, uploaded documents) can attach to a
stable node. Every authenticated FastAPI request upserts this row via
`upsert_user_from_supabase`, so downstream handlers can safely
`MATCH (u:User {supabase_id:$sub})` without a prior merge.

All Cypher leads with a unique prefix so the test stub
(tests/api/conftest.py) can route queries to its in-memory store.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

from api.neo4j_client import run_query_async


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _user_from_row(row: dict[str, Any]) -> dict[str, Any]:
    u = row["u"] if "u" in row else row
    created = u.get("created_at")
    last = u.get("last_login_at")
    return {
        "id": u["supabase_id"],
        "supabase_id": u["supabase_id"],
        "email": u["email"],
        "name": u.get("name") or "",
        "role": u.get("role") or "user",
        "enabled": bool(u.get("enabled", True)),
        "created_at": created.iso_format() if hasattr(created, "iso_format") else str(created or ""),
        "last_login_at": last.iso_format() if hasattr(last, "iso_format") else (str(last) if last else None),
    }


async def upsert_user_from_supabase(
    sub: str, email: str, name: str | None,
) -> dict[str, Any]:
    """Materialise/refresh the Neo4j profile for a Supabase-authenticated user.

    Bootstraps role=admin on first create if `email == ADMIN_BOOTSTRAP_EMAIL`.
    """
    bootstrap = (os.environ.get("ADMIN_BOOTSTRAP_EMAIL") or "").strip().lower()
    role = "admin" if bootstrap and email.lower() == bootstrap else "user"
    rows = await run_query_async(
        """
        MERGE (u:User {supabase_id: $sub})
          ON CREATE SET
            u.email = toLower($email),
            u.name = $name,
            u.role = $role,
            u.enabled = true,
            u.created_at = datetime($now),
            u.last_login_at = datetime($now)
          ON MATCH SET
            u.email = toLower($email),
            u.name = coalesce(u.name, $name),
            u.last_login_at = datetime($now)
        RETURN u { .* } AS u
        """,
        {
            "sub": sub,
            "email": email,
            "name": name or "",
            "role": role,
            "now": _utcnow_iso(),
        },
    )
    return _user_from_row(rows[0])


async def get_user_by_id(supabase_id: str) -> dict[str, Any] | None:
    rows = await run_query_async(
        "MATCH (u:User {supabase_id: $sub}) RETURN u { .* } AS u",
        {"sub": supabase_id},
    )
    return _user_from_row(rows[0]) if rows else None


async def patch_user_self(supabase_id: str, name: str | None) -> dict[str, Any] | None:
    if name is None:
        return await get_user_by_id(supabase_id)
    rows = await run_query_async(
        "MATCH (u:User {supabase_id: $sub}) SET u.name = $name RETURN u { .* } AS u",
        {"sub": supabase_id, "name": name},
    )
    return _user_from_row(rows[0]) if rows else None


async def admin_list_users(limit: int = 200) -> list[dict[str, Any]]:
    rows = await run_query_async(
        """
        MATCH (u:User)
        RETURN u { .* } AS u
        ORDER BY u.created_at DESC
        LIMIT $limit
        """,
        {"limit": limit},
    )
    return [_user_from_row(r) for r in rows]


async def admin_patch_user(
    supabase_id: str, role: str | None, enabled: bool | None,
) -> dict[str, Any] | None:
    rows = await run_query_async(
        """
        MATCH (u:User {supabase_id: $sub})
        SET u.role = coalesce($role, u.role),
            u.enabled = coalesce($enabled, u.enabled)
        RETURN u { .* } AS u
        """,
        {"sub": supabase_id, "role": role, "enabled": enabled},
    )
    return _user_from_row(rows[0]) if rows else None
