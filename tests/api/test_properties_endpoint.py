"""Endpoint tests for GET /properties and GET /auction/{id}.

`run_query` is faked at the router-module level so we can pin the HTTP
contract — response shape, limit clamping, filter params, re-auction
derivation, and error codes — without a live graph. The filter/facet
Cypher builders themselves are covered by test_properties_filters.py.
"""
from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient

# `api.properties.__init__` re-exports an APIRouter as `router`, shadowing the
# module attribute — importlib gets the real module to patch.
props = importlib.import_module("api.properties.router")


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _row(auction_id: str = "a-1", reauction_count: int = 0) -> dict:
    return {
        "auction_id": auction_id, "title": "Flat in Chennai", "url": "http://x",
        "reserve_price": 2500000.0, "emd": 250000.0,
        "auction_start": "2026-07-01T10:00:00Z",
        "state": "Tamil Nadu", "city": "Chennai", "area": "Adyar",
        "bank": "SBI", "asset_category": "Residential",
        "property_types": ["Flat"],
        "previous_reserve_price": 3000000.0 if reauction_count else None,
        "reauction_count": reauction_count,
    }


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Fake the router's run_query; route by query text, capture params."""
    state: dict = {"rows": [_row()], "total": 1, "queries": []}

    def fake_run_query(cypher: str, params: dict | None = None) -> list[dict]:
        state["queries"].append((cypher, dict(params or {})))
        if "count(DISTINCT a) AS total" in cypher:
            return [{"total": state["total"]}]
        if "RETURN a.auction_id AS auction_id" in cypher:
            return [dict(r) for r in state["rows"]]
        if "AS name" in cypher:  # facet queries
            return [{"name": "Residential", "count": 1}]
        return []

    monkeypatch.setattr(props, "run_query", fake_run_query)
    return state


def test_properties_response_shape(captured: dict) -> None:
    r = _client().get("/properties")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["limit"] == 60 and body["offset"] == 0
    assert [row["auction_id"] for row in body["results"]] == ["a-1"]
    assert set(body["facets"]) == {"type", "property_type", "bank", "state", "district", "village"}
    # Non-reauction rows get the derived flags.
    assert body["results"][0]["is_reauction"] is False
    assert body["results"][0]["reauction_count"] == 0


def test_properties_reauction_derivation(captured: dict) -> None:
    captured["rows"] = [_row(reauction_count=2)]
    row = _client().get("/properties").json()["results"][0]
    assert row["is_reauction"] is True
    assert row["previous_reserve_price"] == 3000000.0


def test_properties_limit_clamped_and_offset_floored(captured: dict) -> None:
    body = _client().get("/properties?limit=9999&offset=-5").json()
    assert body["limit"] == 200  # _PROPERTIES_MAX_LIMIT
    assert body["offset"] == 0
    results_params = next(p for c, p in captured["queries"] if "SKIP $offset" in c)
    assert results_params["limit"] == 200 and results_params["offset"] == 0


def test_properties_filters_become_params(captured: dict) -> None:
    _client().get("/properties?district=Chennai&min_price=1000000&q=adyar")
    count_params = next(p for c, p in captured["queries"] if "count(DISTINCT a)" in c)
    # District rides a WHERE clause on the notice-first value, not a :City
    # edge in the MATCH — so its binding is the multi-select list form even
    # for one value.
    assert count_params["f_district_list"] == ["Chennai"]
    assert count_params["f_min_price"] == 1000000.0
    assert count_params["f_q"] == "adyar"


def test_properties_bad_sort_400(captured: dict) -> None:
    assert _client().get("/properties?sort=banana").status_code == 400


def test_properties_upcoming_sort(captured: dict) -> None:
    """`upcoming` (the browse-grid default) buckets live auctions before
    ended ones: bucket CASE first, soonest-upcoming ascending, then ended
    rows most-recently-ended first."""
    assert _client().get("/properties?sort=upcoming").status_code == 200
    results_cypher = next(c for c, p in captured["queries"] if "SKIP $offset" in c)
    order_by = results_cypher.split("ORDER BY", 1)[1]
    assert "WHEN a.auction_start_dt < datetime() THEN 1 ELSE 0 END ASC" in order_by
    assert "CASE WHEN a.auction_start_dt >= datetime() THEN a.auction_start_dt END ASC" in order_by
    assert "a.auction_start_dt DESC" in order_by


def test_properties_bad_date_400(captured: dict) -> None:
    assert _client().get("/properties?date_from=not-a-date").status_code == 400


def test_auction_detail_found_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    detail = {"auction_id": "a-9", "title": "Plot"}
    monkeypatch.setattr(props, "get_auction_detail",
                        lambda aid: detail if aid == "a-9" else None)
    client = _client()
    assert client.get("/auction/a-9").json() == detail
    assert client.get("/auction/nope").status_code == 404
