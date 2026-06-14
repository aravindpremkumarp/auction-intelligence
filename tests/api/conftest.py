"""
tests/api/conftest.py
---------------------
Shared fixtures. Stubs Neo4j + agent imports so api.main can be imported
without live credentials, and stubs Supabase JWT verification so tests can
mint fake bearer tokens without contacting a real Supabase project.
"""
from __future__ import annotations

import os
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

# Make the repo root importable so `from api.main import app` resolves.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

os.environ.setdefault("NEO4J_URI", "bolt://fake:7687")
os.environ.setdefault("NEO4J_USERNAME", "fake")
os.environ.setdefault("NEO4J_PASSWORD", "fake")
os.environ.setdefault("NEO4J_DATABASE", "neo4j")
os.environ.setdefault("OPENROUTER_API_KEY", "fake")
os.environ.setdefault("OPENROUTER_MODEL", "fake/model")
os.environ.setdefault("OPENAI_API_KEY", "fake")
os.environ.setdefault("SUPABASE_URL", "https://fake.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "fake-anon")
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("RATELIMIT_DISABLED", "1")
os.environ.setdefault("AUTH_ENABLED", "true")


def _install_stub_agent() -> None:
    """Replace api.agent with a stub so importing api.main doesn't build a real agent."""
    mod = types.ModuleType("api.agent")

    class ChatDeps:  # noqa: D401
        def __init__(self, *args, **kwargs): pass

    class _Agent:
        async def run(self, *args, **kwargs):  # pragma: no cover - not exercised
            raise RuntimeError("stub agent")

    mod.ChatDeps = ChatDeps
    mod.agent = _Agent()
    sys.modules["api.agent"] = mod


def _install_stub_neo4j_client() -> None:
    """Replace api.neo4j_client.run_query with an in-memory fake store."""
    mod = types.ModuleType("api.neo4j_client")
    feedback: list[dict] = []
    users: dict[str, dict] = {}              # keyed by supabase_id
    mod._store = feedback  # back-compat for feedback tests
    mod._users = users
    mod._feedback = feedback

    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _as_dt(v) -> datetime:
        if isinstance(v, datetime):
            return v
        if isinstance(v, str):
            s = v.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(s)
            except ValueError:
                return _now()
        return _now()

    def run_query(cypher: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        c = cypher.strip()

        # ── Feedback ────────────────────────────────────────────────────
        if c.startswith("CREATE (f:Feedback"):
            feedback.append({
                "id": params["id"],
                "kind": params.get("kind") or "message",
                "rating": params.get("rating"),
                "text": params.get("text"),
                "session_id": params["session_id"],
                "message_index": params["message_index"],
                "question": params.get("question") or "",
                "answer": params.get("answer") or "",
                "artifacts_json": params["artifacts_json"],
                "context_turns_json": params.get("context_turns_json", "[]"),
                "user_agent": params.get("user_agent"),
                "page_url": params.get("page_url"),
                "user_id": params.get("user_id"),
                "created_at": params["created_at"],
                "resolved": False,
            })
            return [{"id": params["id"]}]
        if c.startswith("MATCH (f:Feedback)\n        WHERE"):
            unresolved = params.get("unresolved", True)
            rating = params.get("rating")
            kind = params.get("kind")
            limit = params.get("limit", 50)
            rows = [r for r in feedback
                    if (not unresolved or not r["resolved"])
                    and (rating is None or r["rating"] == rating)
                    and (kind is None or (r.get("kind") or "message") == kind)]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return [{"f": dict(r)} for r in rows[:limit]]
        if c.startswith("MATCH (f:Feedback {id: $id})"):
            for r in feedback:
                if r["id"] == params["id"]:
                    r["resolved"] = True
                    r["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    return [{"id": r["id"]}]
            return []

        # ── Users ───────────────────────────────────────────────────────
        if c.startswith("MERGE (u:User {supabase_id: $sub})"):
            sub = params["sub"]
            existing = users.get(sub)
            email = (params.get("email") or "").lower()
            now = _as_dt(params["now"])
            if not existing:
                users[sub] = {
                    "supabase_id": sub,
                    "email": email,
                    "name": params.get("name") or "",
                    "role": params.get("role") or "user",
                    "enabled": True,
                    "created_at": now,
                    "last_login_at": now,
                }
            else:
                existing["email"] = email
                if not existing.get("name"):
                    existing["name"] = params.get("name") or ""
                existing["last_login_at"] = now
                # Admin promotion via create_admin.py takes an explicit role.
                if params.get("role") == "admin":
                    existing["role"] = "admin"
                if "enabled" in params and params.get("enabled") is not None:
                    existing["enabled"] = params["enabled"]
            return [{"u": dict(users[sub])}]
        if c.startswith("MATCH (u:User {supabase_id: $sub}) RETURN"):
            u = users.get(params["sub"])
            return [{"u": dict(u)}] if u else []
        if c.startswith("MATCH (u:User {supabase_id: $sub}) SET u.name"):
            u = users.get(params["sub"])
            if u:
                u["name"] = params["name"]
                return [{"u": dict(u)}]
            return []
        if c.startswith("MATCH (u:User {supabase_id: $sub})\n        SET u.chat_count"):
            u = users.get(params["sub"])
            if not u:
                return []
            bucket = params["bucket"]
            if u.get("chat_bucket") == bucket:
                u["chat_count"] = (u.get("chat_count") or 0) + 1
            else:
                u["chat_bucket"] = bucket
                u["chat_count"] = 1
            return [{"count": u["chat_count"]}]
        if c.startswith("MATCH (u:User {supabase_id: $sub})\n        SET u.plan_expires_at"):
            u = users.get(params["sub"])
            if not u:
                return []
            u["plan_expires_at"] = params["expires_at"]
            return [{"u": dict(u)}]
        if c.startswith("MATCH (u:User)\n        RETURN u { .* } AS u"):
            limit = params.get("limit", 200)
            rows = sorted(users.values(),
                          key=lambda r: r.get("created_at") or _now(),
                          reverse=True)
            return [{"u": dict(r)} for r in rows[:limit]]
        if c.startswith("MATCH (u:User {supabase_id: $sub})\n        SET u.role"):
            u = users.get(params["sub"])
            if not u:
                return []
            if params.get("role") is not None:
                u["role"] = params["role"]
            if params.get("enabled") is not None:
                u["enabled"] = params["enabled"]
            return [{"u": dict(u)}]

        # ── Schema init (idempotent constraints/indexes) ────────────────
        if c.startswith("CREATE CONSTRAINT") or c.startswith("CREATE INDEX"):
            return []

        return []

    def run_read_query(cypher: str, params: dict | None = None,
                       timeout: float = 10.0, max_rows: int = 200) -> list[dict]:
        """Default stub — tests that exercise read-only tools should
        monkeypatch api.tools.cypher_tools.run_read_query directly."""
        return []

    async def run_query_async(cypher: str, params: dict | None = None) -> list[dict]:
        return run_query(cypher, params)

    async def run_read_query_async(cypher: str, params: dict | None = None,
                                   timeout: float = 10.0, max_rows: int = 200) -> list[dict]:
        return run_read_query(cypher, params, timeout=timeout, max_rows=max_rows)

    mod.run_query = run_query
    mod.run_read_query = run_read_query
    mod.run_query_async = run_query_async
    mod.run_read_query_async = run_read_query_async
    sys.modules["api.neo4j_client"] = mod


def _install_stub_supabase_jwt() -> None:
    """Replace api.auth.supabase_jwt.verify_access_token with a fake that
    decodes `Bearer test-<json>` tokens, so tests don't need a real Supabase
    project. Tests use the `auth_header` helper below to mint tokens."""
    import json as _json
    import base64 as _b64

    mod = types.ModuleType("api.auth.supabase_jwt")

    def verify_access_token(token: str) -> dict:
        import jwt as _jwt
        if not token.startswith("test-"):
            raise _jwt.InvalidTokenError("unknown test token")
        raw = token[len("test-"):]
        # Pad base64 as needed.
        pad = "=" * (-len(raw) % 4)
        claims = _json.loads(_b64.urlsafe_b64decode(raw + pad).decode("utf-8"))
        if claims.get("_expired"):
            raise _jwt.ExpiredSignatureError("test token expired")
        if claims.get("_invalid"):
            raise _jwt.InvalidTokenError("test token invalid")
        return claims

    mod.verify_access_token = verify_access_token
    sys.modules["api.auth.supabase_jwt"] = mod


def auth_header(
    sub: str = "sub-1",
    email: str = "a@b.com",
    name: str = "A",
    verified: bool = True,
    role_claim: str = "authenticated",
) -> dict[str, str]:
    """Return an Authorization header carrying a fake Supabase-style token.

    The conftest stub decodes `Bearer test-<base64url(json)>` and returns the
    JSON payload as claims. `role_claim` controls the Supabase JWT `role`
    claim (not the app-level :User.role — that comes from Neo4j).
    """
    import json as _json
    import base64 as _b64
    claims = {
        "sub": sub,
        "email": email,
        "aud": "authenticated",
        "role": role_claim,
        "user_metadata": {"name": name},
        "email_confirmed_at": "2026-01-01T00:00:00Z" if verified else None,
    }
    body = _b64.urlsafe_b64encode(_json.dumps(claims).encode("utf-8")).rstrip(b"=").decode()
    return {"Authorization": f"Bearer test-{body}"}


_install_stub_agent()
_install_stub_neo4j_client()
_install_stub_supabase_jwt()
