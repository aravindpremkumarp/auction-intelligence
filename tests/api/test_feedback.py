"""End-to-end tests for the /feedback endpoints using the in-memory stub."""
from __future__ import annotations

import os

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _recent_headers() -> dict:
    """Reads of /feedback/recent need the shared token (or an admin JWT)."""
    os.environ.setdefault("FEEDBACK_RESOLVE_TOKEN", "test-resolve-token")
    return {"X-Resolve-Token": os.environ["FEEDBACK_RESOLVE_TOKEN"]}


def _payload(rating: str = "down", text: str | None = "wrong price") -> dict:
    return {
        "rating": rating,
        "text": text,
        "session_id": "sess-1",
        "message_index": 2,
        "question": "price in Chennai?",
        "answer": "about 25 lakh",
        "artifacts": [{"tool": "search_auctions", "args": {"city": "Chennai"}, "result": [1, 2, 3]}],
        "context_turns": [
            {"role": "user", "content": "show me flats in Chennai"},
            {"role": "assistant", "content": "Found 12...", "tool_calls": [
                {"tool": "search_auctions", "args": {"city": "Chennai", "property_type": "flat"}, "result": "DROPME"},
            ]},
            {"role": "user", "content": "price in Chennai?"},
        ],
        "user_agent": "pytest",
    }


def test_submit_and_list_feedback() -> None:
    client = _client()
    r = client.post("/feedback", json=_payload())
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "saved"
    fid = body["id"]

    r2 = client.get("/feedback/recent", headers=_recent_headers())
    assert r2.status_code == 200
    items = r2.json()
    assert len(items) >= 1
    assert items[0]["id"] == fid
    assert items[0]["rating"] == "down"
    assert items[0]["resolved"] is False
    # artifacts were stripped to tool + args only
    assert items[0]["artifacts"] == [{"tool": "search_auctions", "args": {"city": "Chennai"}}]
    # context_turns round-trip, and assistant tool_calls stripped of `result`
    ct = items[0]["context_turns"]
    assert [t["role"] for t in ct] == ["user", "assistant", "user"]
    assert ct[1]["tool_calls"] == [
        {"tool": "search_auctions", "args": {"city": "Chennai", "property_type": "flat"}}
    ]


def test_resolve_requires_token_or_admin() -> None:
    client = _client()
    fid = client.post("/feedback", json=_payload()).json()["id"]

    # No token and no JWT → 401 (was 422 before we made the header optional).
    r = client.patch(f"/feedback/{fid}/resolve")
    assert r.status_code == 401

    # Wrong token, no JWT → 401
    os.environ["FEEDBACK_RESOLVE_TOKEN"] = "correct-token"
    r = client.patch(f"/feedback/{fid}/resolve", headers={"X-Resolve-Token": "nope"})
    assert r.status_code == 401

    # Right token resolves
    r = client.patch(f"/feedback/{fid}/resolve", headers={"X-Resolve-Token": "correct-token"})
    assert r.status_code == 200
    assert r.json()["resolved"] is True

    # unresolved_only now excludes it
    remaining = client.get("/feedback/recent?unresolved_only=true", headers=_recent_headers()).json()
    assert all(item["id"] != fid for item in remaining)


def test_resolve_with_admin_jwt() -> None:
    from api import neo4j_client
    from tests.api.conftest import auth_header
    # Clean slate so the only feedback row is the one we seed here.
    neo4j_client._users.clear()       # type: ignore[attr-defined]
    neo4j_client._feedback.clear()    # type: ignore[attr-defined]

    client = _client()
    fid = client.post("/feedback", json=_payload()).json()["id"]

    # Sign in as a non-admin user and try to resolve → 401 (admin role
    # required since no token is supplied).
    h_user = auth_header(sub="sub-user", email="user@x.com")
    client.get("/auth/me", headers=h_user)  # materialise profile
    r = client.patch(f"/feedback/{fid}/resolve", headers=h_user)
    assert r.status_code == 401

    # Promote the user to admin and retry → 200.
    neo4j_client._users["sub-user"]["role"] = "admin"  # type: ignore[attr-defined]
    r = client.patch(f"/feedback/{fid}/resolve", headers=h_user)
    assert r.status_code == 200
    assert r.json()["resolved"] is True


def test_admin_feedback_list_requires_admin() -> None:
    from api import neo4j_client
    from tests.api.conftest import auth_header
    neo4j_client._users.clear()       # type: ignore[attr-defined]
    neo4j_client._feedback.clear()    # type: ignore[attr-defined]

    client = _client()
    fid = client.post("/feedback", json=_payload()).json()["id"]

    # Anonymous → 401
    assert client.get("/admin/feedback").status_code == 401

    # Authenticated non-admin → 403
    h_user = auth_header(sub="sub-u2", email="u2@x.com")
    client.get("/auth/me", headers=h_user)
    assert client.get("/admin/feedback", headers=h_user).status_code == 403

    # Admin → 200 and sees the feedback item
    neo4j_client._users["sub-u2"]["role"] = "admin"  # type: ignore[attr-defined]
    r = client.get("/admin/feedback", headers=h_user)
    assert r.status_code == 200
    items = r.json()
    assert any(i["id"] == fid for i in items)


def test_rating_filter() -> None:
    client = _client()
    client.post("/feedback", json=_payload(rating="up", text=None))
    client.post("/feedback", json=_payload(rating="down", text="bad"))
    ups = client.get("/feedback/recent?rating=up", headers=_recent_headers()).json()
    assert all(i["rating"] == "up" for i in ups)


def test_general_feedback_without_rating() -> None:
    client = _client()
    r = client.post("/feedback", json={
        "kind": "general",
        "text": "the suggestions row could use more variety",
        "session_id": "sess-g1",
        "user_agent": "pytest",
        "page_url": "https://example.com/",
    })
    assert r.status_code == 200
    fid = r.json()["id"]

    items = client.get("/feedback/recent?kind=general", headers=_recent_headers()).json()
    assert any(i["id"] == fid for i in items)
    rec = next(i for i in items if i["id"] == fid)
    assert rec["kind"] == "general"
    assert rec["rating"] is None
    assert rec["message_index"] == -1
    assert rec["page_url"] == "https://example.com/"

    # kind=message filter excludes it
    msgs = client.get("/feedback/recent?kind=message", headers=_recent_headers()).json()
    assert all(i["id"] != fid for i in msgs)


def test_feedback_recent_requires_credentials() -> None:
    """Records carry users' chat context, so anonymous reads are rejected."""
    from api import neo4j_client
    from tests.api.conftest import auth_header
    neo4j_client._users.clear()       # type: ignore[attr-defined]

    client = _client()
    client.post("/feedback", json=_payload())

    # Anonymous → 401; wrong token → 401.
    assert client.get("/feedback/recent").status_code == 401
    os.environ["FEEDBACK_RESOLVE_TOKEN"] = "correct-token"
    r = client.get("/feedback/recent", headers={"X-Resolve-Token": "nope"})
    assert r.status_code == 401

    # Authenticated non-admin → 401.
    h_user = auth_header(sub="sub-fr", email="fr@x.com")
    client.get("/auth/me", headers=h_user)
    assert client.get("/feedback/recent", headers=h_user).status_code == 401

    # Admin JWT → 200, shared token → 200.
    neo4j_client._users["sub-fr"]["role"] = "admin"  # type: ignore[attr-defined]
    assert client.get("/feedback/recent", headers=h_user).status_code == 200
    r = client.get("/feedback/recent", headers={"X-Resolve-Token": "correct-token"})
    assert r.status_code == 200


def test_general_feedback_requires_rating_or_text() -> None:
    client = _client()
    r = client.post("/feedback", json={
        "kind": "general",
        "session_id": "sess-g2",
    })
    assert r.status_code == 400


def test_resolved_at_populated_after_resolve() -> None:
    """Fix 3 (minimal): FeedbackRecord surfaces `resolved_at` so the
    15-min sync writes it into feedback/all.json and we can see when
    each item was closed."""
    client = _client()
    fid = client.post("/feedback", json=_payload()).json()["id"]

    # Before resolve, resolved_at is None.
    items = client.get("/feedback/recent?unresolved_only=false", headers=_recent_headers()).json()
    rec = next(i for i in items if i["id"] == fid)
    assert rec["resolved"] is False
    assert rec["resolved_at"] is None

    # After resolve, resolved_at is a non-empty string.
    os.environ["FEEDBACK_RESOLVE_TOKEN"] = "correct-token-2"
    r = client.patch(f"/feedback/{fid}/resolve", headers={"X-Resolve-Token": "correct-token-2"})
    assert r.status_code == 200

    items = client.get("/feedback/recent?unresolved_only=false", headers=_recent_headers()).json()
    rec = next(i for i in items if i["id"] == fid)
    assert rec["resolved"] is True
    assert isinstance(rec["resolved_at"], str) and rec["resolved_at"]
