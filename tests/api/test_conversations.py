"""Endpoint tests for /conversations (auth gating + router behaviour).

The repository layer is faked in-memory — these tests pin the router
contract: status codes, JSON round-tripping of the opaque message/result
blobs, the property_id filter, and per-user isolation.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace api.conversations.repository functions with an in-memory store."""
    from api.conversations import repository as repo

    convs: dict[tuple[str, str], dict] = {}  # (supabase_id, conv_id) -> row

    def _rows_for(sub: str, pid: str | None = None) -> list[dict]:
        rows = [
            {"id": cid, "title": c["title"], "property_id": c.get("property_id"),
             "updated_at": c["updated_at"]}
            for (s, cid), c in convs.items()
            if s == sub and (pid is None or c.get("property_id") == pid)
        ]
        return sorted(rows, key=lambda r: r["updated_at"], reverse=True)

    def list_conversations(sub: str) -> list[dict]:
        return _rows_for(sub)

    def list_conversations_for_property(sub: str, pid: str) -> list[dict]:
        return _rows_for(sub, pid)

    def get_conversation(sub: str, cid: str) -> dict | None:
        c = convs.get((sub, cid))
        return dict(c, id=cid) if c else None

    def upsert_conversation(supabase_id, conv_id, title, messages_json,
                            api_history_json, results_json, total_count,
                            property_id=None) -> None:
        existing = convs.get((supabase_id, conv_id))
        convs[(supabase_id, conv_id)] = {
            "title": title,
            "messages_json": messages_json,
            "api_history_json": api_history_json,
            "results_json": results_json,
            "total_count": total_count,
            # property_id binds ON CREATE only, mirroring the Cypher.
            "property_id": existing.get("property_id") if existing else property_id,
            "created_at": existing["created_at"] if existing else "2026-01-01T00:00:00Z",
            "updated_at": f"2026-01-02T00:00:0{len(convs) % 10}Z",
        }

    def delete_conversation(sub: str, cid: str) -> None:
        convs.pop((sub, cid), None)

    monkeypatch.setattr(repo, "list_conversations", list_conversations)
    monkeypatch.setattr(repo, "list_conversations_for_property", list_conversations_for_property)
    monkeypatch.setattr(repo, "get_conversation", get_conversation)
    monkeypatch.setattr(repo, "upsert_conversation", upsert_conversation)
    monkeypatch.setattr(repo, "delete_conversation", delete_conversation)
    return convs


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _body(title: str = "Chennai flats", **over) -> dict:
    return {
        "title": title,
        "messages": [{"role": "user", "content": "hi"}],
        "api_history": [{"kind": "request"}],
        "results": [{"auction_id": "a-1"}],
        "total_count": 12,
        **over,
    }


def test_conversations_require_auth(fake_repo: dict) -> None:
    client = _client()
    assert client.get("/conversations").status_code == 401
    assert client.get("/conversations/c-1").status_code == 401
    assert client.put("/conversations/c-1", json=_body()).status_code == 401
    assert client.delete("/conversations/c-1").status_code == 401


def test_conversation_upsert_get_roundtrip(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="sub-c1", email="c1@x.com")

    assert client.put("/conversations/c-1", json=_body(), headers=h).status_code == 204

    r = client.get("/conversations/c-1", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "c-1"
    assert body["title"] == "Chennai flats"
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["api_history"] == [{"kind": "request"}]
    assert body["results"] == [{"auction_id": "a-1"}]
    assert body["total_count"] == 12

    # Update keeps the same id, new title.
    assert client.put("/conversations/c-1", json=_body(title="Renamed"), headers=h).status_code == 204
    assert client.get("/conversations/c-1", headers=h).json()["title"] == "Renamed"


def test_conversation_missing_404_and_isolation(fake_repo: dict) -> None:
    client = _client()
    h1 = auth_header(sub="sub-c2", email="c2@x.com")
    h2 = auth_header(sub="sub-c3", email="c3@x.com")

    assert client.get("/conversations/ghost", headers=h1).status_code == 404

    client.put("/conversations/c-2", json=_body(), headers=h1)
    # Another user can't read someone else's conversation.
    assert client.get("/conversations/c-2", headers=h2).status_code == 404


def test_conversation_list_and_property_filter(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="sub-c4", email="c4@x.com")
    client.put("/conversations/c-a", json=_body(property_id="prop-9"), headers=h)
    client.put("/conversations/c-b", json=_body(), headers=h)

    ids = {c["id"] for c in client.get("/conversations", headers=h).json()["conversations"]}
    assert ids == {"c-a", "c-b"}

    rows = client.get("/conversations?property_id=prop-9", headers=h).json()["conversations"]
    assert [c["id"] for c in rows] == ["c-a"]


def test_conversation_title_validation(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="sub-c5", email="c5@x.com")
    r = client.put("/conversations/c-x", json=_body(title=""), headers=h)
    assert r.status_code == 422


def test_conversation_delete(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="sub-c6", email="c6@x.com")
    client.put("/conversations/c-d", json=_body(), headers=h)
    assert client.delete("/conversations/c-d", headers=h).status_code == 204
    assert client.get("/conversations/c-d", headers=h).status_code == 404


def test_conversation_corrupt_json_degrades(fake_repo: dict) -> None:
    """Bad stored JSON falls back to empty values instead of 500ing."""
    client = _client()
    h = auth_header(sub="sub-c7", email="c7@x.com")
    client.put("/conversations/c-j", json=_body(), headers=h)
    fake_repo[("sub-c7", "c-j")]["messages_json"] = "{not json"
    body = client.get("/conversations/c-j", headers=h).json()
    assert body["messages"] == []
