"""Verify the Cypher produced by search_auctions / price_comparison /
get_auction_detail uses the new per-auction PropertyType edge and the new
`area` filter — the fix for feedback items 19224426, 5fcd2638, 72a75404,
3ba48edd (shared-taxonomy bug + missing area filter).
"""
from __future__ import annotations

import sys


def _patch_run_query(monkeypatch):
    """Capture every (cypher, params) call to api.tools.cypher_tools.run_query.

    Returns a list that each call appends to.
    """
    calls: list[tuple[str, dict]] = []

    def fake_run_query(cypher: str, params: dict | None = None):
        calls.append((cypher, dict(params or {})))
        return []

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_query", fake_run_query)
    return calls


def test_search_auctions_property_type_uses_direct_edge(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(property_type="Flat", city="Chennai", limit=0)

    assert calls, "run_query should have been invoked"
    cypher, params = calls[0]
    # New per-auction edge must be present
    assert "(a)-[:OF_PROPERTY_TYPE]->(pt:PropertyType {name: $property_type})" in cypher
    # Old broken shared-taxonomy path must be gone
    assert "HAS_TYPE" not in cypher
    assert params["property_type"] == "Flat"
    assert params["city"] == "Chennai"


def test_search_auctions_area_filter(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", area="ambattur", limit=0)

    cypher, params = calls[0]
    assert "(a)-[:LOCATED_IN_AREA]->(ar:Area)" in cypher
    assert "toLower(ar.name) CONTAINS toLower($area)" in cypher
    assert params["area"] == "ambattur"


def test_search_auctions_row_fetch_projection(monkeypatch) -> None:
    """When limit>0 the detail query is issued with the new OF_PROPERTY_TYPE
    OPTIONAL MATCH (replacing the old HAS_TYPE fan-out)."""
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Chennai", limit=5)

    # Two calls expected: aggregate + row fetch. Row fetch is the one with LIMIT.
    row_cypher = next(c for c, _ in calls if "LIMIT $limit" in c)
    assert "OPTIONAL MATCH (a)-[:OF_PROPERTY_TYPE]->(pt:PropertyType)" in row_cypher
    assert "HAS_TYPE" not in row_cypher


def test_price_comparison_uses_direct_edge(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import price_comparison

    price_comparison(city="Chennai", property_type="Flat")

    cypher, params = calls[0]
    assert "(a)-[:OF_PROPERTY_TYPE]->(:PropertyType {name: $property_type})" in cypher
    assert "HAS_TYPE" not in cypher
    assert params == {"city": "Chennai", "property_type": "Flat"}


def test_get_auction_detail_uses_direct_edge(monkeypatch) -> None:
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import get_auction_detail

    get_auction_detail("717410")

    cypher, _ = calls[0]
    assert "OPTIONAL MATCH (a)-[:OF_PROPERTY_TYPE]->(pt:PropertyType)" in cypher
    assert "(ac)-[:HAS_TYPE]->(pt:PropertyType)" not in cypher
