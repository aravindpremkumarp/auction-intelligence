"""End-to-end tests for the /feedback endpoints using the in-memory stub."""
from __future__ import annotations

import os

from fastapi.testclient import TestClient


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


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

    r2 = client.get("/feedback/recent")
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


def test_resolve_requires_token() -> None:
    client = _client()
    fid = client.post("/feedback", json=_payload()).json()["id"]

    # missing header
    r = client.patch(f"/feedback/{fid}/resolve")
    assert r.status_code == 422  # FastAPI validation error for required header

    # wrong token
    os.environ["FEEDBACK_RESOLVE_TOKEN"] = "correct-token"
    r = client.patch(f"/feedback/{fid}/resolve", headers={"X-Resolve-Token": "nope"})
    assert r.status_code == 401

    # right token resolves
    r = client.patch(f"/feedback/{fid}/resolve", headers={"X-Resolve-Token": "correct-token"})
    assert r.status_code == 200
    assert r.json()["resolved"] is True

    # unresolved_only now excludes it
    remaining = client.get("/feedback/recent?unresolved_only=true").json()
    assert all(item["id"] != fid for item in remaining)


def test_rating_filter() -> None:
    client = _client()
    client.post("/feedback", json=_payload(rating="up", text=None))
    client.post("/feedback", json=_payload(rating="down", text="bad"))
    ups = client.get("/feedback/recent?rating=up").json()
    assert all(i["rating"] == "up" for i in ups)
