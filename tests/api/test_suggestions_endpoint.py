"""Endpoint tests for GET /suggestions.

`search_auctions` is faked at the chat-router module level so we can pin the
HTTP contract — chip shape, the hourly cache, and graceful degradation on a
graph read failure — without a live graph. The chip assembly itself is covered
by test_suggestions.py.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

chat = importlib.import_module("api.chat.router")


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _dist(*pairs: tuple[str, int]) -> dict:
    return {"distribution": [{"value": v, "auction_count": n} for v, n in pairs]}


_FAKE_GRAPH = {
    "city": _dist(("Chennai", 412), ("Coimbatore", 88)),
    "property_type": _dist(("Flat", 230), ("Plot", 140)),
    "asset_category": _dist(("Residential", 500), ("Commercial", 88)),
    "area": _dist(("Ambattur", 14)),
}


@pytest.fixture(autouse=True)
def _clear_cache():
    """The suggestion cache is module-global; reset it around every test so
    ordering can't leak a cached set between cases."""
    chat._SUGGESTIONS_CACHE.clear()
    yield
    chat._SUGGESTIONS_CACHE.clear()


@pytest.fixture
def graph(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake search_auctions; route by the group_by dimension, count calls."""
    state: dict = {"calls": 0, "graph": dict(_FAKE_GRAPH)}

    def fake_search(*args, **kwargs):
        state["calls"] += 1
        return state["graph"].get(kwargs.get("group_by"), {"distribution": []})

    monkeypatch.setattr(chat.cypher_T, "search_auctions", fake_search)
    return state


def test_suggestions_shape_from_live_distributions(graph: dict):
    r = _client().get("/suggestions")
    assert r.status_code == 200
    chips = r.json()["suggestions"]
    assert chips == [
        {"label": "Auctions in Chennai", "q": "auctions in Chennai", "count": 412},
        {"label": "Flat listings", "q": "flat listings", "count": 230},
        {"label": "Residential properties", "q": "residential properties", "count": 500},
        {"label": "Plot listings", "q": "plot listings", "count": 140},
    ]


def test_second_call_is_served_from_cache(graph: dict):
    client = _client()
    client.get("/suggestions")
    first_calls = graph["calls"]
    assert first_calls > 0  # cold cache hit the graph once per dimension
    client.get("/suggestions")
    assert graph["calls"] == first_calls  # warm cache: no new graph reads


def test_graph_failure_degrades_to_empty(monkeypatch: pytest.MonkeyPatch):
    def boom(*args, **kwargs):
        raise RuntimeError("neo4j down")

    monkeypatch.setattr(chat.cypher_T, "search_auctions", boom)
    r = _client().get("/suggestions")
    assert r.status_code == 200
    # Cold cache + total outage → empty list; the web UI keeps its fallback chips.
    assert r.json()["suggestions"] == []


def test_stale_cache_survives_a_failed_refresh(graph: dict, monkeypatch: pytest.MonkeyPatch):
    client = _client()
    good = client.get("/suggestions").json()["suggestions"]
    assert good
    # Expire the cache, then make the next refresh fail: last good set should
    # still be served rather than blanking the landing.
    stamp, chips = chat._SUGGESTIONS_CACHE["default"]
    chat._SUGGESTIONS_CACHE["default"] = (stamp - chat._SUGGESTIONS_TTL_SECONDS - 1, chips)

    def boom(*args, **kwargs):
        raise RuntimeError("neo4j blip")

    monkeypatch.setattr(chat.cypher_T, "search_auctions", boom)
    r = client.get("/suggestions")
    assert r.json()["suggestions"] == good
