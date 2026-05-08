"""Unit tests for `_properties_filter_cypher` — the helper that turns the
`/properties` query params into MATCH + WHERE + params for the listing /
count / facet queries.

Tested at the cypher-string seam rather than via Neo4j because the conftest
stub `run_query` does not understand AuctionProperty queries. These tests
prove the right edges are inserted into the MATCH clause and the right
params dict is built — Neo4j execution is downstream and out of scope.
"""
from __future__ import annotations

from api.main import _facet_filters_for, _properties_filter_cypher


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


def test_multi_value_filter_emits_in_clause() -> None:
    """When a categorical filter is given a list with >1 values, the cypher
    switches to an aliased node + `IN` WHERE clause so the dimension is OR'd
    within while still AND-ing across other dimensions."""
    match, where, params = _properties_filter_cypher(
        {"property_type": ["Apartment", "Villa"]},
    )

    assert "HAS_PROPERTY_TYPE" in match
    assert "(s_property_type:PropertyType)" in match
    assert "s_property_type.name IN $f_property_type_list" in where
    assert params == {"f_property_type_list": ["Apartment", "Villa"]}


def test_single_element_list_keeps_inline_pattern() -> None:
    """A one-element list must produce the same cheap inline-equals pattern
    as the scalar form — the IN-list form is reserved for true multi-select."""
    match, _, params = _properties_filter_cypher({"bank": ["ICICI"]})

    assert "(a)-[:CONDUCTED_BY]->(:Bank {name: $f_bank})" in match
    assert params == {"f_bank": "ICICI"}


def test_multi_value_across_dimensions_compose_with_AND() -> None:
    """Multi-value filters across separate dimensions still AND together —
    each adds its own MATCH/WHERE without colliding on params or aliases."""
    match, where, params = _properties_filter_cypher(
        {"state": ["Tamil Nadu", "Karnataka"], "bank": ["ICICI", "HDFC"]},
    )

    assert "(s_state:State)" in match
    assert "(s_bank:Bank)" in match
    assert "s_state.name IN $f_state_list" in where
    assert "s_bank.name IN $f_bank_list" in where
    assert params == {
        "f_state_list": ["Tamil Nadu", "Karnataka"],
        "f_bank_list": ["ICICI", "HDFC"],
    }


def test_empty_list_filter_is_ignored() -> None:
    """An empty list (no values selected) must not add any MATCH/WHERE/param
    — same as if the filter weren't passed at all."""
    match, where, params = _properties_filter_cypher({"state": [], "bank": []})

    assert "LOCATED_IN_STATE" not in match
    assert "CONDUCTED_BY" not in match
    assert where == ""
    assert params == {}


def test_facet_filters_for_drops_own_dimension() -> None:
    """A non-cascade facet must drop its own filter so the panel keeps
    showing all values the user could add — without this, picking one bank
    would shrink the bank dropdown to just that bank."""
    filters = {"bank": ["ICICI"], "state": ["Tamil Nadu"]}

    assert _facet_filters_for(filters, "bank") == {"state": ["Tamil Nadu"]}
    # Other dimensions still narrow the bank facet so counts reflect the
    # user's other selections — only the bank dim itself is removed.


def test_facet_filters_for_state_drops_cascade_descendants() -> None:
    """The state facet must drop state, district, and village so picking a
    district doesn't strand the user with only that district's state in the
    state dropdown."""
    filters = {
        "state":    ["Tamil Nadu"],
        "district": ["Chennai"],
        "village":  ["Adyar"],
        "bank":     ["ICICI"],
    }

    assert _facet_filters_for(filters, "state") == {"bank": ["ICICI"]}


def test_facet_filters_for_district_drops_self_and_village_only() -> None:
    """The district facet keeps state (so districts still narrow to the
    chosen state) but drops district and village."""
    filters = {
        "state":    ["Tamil Nadu"],
        "district": ["Chennai"],
        "village":  ["Adyar"],
    }

    assert _facet_filters_for(filters, "district") == {"state": ["Tamil Nadu"]}


def test_facet_filters_for_village_drops_only_self() -> None:
    """Village is the leaf of the geographic cascade — dropping just its own
    filter is enough."""
    filters = {
        "state":    ["Tamil Nadu"],
        "district": ["Chennai"],
        "village":  ["Adyar"],
    }

    assert _facet_filters_for(filters, "village") == {
        "state":    ["Tamil Nadu"],
        "district": ["Chennai"],
    }
