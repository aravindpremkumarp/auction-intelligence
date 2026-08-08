"""Unit tests for pipeline/measures.py (areas, lengths, road widths).

The conversion cases here are drawn from real extracted values — the corpus has
83 cent, 56 acre, 20 are, 11 hectare and 28 sq.m areas, 18 with unicode
fractions, plus 7 boundary "measurements" that were actually areas.
"""
from __future__ import annotations

import pytest

import pipeline.measures as M


# ── the substring trap ───────────────────────────────────────────────────────
# "square feet" and "hectare" both contain "are". Matching that naively
# converts square feet as ares (1076x) or hectares as ares (100x).

def test_square_feet_is_not_matched_as_are():
    assert M.detect_unit("2180 Square Feet") == "sq_ft"


def test_hectare_is_not_matched_as_are():
    assert M.detect_unit("1.25 Hectares") == "hectare"


def test_are_still_matches_when_it_is_genuinely_are():
    assert M.detect_unit("12 Ares") == "are"


def test_acre_does_not_collide_with_are():
    # "acre" does not contain "are" — asserted so the alias table can't regress
    assert "are" not in "acre"
    assert M.detect_unit("6.00 Acres") == "acre"


@pytest.mark.parametrize("raw,unit", [
    ("2180 Sq. ft", "sq_ft"),
    ("2180 Sq . Ft", "sq_ft"),   # OCR spaces either side of the period
    ("2180  sq  ft", "sq_ft"),
    ("0.202 Sq.mt", "sq_m"),
    ("500 sq.yds", "sq_yard"),
    ("6.00 Cents", "cent"),
    ("2 Grounds", "ground"),
])
def test_unit_aliases(raw, unit):
    assert M.detect_unit(raw) == unit


def test_decimal_point_survives_unit_normalization():
    # the spacing fold must not corrupt the number it sits next to
    assert M.parse_area("6.75 Cents")[0] == pytest.approx(6.75)


def test_unknown_unit_is_none():
    assert M.detect_unit("2180") is None


# ── conversion ───────────────────────────────────────────────────────────────

def test_cent_to_sqft():
    value, unit, sqft = M.parse_area("6.00 Cents")
    assert (value, unit) == (6.0, "cent")
    assert sqft == pytest.approx(2613.6)


def test_acre_to_sqft():
    assert M.parse_area("1 Acre")[2] == pytest.approx(43560.0)


def test_hundred_cents_equals_one_acre():
    assert M.to_sqft(100, "cent") == pytest.approx(M.to_sqft(1, "acre"))


def test_hectare_is_hundred_ares():
    assert M.to_sqft(1, "hectare") == pytest.approx(M.to_sqft(100, "are"), rel=1e-5)


def test_sqft_passthrough():
    assert M.parse_area("2180 Sq.ft")[2] == pytest.approx(2180.0)


def test_unconvertible_returns_none_sqft():
    value, unit, sqft = M.parse_area("2180")
    assert value == 2180.0 and unit is None and sqft is None


# ── quantities: fractions and part-of-whole ──────────────────────────────────

def test_unicode_half():
    assert M.parse_quantity("2 ½") == pytest.approx(2.5)


def test_ascii_fraction():
    assert M.parse_quantity("16 1/2 feet") == pytest.approx(16.5)


def test_bare_unicode_fraction():
    assert M.parse_quantity("¾") == pytest.approx(0.75)


def test_thousands_separator():
    assert M.parse_quantity("2,180 sq.ft") == pytest.approx(2180.0)


def test_part_of_whole_takes_the_parcel_being_sold():
    # "Acre 6.00 cents out of Acre 7.86 Cents" — 6 cents is what is sold
    assert M.parse_quantity("Acre 6.00. cents out of Acre 7.86 Cents") == pytest.approx(6.0)


def test_no_number_is_none():
    assert M.parse_quantity("as is where is") is None


# ── lengths ──────────────────────────────────────────────────────────────────

def test_plain_feet():
    assert M.parse_length("30 feet") == pytest.approx(30.0)


def test_feet_and_inches():
    assert M.parse_length("34'9 ft") == pytest.approx(34.75)


def test_fractional_feet():
    assert M.parse_length("16 1/2 feet") == pytest.approx(16.5)


def test_metric_length_converts():
    assert M.parse_length("10 metres") == pytest.approx(32.808, rel=1e-3)


def test_area_in_a_length_field_is_rejected():
    # 7 real cases in the corpus, e.g. a boundary "measurement" of "19 Sq.Ft"
    assert M.is_length("19 Sq.Ft") is False
    assert M.parse_length("19 Sq.Ft") is None


def test_plain_length_is_valid():
    assert M.is_length("25 ft") is True


# ── adjacency: access kind + road width ──────────────────────────────────────

def test_road_with_width():
    assert M.read_adjacency("23 Feet wide East-West Road") == ("road", 23.0)


def test_setback_is_not_frontage():
    # contains "ROAD" but means land reserved for widening — it REDUCES the plot
    kind, width = M.read_adjacency("30 FT LAND LEFT BY ROAD")
    assert kind == "setback"
    assert width == 30.0


def test_pathway_is_distinguished_from_road():
    assert M.read_adjacency("15 Feet Common Pathway")[0] == "pathway"


def test_street_is_distinguished_from_road():
    assert M.read_adjacency("10 feet common street")[0] == "street"


def test_neighbouring_plot_yields_no_road_width():
    # the number here is the neighbour's dimension, not access width
    kind, width = M.read_adjacency("Plot of Mr.X 40 ft")
    assert kind == "plot"
    assert width is None


def test_road_without_a_stated_width():
    assert M.read_adjacency("East West Road") == ("road", None)


def test_metric_road_width_converts():
    _, width = M.read_adjacency("6 metre wide Road")
    assert width == pytest.approx(19.69, rel=1e-3)


def test_empty_adjacency():
    assert M.read_adjacency("") == (None, None)


# ── headline extent selection ────────────────────────────────────────────────

def test_flat_uses_built_up_not_uds_parent():
    kinds = {"built_up": 950.0, "uds": 509.0, "uds_parent": 80854.0}
    assert M.pick_headline(kinds, "flat") == "built_up"


def test_flat_never_falls_back_to_uds_parent():
    # only the UDS parent is convertible — still refuse it rather than divide
    # a single flat's price by the whole apartment plot
    assert M.pick_headline({"uds_parent": 80854.0}, "flat") is None


def test_flat_falls_back_to_super_built_up():
    assert M.pick_headline({"super_built_up": 1200.0}, "flat") == "super_built_up"


def test_land_uses_total_area():
    assert M.pick_headline({"total": 2613.6, "built_up": 800.0}, "land") == "total"


def test_no_usable_measurement_returns_none():
    assert M.pick_headline({"total": None}, "plot") is None


def test_uds_implies_flat_when_property_type_is_missing():
    # Only a flat holds an undivided share. Notices routinely omit
    # property_type, so the UDS itself has to identify the dwelling — else the
    # default order picks the land extent as the price-per-sqft denominator.
    kinds = {"uds": 1266.0, "extent": 1880.0, "built_up": 1880.0}
    assert M.pick_headline(kinds, None) == "built_up"


def test_uds_inference_still_refuses_uds_parent():
    assert M.pick_headline({"uds_parent": 80854.0, "uds": 509.0}, None) is None


def test_explicit_land_type_beats_uds_inference():
    kinds = {"uds": 100.0, "total": 4356.0}
    assert M.pick_headline(kinds, "agricultural") == "total"
