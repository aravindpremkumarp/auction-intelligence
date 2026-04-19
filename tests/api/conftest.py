"""
tests/api/conftest.py
---------------------
Shared fixtures. Stubs Neo4j + agent imports so api.main can be imported
without live credentials or external dependencies.
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
os.environ.setdefault("JWT_SECRET", "test-secret")
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
    users: dict[str, dict] = {}              # keyed by id
    users_by_email: dict[str, str] = {}       # email → id
    refresh: dict[str, dict] = {}             # keyed by jti
    verify: dict[str, dict] = {}              # keyed by token
    mod._store = feedback  # back-compat for feedback tests
    mod._users = users
    mod._users_by_email = users_by_email
    mod._refresh = refresh
    mod._verify = verify
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

        # ── Feedback (unchanged) ────────────────────────────────────────
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
                    # Mirror the Neo4j SET f.resolved_at = datetime() so
                    # FeedbackRecord.resolved_at is testable end-to-end.
                    from datetime import datetime, timezone
                    r["resolved_at"] = datetime.now(timezone.utc).isoformat()
                    return [{"id": r["id"]}]
            return []

        # ── Users ───────────────────────────────────────────────────────
        if c.startswith("CREATE (u:User {"):
            email = params["email"].lower()
            if email in users_by_email:
                raise RuntimeError("user email already exists")
            uid = params["id"]
            users[uid] = {
                "id": uid,
                "email": email,
                "password_hash": params["hash"],
                "name": params["name"],
                "role": "user",
                "email_verified": False,
                "enabled": True,
                "created_at": _as_dt(params["now"]),
                "last_login_at": None,
            }
            users_by_email[email] = uid
            return [{"u": dict(users[uid])}]
        if c.startswith("MATCH (u:User {email: toLower($email)}) RETURN"):
            uid = users_by_email.get(params["email"].lower())
            if not uid:
                return []
            return [{"u": dict(users[uid])}]
        if c.startswith("MATCH (u:User {id: $id}) RETURN"):
            u = users.get(params["id"])
            return [{"u": dict(u)}] if u else []
        if c.startswith("MATCH (u:User {id: $id}) SET u.last_login_at"):
            u = users.get(params["id"])
            if u:
                u["last_login_at"] = _now()
            return []
        if c.startswith("MATCH (u:User {id: $id}) SET u.email_verified"):
            u = users.get(params["id"])
            if u:
                u["email_verified"] = True
            return []
        if c.startswith("MATCH (u:User {id: $id}) SET u.password_hash"):
            u = users.get(params["id"])
            if u:
                u["password_hash"] = params["hash"]
            return []
        if c.startswith("MATCH (u:User {id: $id}) SET u.name"):
            u = users.get(params["id"])
            if u:
                u["name"] = params["name"]
                return [{"u": dict(u)}]
            return []
        if c.startswith("MATCH (u:User)\n        RETURN u { .* } AS u"):
            limit = params.get("limit", 200)
            rows = sorted(users.values(),
                          key=lambda r: r.get("created_at") or _now(),
                          reverse=True)
            return [{"u": dict(r)} for r in rows[:limit]]
        if c.startswith("MATCH (u:User {id: $id})\n        SET u.role"):
            u = users.get(params["id"])
            if not u:
                return []
            if params.get("role") is not None:
                u["role"] = params["role"]
            if params.get("enabled") is not None:
                u["enabled"] = params["enabled"]
            return [{"u": dict(u)}]
        # Admin bootstrap MERGE (used by scripts.create_admin)
        if c.startswith("MERGE (u:User {email:"):
            email = params["email"].lower()
            uid = users_by_email.get(email) or params["id"]
            existing = users.get(uid)
            if not existing:
                users[uid] = {
                    "id": uid, "email": email,
                    "created_at": _as_dt(params["now"]),
                    "last_login_at": None,
                }
                users_by_email[email] = uid
            u = users[uid]
            u["password_hash"] = params["hash"]
            u["name"] = params["name"]
            u["role"] = "admin"
            u["email_verified"] = True
            u["enabled"] = True
            return [{"id": uid}]

        # ── Refresh tokens ──────────────────────────────────────────────
        if c.startswith("CREATE (r:RefreshToken"):
            refresh[params["jti"]] = {
                "jti": params["jti"],
                "user_id": params["uid"],
                "issued_at": _now(),
                "expires_at": _as_dt(params["exp"]),
                "revoked_at": None,
                "user_agent": params.get("ua"),
                "ip": params.get("ip"),
            }
            return []
        if c.startswith("MATCH (r:RefreshToken {jti: $jti})\n        WHERE"):
            r = refresh.get(params["jti"])
            if not r:
                return []
            if r["revoked_at"] is not None:
                return []
            if r["expires_at"] <= _now():
                return []
            return [{"user_id": r["user_id"]}]
        if c.startswith("MATCH (r:RefreshToken {jti: $jti}) SET r.revoked_at"):
            r = refresh.get(params["jti"])
            if r:
                r["revoked_at"] = _now()
            return []

        # ── Verification tokens ─────────────────────────────────────────
        if c.startswith("CREATE (v:VerificationToken"):
            verify[params["t"]] = {
                "token": params["t"],
                "user_id": params["uid"],
                "purpose": params["p"],
                "expires_at": _as_dt(params["exp"]),
                "used_at": None,
            }
            return []
        if c.startswith("MATCH (v:VerificationToken {token: $t, purpose: $p})"):
            v = verify.get(params["t"])
            if not v:
                return []
            if v["purpose"] != params["p"]:
                return []
            if v["used_at"] is not None:
                return []
            if v["expires_at"] <= _now():
                return []
            v["used_at"] = _now()
            return [{"user_id": v["user_id"]}]

        # ── Schema init (idempotent constraints/indexes) ────────────────
        if c.startswith("CREATE CONSTRAINT") or c.startswith("CREATE INDEX"):
            return []

        return []

    def run_read_query(cypher: str, params: dict | None = None,
                       timeout: float = 10.0, max_rows: int = 200) -> list[dict]:
        """Default stub — tests that exercise read-only tools should
        monkeypatch api.tools.cypher_tools.run_read_query directly."""
        return []

    mod.run_query = run_query
    mod.run_read_query = run_read_query
    sys.modules["api.neo4j_client"] = mod


_install_stub_agent()
_install_stub_neo4j_client()
