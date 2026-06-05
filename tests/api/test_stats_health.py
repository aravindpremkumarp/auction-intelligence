"""
Tests for the health + data-freshness endpoints (api/health, api/properties).

With the conftest Neo4j stub, unknown queries return [] so the endpoints must
degrade gracefully to zeros/None rather than 500.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def test_health_ok() -> None:
    assert _client().get("/health").json() == {"status": "ok"}


def test_health_deep_shape() -> None:
    deep = _client().get("/health/deep").json()
    assert deep["status"] in {"ok", "degraded"}
    # Freshness + connectivity fields are always present, even on an empty graph.
    assert "auction_count" in deep
    assert "vector_index" in deep
    assert "last_enriched" in deep


def test_stats_empty_graph_degrades_gracefully() -> None:
    body = _client().get("/stats").json()
    assert body["total_auctions"] == 0
    assert body["upcoming_auctions"] == 0
    assert body["last_enriched"] is None
    assert body["generated_at"].endswith("Z")


def test_stats_reports_freshness(monkeypatch: pytest.MonkeyPatch) -> None:
    # importlib.import_module resolves via sys.modules, sidestepping the
    # package re-export that shadows the `router` submodule with the APIRouter
    # under plain attribute traversal (`import ... as` / monkeypatch strings).
    import importlib
    mod = importlib.import_module("api.properties.router")
    monkeypatch.setattr(
        mod, "run_query",
        lambda c, p=None: [
            {"total": 1234, "upcoming": 56, "last_enriched": "2026-06-01T00:00:00Z"}
        ],
    )
    body = _client().get("/stats").json()
    assert body["total_auctions"] == 1234
    assert body["upcoming_auctions"] == 56
    assert body["last_enriched"] == "2026-06-01T00:00:00Z"
