"""Endpoint tests for /watchlist (auth gating + router behaviour).

The repository layer is faked in-memory — these tests pin the router
contract: status codes, 404 on unknown auctions, and per-user isolation.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


@pytest.fixture
def fake_repo(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Replace api.watchlist.repository functions with an in-memory store."""
    from api.watchlist import repository as repo

    state = {
        "auctions": {"a-1", "a-2"},          # auctions that "exist" in the graph
        "saved": {},                          # supabase_id -> list[str]
    }

    def list_saved_auction_ids(sub: str) -> list[str]:
        return list(state["saved"].get(sub, []))

    def add_saved(sub: str, aid: str) -> bool:
        if aid not in state["auctions"]:
            return False
        ids = state["saved"].setdefault(sub, [])
        if aid not in ids:
            ids.insert(0, aid)
        return True

    def remove_saved(sub: str, aid: str) -> None:
        ids = state["saved"].get(sub, [])
        if aid in ids:
            ids.remove(aid)

    monkeypatch.setattr(repo, "list_saved_auction_ids", list_saved_auction_ids)
    monkeypatch.setattr(repo, "add_saved", add_saved)
    monkeypatch.setattr(repo, "remove_saved", remove_saved)
    return state


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def test_watchlist_requires_auth(fake_repo: dict) -> None:
    client = _client()
    assert client.get("/watchlist").status_code == 401
    assert client.post("/watchlist/a-1").status_code == 401
    assert client.delete("/watchlist/a-1").status_code == 401


def test_watchlist_save_list_remove(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="sub-w1", email="w1@x.com")

    assert client.get("/watchlist", headers=h).json() == {"ids": []}

    assert client.post("/watchlist/a-1", headers=h).status_code == 204
    assert client.post("/watchlist/a-2", headers=h).status_code == 204
    # Saving twice is idempotent.
    assert client.post("/watchlist/a-1", headers=h).status_code == 204
    assert client.get("/watchlist", headers=h).json() == {"ids": ["a-2", "a-1"]}

    assert client.delete("/watchlist/a-2", headers=h).status_code == 204
    assert client.get("/watchlist", headers=h).json() == {"ids": ["a-1"]}
    # Removing an id that isn't saved is a no-op, still 204.
    assert client.delete("/watchlist/a-2", headers=h).status_code == 204


def test_watchlist_unknown_auction_404(fake_repo: dict) -> None:
    client = _client()
    h = auth_header(sub="sub-w2", email="w2@x.com")
    assert client.post("/watchlist/nope", headers=h).status_code == 404


def test_watchlist_is_per_user(fake_repo: dict) -> None:
    client = _client()
    h1 = auth_header(sub="sub-w3", email="w3@x.com")
    h2 = auth_header(sub="sub-w4", email="w4@x.com")
    client.post("/watchlist/a-1", headers=h1)
    assert client.get("/watchlist", headers=h1).json() == {"ids": ["a-1"]}
    assert client.get("/watchlist", headers=h2).json() == {"ids": []}
