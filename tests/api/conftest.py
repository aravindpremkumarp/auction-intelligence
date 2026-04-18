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
    store: list[dict] = []
    mod._store = store  # exposed for tests

    def run_query(cypher: str, params: dict | None = None) -> list[dict]:
        params = params or {}
        c = cypher.strip()
        if c.startswith("CREATE (f:Feedback"):
            store.append({
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
                "created_at": params["created_at"],
                "resolved": False,
            })
            return [{"id": params["id"]}]
        if c.startswith("MATCH (f:Feedback)\n        WHERE"):
            unresolved = params.get("unresolved", True)
            rating = params.get("rating")
            kind = params.get("kind")
            limit = params.get("limit", 50)
            rows = [r for r in store
                    if (not unresolved or not r["resolved"])
                    and (rating is None or r["rating"] == rating)
                    and (kind is None or (r.get("kind") or "message") == kind)]
            rows.sort(key=lambda r: r["created_at"], reverse=True)
            return [{"f": dict(r)} for r in rows[:limit]]
        if c.startswith("MATCH (f:Feedback {id: $id})"):
            for r in store:
                if r["id"] == params["id"]:
                    r["resolved"] = True
                    return [{"id": r["id"]}]
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
