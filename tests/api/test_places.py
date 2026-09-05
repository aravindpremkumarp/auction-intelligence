"""Tests for api/places.py — the notice-first place precedence.

The rule these pin is the same one `pipeline.property_taxonomy` already
holds for property type: the notice wins, the portal is the fallback and
never the override.
"""
from __future__ import annotations

from api.places import district_effective, suppress_portal_city


def test_the_notice_district_comes_first_in_the_expression() -> None:
    """`coalesce` order IS the precedence — reversed, the portal would win
    every listing that has both, which is the bug this exists to close."""
    expr = district_effective("a", "city")
    assert expr == "coalesce(a.revenue_district, city.name)"


def test_the_portal_side_can_be_read_without_an_extra_clause() -> None:
    """A bare WHERE has nowhere to hang an OPTIONAL MATCH, so the portal
    fallback has to be readable as an expression on its own."""
    expr = district_effective("a")
    assert expr.startswith("coalesce(a.revenue_district, [(a)-[:LOCATED_IN_CITY]->")
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
