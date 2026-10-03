"""Tests for api/places.py — the notice-first place precedence.

The rule these pin is the same one `pipeline.property_taxonomy` already
holds for property type: the notice wins, the portal is the fallback and
never the override.
"""
from __future__ import annotations

from api.places import district_effective, in_service_area, suppress_portal_city


def test_the_notice_district_comes_first_in_the_expression() -> None:
    """`coalesce` order IS the precedence — reversed, the portal would win
    every listing that has both, which is the bug this exists to close."""
    expr = district_effective("a", "city")
    assert expr == "coalesce(a.revenue_district, a.portal_district, city.name)"


def test_the_portal_side_can_be_read_without_an_extra_clause() -> None:
    """A bare WHERE has nowhere to hang an OPTIONAL MATCH, so the portal
    fallback has to be readable as an expression on its own."""
    expr = district_effective("a")
    assert expr.startswith("coalesce(a.revenue_district, a.portal_district, [(a)-[:LOCATED_IN_CITY]->")
    assert expr.endswith("][0])")


def test_two_uses_in_one_clause_can_avoid_declaring_the_same_variable() -> None:
    """Cypher rejects a second declaration of a name already bound in the
    clause, so a caller using this twice side by side needs distinct ones."""
    a = district_effective("a", var="_x")
    b = district_effective("a", var="_y")
    assert "_x" in a and "_y" in b and a != b


def test_a_row_with_both_places_loses_the_portal_one() -> None:
    row = suppress_portal_city({"city": "Chennai", "district": "Chengalpattu"})
    assert row == {"city": None, "district": "Chengalpattu"}


def test_a_row_the_notice_never_placed_keeps_its_portal_city() -> None:
    row = suppress_portal_city({"city": "Chennai", "district": None})
    assert row["city"] == "Chennai"


def test_an_empty_district_is_not_a_district() -> None:
    """A blank string is the absence of an answer, not a better one."""
    row = suppress_portal_city({"city": "Chennai", "district": ""})
    assert row["city"] == "Chennai"


def test_the_lot_a_listing_is_decides_its_service_area() -> None:
    """The lot carries the notice's own state, so it outranks the listing's
    flag; the listing's flag is read only when no IS_LOT names a lot."""
    expr = in_service_area("p")
    assert "MATCH (p)-[:IS_LOT]->(_oa:Lot) WHERE _oa.out_of_area = true" in expr
    assert "coalesce(p.out_of_area, false) AND NOT EXISTS { MATCH (p)-[:IS_LOT]->(:Lot) }" in expr
    assert expr.startswith("NOT (")


def test_every_buyer_search_applies_the_service_area() -> None:
    """Hiding is a read-side choice, so every path a buyer searches through
    has to carry it — one missed path and the listing is back."""
    import importlib
    import inspect

    import api.agent3.benchmark_price as bench
    import api.agent3.find_properties as fp
    import api.agent3.identifiers as ids
    import api.agent3.search_notices as sn
    import api.tools.cypher_tools as legacy

    # the package re-exports `router`, which shadows the module attribute
    browse = importlib.import_module("api.properties.router")
    assert 'in_service_area("a")' in inspect.getsource(browse._properties_filter_cypher)
    assert 'in_service_area("a")' in inspect.getsource(fp._Query.base)
    assert in_service_area("a") in sn._LOT_CYPHER and in_service_area("a") in sn._LISTING_CYPHER
    assert ids._RESOLVE_CYPHER.count(in_service_area("a")) == 2
    assert ids._DETAIL_CYPHER.count(in_service_area("a")) == 2
    assert in_service_area("a") in bench._RING.format(match="")
    assert 'where = [in_service_area("a")]' in inspect.getsource(legacy.search_auctions)
    assert 'where = [in_service_area("p")]' in inspect.getsource(legacy.semantic_search)
