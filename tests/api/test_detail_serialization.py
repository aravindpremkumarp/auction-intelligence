"""Regression for the "Not found / Failed to fetch" property-detail outage.

`get_auction_detail` returns `properties(node)` maps that hold raw neo4j
temporal objects (neo4j.time.DateTime/Date/…). FastAPI's pydantic-v2 response
serializer cannot encode those, so a single un-coerced datetime — typically on
a *related* node (Bank/City/…) or a newly-added AuctionProperty field — made
the endpoint 500 for every property. The detail payload must therefore be
fully JSON-serializable, with every temporal value rendered as an ISO string.
"""
from __future__ import annotations

import json

from neo4j.time import Date, DateTime


def _patch_detail(monkeypatch, rows: list[dict]) -> None:
    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", lambda c, p=None: rows)
    monkeypatch.setattr(ct, "run_read_query",
                        lambda c, p=None, timeout=10.0, max_rows=200: rows)


def test_detail_payload_is_json_serializable_with_temporal_values(monkeypatch) -> None:
    _patch_detail(monkeypatch, [{
        "fields": {
            "auction_id": "ts",
            "title": "Plot 7",
            "auction_start_dt": DateTime(2026, 6, 7, 10, 0, 0),
            # A datetime field that was NOT in the old hardcoded key list —
            # on its own enough to 500 the endpoint before the fix.
            "first_seen_at": DateTime(2026, 1, 1, 0, 0, 0),
        },
        "relationships": {
            # The real-world trigger: a temporal property on a related node,
            # surfaced raw by properties(bank)/properties(city) and never
            # coerced by the old top-level-only logic.
            "bank": {"name": "SBI", "verified_at": DateTime(2026, 5, 1, 9, 0, 0)},
            "city": {"name": "Chennai", "ingested_on": Date(2025, 12, 31)},
            "property_types": ["Residential"],
        },
        "documents": [],
        "siblings": [],
    }])
    from api.tools.cypher_tools import get_auction_detail

    out = get_auction_detail("ts")
    assert out is not None

    # The crux: the whole payload must be JSON-encodable. A raw neo4j.time
    # object anywhere in here raises TypeError — exactly what produced the
    # production 500.
    json.dumps(out)

    assert out["fields"]["auction_start_dt"].startswith("2026-06-07T10:00:00")
    assert out["fields"]["first_seen_at"].startswith("2026-01-01T00:00:00")
    assert out["relationships"]["bank"]["verified_at"].startswith("2026-05-01T09:00:00")
    assert out["relationships"]["city"]["ingested_on"] == "2025-12-31"
    # Non-temporal values pass through untouched.
    assert out["relationships"]["bank"]["name"] == "SBI"
    assert out["relationships"]["property_types"] == ["Residential"]


def test_auction_detail_route_returns_200_with_related_node_datetime(monkeypatch) -> None:
    """End-to-end: GET /auction/{id} must come back 200 even when a related
    node carries a raw neo4j datetime. This drives the real FastAPI pydantic-v2
    response serializer — the layer that raised and turned every detail into a
    500 (surfaced in the UI as "Failed to fetch")."""
    import importlib
    ct = importlib.import_module("api.tools.cypher_tools")
    rows = [{
        "fields": {"auction_id": "e2e", "title": "Plot 7",
                   "auction_start_dt": DateTime(2026, 6, 7, 10, 0, 0)},
        "relationships": {"bank": {"name": "SBI",
                                   "verified_at": DateTime(2026, 5, 1, 9, 0, 0)}},
        "documents": [],
        "siblings": [],
    }]
    monkeypatch.setattr(ct, "run_query", lambda c, p=None: rows)
    monkeypatch.setattr(ct, "run_read_query",
                        lambda c, p=None, timeout=10.0, max_rows=200: rows)

    from fastapi.testclient import TestClient
    from api.main import app
    r = TestClient(app).get("/auction/e2e")

    assert r.status_code == 200
    body = r.json()
    assert body["auction_id"] == "e2e"
    assert body["relationships"]["bank"]["verified_at"].startswith("2026-05-01T09:00:00")
