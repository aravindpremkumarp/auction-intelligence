"""Tests for list_distinct — verifies the Cypher shape and parameter wiring
for the enum-discovery tool. The underlying Neo4j call is stubbed."""
from __future__ import annotations

import pytest


def _patch_read_query(monkeypatch, response=None):
    calls: list[tuple[str, dict, dict]] = []
    response = response or [{"value": "Chennai", "auction_count": 1800}]

    def fake_run_read_query(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {}), {"timeout": timeout, "max_rows": max_rows}))
        return response

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake_run_read_query)
    return calls


def test_list_distinct_city(monkeypatch):
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    out = list_distinct("city", limit=50)
    assert out["field"] == "city"
    assert out["filter_city"] is None
    assert out["results"] == [{"value": "Chennai", "auction_count": 1800}]

    cypher, params, config = calls[0]
    assert "(n:City)" in cypher
    assert "LOCATED_IN_CITY" in cypher
    assert params == {"limit": 50}
    assert config["max_rows"] == 50


def test_list_distinct_bank_with_city_filter(monkeypatch):
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    list_distinct("bank", city="Chennai", limit=20)

    cypher, params, _ = calls[0]
    assert "(n:Bank)" in cypher
    assert "LOCATED_IN_CITY" in cypher
    assert "CONDUCTED_BY" in cypher
    assert params == {"limit": 20, "city": "Chennai"}


def test_list_distinct_city_ignores_city_filter(monkeypatch):
    """Filtering city by city doesn't make sense; the tool drops the filter."""
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    list_distinct("city", city="Chennai")
    _, params, _ = calls[0]
    assert "city" not in params


def test_list_distinct_rejects_unknown_field(monkeypatch):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    with pytest.raises(ValueError, match="field must be one of"):
        list_distinct("random_field")


@pytest.mark.parametrize("field", ["city", "area", "state", "bank", "borrower",
                                   "asset_category", "property_type"])
def test_list_distinct_accepts_all_whitelisted_fields(monkeypatch, field):
    _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    out = list_distinct(field)
    assert out["field"] == field


def test_list_distinct_property_type_scoped_by_bank(monkeypatch):
    """Regression for feedback df329942: 'spread of property types for SBI'
    must produce a bank-scoped breakdown in a single call, not iterate
    auctions one by one."""
    calls = _patch_read_query(
        monkeypatch,
        response=[
            {"value": "Land", "auction_count": 40},
            {"value": "Flat", "auction_count": 30},
        ],
    )
    from api.tools.cypher_tools import list_distinct

    out = list_distinct("property_type", bank="State Bank of India", limit=50)

    cypher, params, _ = calls[0]
    # The scope-match lives on AuctionProperty, NOT on Bank — guards
    # against the agent's previous mistake of chaining
    # (Bank)-[:HAS_PROPERTY_TYPE].
    assert "(a)-[:CONDUCTED_BY]->(:Bank {name: $bank})" in cypher
    assert "(a)-[:HAS_PROPERTY_TYPE]->(n:PropertyType)" in cypher
    assert params == {"limit": 50, "bank": "State Bank of India"}
    assert out["filter_bank"] == "State Bank of India"
    assert out["results"][0]["auction_count"] == 40


def test_list_distinct_combines_multiple_scopes(monkeypatch):
    """Residential property types in Kanchipuram — city + asset_category
    filters must both appear in the MATCH clause."""
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    list_distinct(
        "property_type",
        city="Kanchipuram",
        asset_category="Residential",
    )

    cypher, params, _ = calls[0]
    assert "LOCATED_IN_CITY" in cypher
    assert "HAS_ASSET_CATEGORY" in cypher
    assert "HAS_PROPERTY_TYPE" in cypher
    assert params["city"] == "Kanchipuram"
    assert params["asset_category"] == "Residential"


def test_list_distinct_drops_self_scope(monkeypatch):
    """Filtering by the same dimension you're grouping on is a no-op;
    the tool drops it silently rather than raising."""
    calls = _patch_read_query(monkeypatch)
    from api.tools.cypher_tools import list_distinct

    list_distinct("bank", bank="State Bank of India")
    _, params, _ = calls[0]
    assert "bank" not in params
