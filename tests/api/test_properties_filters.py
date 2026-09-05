"""Unit tests for `_properties_filter_cypher` — the helper that turns the
`/properties` query params into MATCH + WHERE + params for the listing /
count / facet queries.

Tested at the cypher-string seam rather than via Neo4j because the conftest
stub `run_query` does not understand AuctionProperty queries. These tests
prove the right edges are inserted into the MATCH clause and the right
params dict is built — Neo4j execution is downstream and out of scope.
"""
from __future__ import annotations

from api.properties.router import _facet_filters_for, _properties_filter_cypher


def test_property_type_filters_on_the_notice_bucket_not_the_portal_edge() -> None:
    """The portal dropdown is the value that is WRONG — 832 listings live
    disagree with their notice, 139 of them flats and houses filed under Land
    or Plot. Matching `HAS_PROPERTY_TYPE` is what puts a flat in a land
    search, so the filter runs on the notice-derived bucket instead."""
    match, where, params = _properties_filter_cypher({"property_type": "Flat"})

    assert "HAS_PROPERTY_TYPE" not in match
    assert "a.property_type_effective IN $f_property_type_buckets" in where
    assert params["f_property_type_buckets"] == ["flat"]


def test_a_land_search_also_matches_plots() -> None:
    """Someone filtering for land means bare ground; whether the notice called
    it a plot is not a distinction they asked for."""
    _, _, params = _properties_filter_cypher({"property_type": "Land"})
    assert params["f_property_type_buckets"] == ["land", "plot"]


def test_bucket_names_from_the_facet_are_accepted() -> None:
    """The facet now hands out bucket names, and whatever it returns must be
    valid to send back."""
    _, _, params = _properties_filter_cypher({"property_type": "house"})
    assert params["f_property_type_buckets"] == ["house"]


def test_portal_names_from_old_bookmarks_still_work() -> None:
    """Links made before this change carry the portal vocabulary. Refusing
    them would break every saved search for a rename nobody asked for."""
    _, _, params = _properties_filter_cypher({"property_type": "Land And Building"})
    assert params["f_property_type_buckets"] == ["house"]


def test_a_hand_typed_name_falls_back_to_the_notice_classifier() -> None:
    """"Apartment" is not one of the portal's 23 dropdown values, but it is
    plainly a flat. The keyword rules that read the notices resolve it rather
    than the filter returning nothing."""
    _, _, params = _properties_filter_cypher({"property_type": "Apartment"})
    assert params["f_property_type_buckets"] == ["flat"]


def test_an_unknown_type_matches_nothing_rather_than_everything() -> None:
    """Silently dropping the filter would report a wider result set as if it
    had been filtered."""
    _, where, params = _properties_filter_cypher({"property_type": "Spaceship"})
    assert "false" in where
    assert "f_property_type_buckets" not in params


def test_property_type_and_asset_category_compose_with_AND() -> None:
    """Both filters must coexist as separate MATCH edges (Cypher MATCH list
    is implicitly AND), each bound to its own param. They must NOT collide."""
    match, _, params = _properties_filter_cypher(
        {"type": "Residential", "property_type": "Flat"},
    )

    # Independent dimensions, no overwrite — asset category still rides an
    # edge, property type now rides the notice bucket.
    assert "HAS_ASSET_CATEGORY" in match
    assert params["f_type"] == "Residential"
    assert params["f_property_type_buckets"] == ["flat"]


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
    """All-filter regression: property_type and district alongside
    state/village keep the node-backed edges in the MATCH list while riding
    WHERE clauses themselves, with correct param bindings and no collision."""
    match, where, params = _properties_filter_cypher(
        {
            "state": "Tamil Nadu",
            "district": "Chennai",
            "village": "Adyar",
            "property_type": "Apartment",
        },
    )

    for edge in ("LOCATED_IN_STATE", "LOCATED_IN_AREA"):
        assert edge in match
    assert "HAS_PROPERTY_TYPE" not in match
    # District left the MATCH list for the same reason property type did: it
    # no longer resolves to a node.
    assert "LOCATED_IN_CITY" not in match
    assert "a.property_type_effective IN $f_property_type_buckets" in where
    assert "a.revenue_district" in where
    assert params == {
        "f_state": "Tamil Nadu",
        "f_district_list": ["Chennai"],
        "f_village": "Adyar",
        "f_property_type_buckets": ["flat"],
    }


def test_multi_value_property_type_unions_its_buckets() -> None:
    """A multi-select on this dimension resolves every chosen name to a bucket
    and OR's them in one IN-list — the same OR-within/AND-across semantics the
    node-backed dimensions get, minus the node."""
    _, where, params = _properties_filter_cypher(
        {"property_type": ["Apartment", "Villa"]},
    )

    assert "a.property_type_effective IN $f_property_type_buckets" in where
    assert params == {"f_property_type_buckets": ["flat", "house"]}


def test_multi_value_filter_emits_in_clause() -> None:
    """When a node-backed categorical filter is given a list with >1 values,
    the cypher switches to an aliased node + `IN` WHERE clause so the dimension
    is OR'd within while still AND-ing across other dimensions."""
    match, where, params = _properties_filter_cypher(
        {"type": ["Residential", "Commercial"]},
    )

    assert "HAS_ASSET_CATEGORY" in match
    assert "(s_type:AssetCategory)" in match
    assert "s_type.name IN $f_type_list" in where
    assert params == {"f_type_list": ["Residential", "Commercial"]}


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


# ── district: the notice's revenue district, not the portal's :City ──────────

def test_district_filters_on_the_notice_district_not_the_portal_city() -> None:
    """The portal City is the witness the notice contradicts. Matching the
    `LOCATED_IN_CITY` edge is what returns a listing under a district its own
    sale notice does not place it in."""
    match, where, params = _properties_filter_cypher({"district": "Chengalpattu"})

    assert "LOCATED_IN_CITY" not in match
    assert "a.revenue_district" in where
    assert params["f_district_list"] == ["Chengalpattu"]


def test_district_falls_back_to_the_portal_city() -> None:
    """A listing place resolution never reached has no revenue district, and
    must stay findable under the only name anything holds for it — otherwise
    the better source being silent removes the listing from the dropdown."""
    _, where, _ = _properties_filter_cypher({"district": "Chennai"})

    assert "coalesce(a.revenue_district" in where
    assert "LOCATED_IN_CITY" in where  # the fallback, read inside the WHERE


def test_multi_value_district_unions_in_one_in_list() -> None:
    """Same OR-within/AND-across semantics as the node-backed dimensions."""
    _, where, params = _properties_filter_cypher(
        {"district": ["Chennai", "Salem"]},
    )

    assert "IN $f_district_list" in where
    assert params["f_district_list"] == ["Chennai", "Salem"]


def test_free_text_search_reaches_the_notice_district() -> None:
    """Typing a district the portal spells differently — or does not carry —
    must still find the listings whose notice names it."""
    _, where, params = _properties_filter_cypher({"q": "chengalpattu"})

    assert "toLower(coalesce(a.revenue_district, '')) CONTAINS $f_q" in where
    assert params["f_q"] == "chengalpattu"
