"""Tests for search_auctions(group_by=...) — the distribution/breakdown path
that replaced the standalone list_distinct tool. Verifies the Cypher shape,
that the full filter set composes with the grouping, and that the row fetch
is skipped. The underlying Neo4j call is stubbed."""
from __future__ import annotations

import pytest


def _patch_run_query(monkeypatch, *, total_count: int = 10, dist_rows=None):
    """Stub run_read_query. First call is the count aggregate; later calls
    (distribution / diagnostic) return `dist_rows`."""
    dist_rows = dist_rows if dist_rows is not None else [
        {"value": "Chennai", "auction_count": 1800}
    ]
    calls: list[tuple[str, dict]] = []
    state = {"call": 0}

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {})))
        state["call"] += 1
        if state["call"] == 1:
            return [{"total_count": total_count}]
        return dist_rows

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake)
    return calls


def test_group_by_city_walks_edge_and_skips_rows(monkeypatch):
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(group_by="city")

    assert out["group_by"] == "city"
    assert out["distribution"] == [{"value": "Chennai", "auction_count": 1800}]
    assert out["results"] == []  # rows skipped — buckets are the answer
    dist_cypher, _ = calls[1]
    assert "(a)-[:LOCATED_IN_CITY]->(g:City)" in dist_cypher
    assert "g.name AS value" in dist_cypher
    assert "count(DISTINCT a) AS auction_count" in dist_cypher
    assert len(calls) == 2  # count + distribution, no row fetch


def test_group_by_composes_with_full_filter_set(monkeypatch):
    """The capability gain over old list_distinct: price/EMD/date filters
    scope the distribution. 'Bank mix under 30 lakhs' in one call."""
    calls = _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    search_auctions(group_by="bank", max_price=3_000_000, city="Chennai")

    dist_cypher, dist_params = calls[1]
    assert "(a)-[:CONDUCTED_BY]->(g:Bank)" in dist_cypher
    assert "a.reserve_price_num <= $max_price" in dist_cypher
    assert "c.name IN $city" in dist_cypher
    assert dist_params["max_price"] == 3_000_000
    assert dist_params["city"] == ["Chennai"]


def test_group_by_service_provider_groups_off_node_property(monkeypatch):
    """service_provider is an AuctionProperty property, not a reference
    node: grouped off a.service_provider, nulls skipped, no edge walk."""
    calls = _patch_run_query(
        monkeypatch, dist_rows=[{"value": "BAANKNET", "auction_count": 893}]
    )
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(group_by="service_provider")

    assert out["distribution"][0]["value"] == "BAANKNET"
    dist_cypher, _ = calls[1]
    assert "a.service_provider AS value" in dist_cypher
    assert "a.service_provider IS NOT NULL" in dist_cypher
    assert "(g:" not in dist_cypher


def test_group_by_rejects_unknown_dimension(monkeypatch):
    _patch_run_query(monkeypatch)
    from api.tools.cypher_tools import search_auctions

    with pytest.raises(ValueError, match="group_by"):
        search_auctions(group_by="nope")


def test_group_by_zero_matches_skips_distribution_query(monkeypatch):
    """total_count == 0 → empty distribution without spending the group
    query; the zero-result diagnostic still runs (default future floor)."""
    calls = _patch_run_query(monkeypatch, total_count=0, dist_rows=[])
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(group_by="bank", city="Nowhere")

    assert out["distribution"] == []
    assert out["total_count"] == 0
    # count + zero-diagnostic only; no distribution query ran.
    assert not any("auction_count" in c for c, _ in calls)
