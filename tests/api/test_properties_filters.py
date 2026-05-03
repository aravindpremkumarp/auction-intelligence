"""Unit tests for `_properties_filter_cypher` — the helper that turns the
`/properties` query params into MATCH + WHERE + params for the listing /
count / facet queries.

Tested at the cypher-string seam rather than via Neo4j because the conftest
stub `run_query` does not understand AuctionProperty queries. These tests
prove the right edges are inserted into the MATCH clause and the right
params dict is built — Neo4j execution is downstream and out of scope.
"""
from __future__ import annotations

from api.main import _properties_filter_cypher


def test_property_type_filter_adds_match_and_param() -> None:
    """A `property_type` filter must add a HAS_PROPERTY_TYPE MATCH edge and
    bind the value as $f_property_type."""
    match, where, params = _properties_filter_cypher({"property_type": "Apartment"})

    assert "(a)-[:HAS_PROPERTY_TYPE]->(:PropertyType {name: $f_property_type})" in match
    assert params["f_property_type"] == "Apartment"


def test_property_type_and_asset_category_compose_with_AND() -> None:
    """Both filters must coexist as separate MATCH edges (Cypher MATCH list
    is implicitly AND), each bound to its own param. They must NOT collide."""
    match, _, params = _properties_filter_cypher(
        {"type": "Residential", "property_type": "Apartment"},
    )

    # Both edges present — independent dimensions, no overwrite.
    assert "HAS_ASSET_CATEGORY" in match
    assert "HAS_PROPERTY_TYPE" in match
    assert params["f_type"] == "Residential"
    assert params["f_property_type"] == "Apartment"


def test_property_type_absent_when_filter_unset() -> None:
    """Without the filter, neither the edge nor the param leak into the
    output — so unrelated queries don't pay for it."""
    match, _, params = _properties_filter_cypher({"type": "Residential"})

    assert "HAS_PROPERTY_TYPE" not in match
    assert "f_property_type" not in params


def test_q_searches_property_type_names() -> None:
    """Free-text search must match PropertyType node names too — the new
    dimension should be discoverable from the same search box that already
    finds banks, cities, areas, and asset categories."""
    _, where, params = _properties_filter_cypher({"q": "Plot"})

    assert "HAS_PROPERTY_TYPE" in where
    assert "PropertyType" in where
    assert params["f_q"] == "plot"


def test_property_type_compose_with_geographic_filters() -> None:
    """All-filter regression: property_type alongside state/district/village
    produces a fully-composed MATCH list with correct param bindings."""
    match, _, params = _properties_filter_cypher(
        {
            "state": "Tamil Nadu",
            "district": "Chennai",
            "village": "Adyar",
            "property_type": "Apartment",
        },
    )

    for edge in (
        "LOCATED_IN_STATE",
        "LOCATED_IN_CITY",
        "LOCATED_IN_AREA",
        "HAS_PROPERTY_TYPE",
    ):
        assert edge in match
    assert params == {
        "f_state": "Tamil Nadu",
        "f_district": "Chennai",
        "f_village": "Adyar",
        "f_property_type": "Apartment",
    }
