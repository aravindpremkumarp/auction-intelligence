"""Unit tests for pipeline/area_agreement.py.

The listing-side strings below are real values from the live graph — the
parser exists because of them, so they are the fixtures.
"""
from __future__ import annotations

import pipeline.area_agreement as AA


# ── stated_sqft: the free-text parser ────────────────────────────────────────

def test_plain_sqft():
    assert AA.stated_sqft("1853.5 sq.ft") == (1853.5, "stated")


def test_grouped_digits_and_spacing_variants():
    assert AA.stated_sqft("2,180 Sq. ft") == (2180.0, "stated")
    assert AA.stated_sqft("646 sqft") == (646.0, "stated")


def test_bracketed_conversion_prefers_the_stated_sqft():
    """809765: '0.10 1/4 acre (4625 Sq ft)'. The bracket is the writer's own
    bottom line, and the '0.10 1/4' defeats any fraction grammar — converting
    the acre figure ourselves would manufacture a disagreement."""
    assert AA.stated_sqft("0.10 1/4 acre (4625 Sq ft)") == (4625.0, "stated")


def test_hectare_string_with_its_own_conversion_chain():
    """810376: hectares, then cents, then sq.ft — take the sq.ft."""
    v, how = AA.stated_sqft("0.25.5 Hectares (12.209 cents or 5318.4375 sq.ft)")
    assert (round(v, 1), how) == (5318.4, "stated")


def test_sqm_bracket_does_not_shadow_the_sqft():
    assert AA.stated_sqft("1000 sq.ft (92.91 sq.m)") == (1000.0, "stated")


def test_multi_value_string_refuses_to_pick():
    """811295: '7200 Sq. ft (each plot 1800 Sq. ft.)' describes more than one
    thing. My first cut took the last number and got the per-plot figure —
    exactly the guess this refuses to make."""
    assert AA.stated_sqft("7200 Sq. ft (each plot 1800 Sq. ft.)") == (None, "multi_value")


def test_repeated_equal_sqft_is_not_multi_value():
    assert AA.stated_sqft("1200 sq.ft (1,200 Sq. Ft.)") == (1200.0, "stated")


def test_land_units_convert_when_no_sqft_stated():
    v, how = AA.stated_sqft("6.00 Cents")
    assert (v, how) == (2613.6, "converted")
    v, how = AA.stated_sqft("2 grounds")
    assert (v, how) == (4800.0, "converted")


def test_unparseable_and_empty():
    assert AA.stated_sqft("residential plot") == (None, "unparsed")
    assert AA.stated_sqft(None) == (None, "unparsed")
    assert AA.stated_sqft("") == (None, "unparsed")


# ── compare_areas ────────────────────────────────────────────────────────────

def test_agree_within_tolerance():
    assert AA.compare_areas(1000, 1000)[0] == "agree"
    assert AA.compare_areas(999.98, 1000)[0] == "agree"  # sq.m round-trip


def test_magnitude_slip_10x_and_100x():
    assert AA.compare_areas(120, 1200)[0] == "magnitude_slip"
    assert AA.compare_areas(26910, 269.1)[0] == "magnitude_slip"


def test_disagree_is_everything_else():
    # 812923: listing 1997 vs notice 890 — 2.2x, a real conflict
    verdict, ratio = AA.compare_areas(1997, 890)
    assert verdict == "disagree"
    assert round(ratio, 2) == 2.24


def test_unknown_when_a_side_is_missing_or_zero():
    assert AA.compare_areas(None, 1200)[0] == "unknown"
    assert AA.compare_areas(1200, 0)[0] == "unknown"


# ── check_match ──────────────────────────────────────────────────────────────

def _pair(area_raw, notice_area):
    return ({"aid": "a1", "area_raw": area_raw},
            {"lot_index": "1", "fields": {"total_area": notice_area}})


def test_agreeing_pair_is_not_a_finding():
    assert AA.check_match(*_pair("1200 sq.ft", "1200 sq.ft")) is None


def test_disagreeing_pair_carries_both_raw_strings():
    f = AA.check_match(*_pair("1997 Sq. ft.", "890 sq.ft"))
    assert f["verdict"] == "disagree" and f["severity"] == "med"
    assert f["listing_area"] == "1997 Sq. ft."
    assert f["notice_area"] == "890 sq.ft"
    assert f["parse_how"] == "stated/stated"


def test_slip_is_critical():
    f = AA.check_match(*_pair("120 sq.ft", "1200 sq.ft"))
    assert f["verdict"] == "magnitude_slip" and f["severity"] == "critical"


def test_unknown_is_not_a_finding():
    assert AA.check_match(*_pair("residential plot", "1200 sq.ft")) is None
    assert AA.check_match(*_pair(None, "1200 sq.ft")) is None


def test_headline_measurement_outranks_the_field_text():
    """total_area on the listing is overwritten from this same extraction's
    field text every pass, so text-vs-text comparison finds nothing (measured:
    0 findings corpus-wide). The graph's headline measurement is the second
    number a user actually sees, and it wins when present."""
    listing, lot = _pair("1200 sq.ft", "1200 sq.ft")
    f = AA.check_match(listing, lot, headline_sqft=4840.0)
    assert f["verdict"] == "disagree"
    assert f["notice_sqft"] == 4840.0
    assert f["parse_how"] == "stated/headline"


def test_no_headline_falls_back_to_field_text():
    listing, lot = _pair("1200 sq.ft", "890 sq.ft")
    f = AA.check_match(listing, lot, headline_sqft=None)
    assert f["notice_sqft"] == 890.0
    assert f["parse_how"] == "stated/stated"


def test_mixed_fraction_before_the_unit():
    """748228: '7527 1/2 sq.ft' is 7527.5 — a plain digit grab reads the
    '2' alone and manufactures a 3763x disagreement out of an exact match."""
    assert AA.stated_sqft("7527 1/2 sq.ft") == (7527.5, "stated")
    assert AA.stated_sqft("650 ½ sq.ft") == (650.5, "stated")


# ── the sq.ft as an addend, not the total ────────────────────────────────────

def test_an_and_joined_sqft_is_an_addend_not_the_extent():
    """A Tamil-notice idiom: "5 cents and 7 sq.ft" is 2,185 sq.ft — a whole
    number of land units plus the remainder. Reading the 7 as the extent is
    the one way the explicit-sq.ft rule fails catastrophically low, and four
    live headline extents (7, 392, 728, 1540) were doing exactly that against
    real properties of 2,185, 828, 3,128 and 6,340 sq.ft.

    The composite is refused, not summed: the notice states the parts in two
    units and adding them would be this module doing arithmetic the notice
    did not.
    """
    assert AA.stated_sqft("5 cents and 7 sq.ft") == (None, "composite")
    assert AA.stated_sqft("1 Ground and 728 Sq.Ft.") == (None, "composite")
    assert AA.stated_sqft("1 Cent and 392 Sqft") == (None, "composite")
    assert AA.stated_sqft("2 grounds and 1540 sq.ft") == (None, "composite")


def test_a_bracketed_sqft_still_restates_the_whole_extent():
    """The guard above must not swallow the ordinary case: a sq.ft in
    brackets after a land unit is the writer's own conversion of the SAME
    extent, and it is the figure to trust."""
    assert AA.stated_sqft("9-1/2 cents (4148 sq.ft)") == (4148.0, "stated")
    assert AA.stated_sqft("0.02 Cent (881 sq.ft)") == (881.0, "stated")
    assert AA.stated_sqft("3597 sq.ft (8 1/4 cents)") == (3597.0, "stated")
    assert AA.stated_sqft(
        "0.25.5 Hectares (12.209 cents or 5318.4375 sq.ft)"
    ) == (5318.4375, "stated")
