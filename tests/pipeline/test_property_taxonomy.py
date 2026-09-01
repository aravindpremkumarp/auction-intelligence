"""Property-type taxonomy: the rule order, the substring traps, and a sample
of the 219 real values LangExtract produced across the corpus."""
from __future__ import annotations

import pytest

from pipeline.property_taxonomy import (
    AGRICULTURAL,
    COMMERCIAL,
    FLAT,
    HOUSE,
    INDUSTRIAL,
    LAND,
    MIXED,
    MOVABLE,
    PLOT,
    UNKNOWN,
    asset_category,
    classify_portal_type,
    classify_property_type,
    conflict_severity,
    is_conflict,
)


# ── head of the distribution: the seven values covering ~78% of entities ─────

@pytest.mark.parametrize("raw,expected", [
    ("land and building", HOUSE),
    ("flat", FLAT),
    ("land", LAND),
    ("house", HOUSE),
    ("plot", PLOT),
    ("land with building", HOUSE),
    ("vacant land", LAND),
])
def test_head_values(raw, expected):
    assert classify_property_type(raw) == expected


# ── "house site" means a vacant plot, not a house ────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("house site", PLOT),
    ("house sites", PLOT),
    ("vacant house site", PLOT),
    ("house-site plot", PLOT),
    ("house plot", PLOT),
    ("rcc house site", PLOT),
    ("housing site", PLOT),
    ("vacant house site land", PLOT),
    # ...but a structure on that site makes it a house again
    ("house site with building", HOUSE),
    ("house plot with rcc building", HOUSE),
    ("house sites with house", HOUSE),
    ("vacant house site with house", HOUSE),
    ("house plot with terraced house", HOUSE),
    # "house and site" is not the compound noun — two things, one is a house
    ("house and site", HOUSE),
])
def test_house_site_is_a_plot(raw, expected):
    assert classify_property_type(raw) == expected


# ── substring traps that a naive `in` check gets wrong ───────────────────────

def test_finished_does_not_contain_a_shed():
    # "semi-finished" ends in ...s-h-e-d; an unanchored 'shed' reads industrial
    assert classify_property_type("land with semi-finished building") == HOUSE


def test_non_agricultural_is_not_agricultural():
    assert classify_property_type("non-agricultural land") == LAND
    assert classify_property_type("non- agricultural land") == LAND


def test_immovable_is_not_movable():
    # "immovable" ends in ...m-o-v-a-b-l-e
    assert classify_property_type("immovable property") == UNKNOWN
    assert classify_property_type("immovable") == UNKNOWN


def test_accumulated_is_not_cum():
    assert classify_property_type("land with accumulated structures") == HOUSE


# ── real estate wins over machinery listed alongside it ──────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("industrial land and building with plant and machinery", INDUSTRIAL),
    ("rice mill land and building with plant and machinery", INDUSTRIAL),
    ("industrial land with buildings and machineries", INDUSTRIAL),
])
def test_immovable_anchor_beats_machinery(raw, expected):
    assert classify_property_type(raw) == expected


@pytest.mark.parametrize("raw", [
    "vehicle", "machinery", "plant and machinery", "movable", "fabrics",
    "stocks", "trademark", "securities and financial assets", "asset bundle",
    "plant and machinery, vehicles, office equipment",
])
def test_pure_movables(raw):
    assert classify_property_type(raw) == MOVABLE


# ── category beats the generic "has a building" rule ─────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("commercial building", COMMERCIAL),
    ("industrial building", INDUSTRIAL),
    ("factory land and building", INDUSTRIAL),
    ("land with rcc factory shed", INDUSTRIAL),
    ("rice mill with land and building", INDUSTRIAL),
    ("shop room and godown with land", COMMERCIAL),
    ("hotel building with land", COMMERCIAL),
    ("commercial godown land", COMMERCIAL),
    ("industrial land with godown", INDUSTRIAL),
    ("agricultural land with poly house", AGRICULTURAL),
    ("punjai land with building", AGRICULTURAL),
    ("nanja land", AGRICULTURAL),
])
def test_category_before_generic_building(raw, expected):
    assert classify_property_type(raw) == expected


@pytest.mark.parametrize("raw", [
    "residential cum commercial",
    "residential cum industrial building mixed land",
    "commercial cum residential",
    "mixed-use",
    "residential and industrial",
])
def test_mixed_use(raw):
    assert classify_property_type(raw) == MIXED


# ── Tamil-origin and long-tail phrasings ─────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("manai with rcc building and land", HOUSE),
    ("manai and building", HOUSE),
    ("land with superstructure", HOUSE),
    ("land along with building", HOUSE),
    ("land together with building", HOUSE),
    ("land & building", HOUSE),
    ("land and buildings", HOUSE),
    ("site and superstructure", HOUSE),
    ("building and land", HOUSE),
    ("terraced house", HOUSE),
    ("tiled house", HOUSE),
    ("villa", HOUSE),
    ("individual house", HOUSE),
    ("vacant sites", PLOT),
    ("site", PLOT),
    ("leasehold land", LAND),
    ("land with common passage", LAND),
    ("residential apartment (under construction)", FLAT),
    ("land and residential flats", FLAT),
])
def test_long_tail(raw, expected):
    assert classify_property_type(raw) == expected


# ── values that genuinely name nothing ───────────────────────────────────────

@pytest.mark.parametrize("raw", [
    "", None, "   ", "property", "mortgaged property", "vacant property",
])
def test_unknown(raw):
    assert classify_property_type(raw) == UNKNOWN


# ── asset category ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("bucket,expected", [
    (HOUSE, "residential"),
    (FLAT, "residential"),
    (LAND, "residential"),
    (PLOT, "residential"),
    (COMMERCIAL, "commercial"),
    (INDUSTRIAL, "industrial"),
    (AGRICULTURAL, "agricultural"),
    (MIXED, "mixed"),
    (MOVABLE, "movable"),
    (UNKNOWN, "unknown"),
])
def test_category_of_bucket(bucket, expected):
    assert asset_category(bucket) == expected


def test_category_falls_back_to_raw_text_when_form_is_unnamed():
    # bucket is UNKNOWN, but the category word is still there
    assert classify_property_type("residential property") == UNKNOWN
    assert asset_category(UNKNOWN, "residential property") == "residential"
    assert asset_category(UNKNOWN, "industrial property") == "industrial"
    assert asset_category(UNKNOWN, "mortgaged property") == "unknown"


# ── portal dropdown ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("Land And Building", HOUSE),
    ("Land", LAND),
    ("Plot", PLOT),
    ("Flat", FLAT),
    ("House", HOUSE),
    ("Villa", HOUSE),
    ("Residential Unit", HOUSE),
    ("Factory land and Building", INDUSTRIAL),
    ("Industrial Land & Building", INDUSTRIAL),
    ("Cold Storage Land And Building", INDUSTRIAL),
    ("Shed", INDUSTRIAL),
    ("Commercial Shop", COMMERCIAL),
    ("Godown", COMMERCIAL),
    ("Agricultural Land", AGRICULTURAL),
    ("Non- Agricultural Land", LAND),
    ("Plant & Machinery", MOVABLE),
    ("Machinary", MOVABLE),
    ("Others", UNKNOWN),
    (None, UNKNOWN),
    ("Something The Portal Just Added", UNKNOWN),
])
def test_portal_buckets(name, expected):
    assert classify_portal_type(name) == expected


# ── conflict flag ────────────────────────────────────────────────────────────

def test_conflict_needs_two_real_buckets():
    assert is_conflict(HOUSE, LAND) is True
    assert is_conflict(FLAT, PLOT) is True
    assert is_conflict(HOUSE, HOUSE) is False
    # an unknown on either side is a gap, not a disagreement
    assert is_conflict(UNKNOWN, LAND) is False
    assert is_conflict(HOUSE, UNKNOWN) is False


def test_land_and_plot_are_the_same_thing():
    """Both mean bare ground; the split between them is how the ground is
    described, not what is sold. The two sources routinely pick different
    words for one property, and counting that put 226 rows in front of a
    reviewer with nothing to decide."""
    assert is_conflict(PLOT, LAND) is False
    assert is_conflict(LAND, PLOT) is False


def test_a_building_sold_as_bare_ground_is_critical():
    """The disagreement that misleads a search: someone filtering for land
    is shown a house. 666 live listings, 139 of them flats under Land/Plot."""
    assert conflict_severity(HOUSE, LAND) == "critical"
    assert conflict_severity(FLAT, PLOT) == "critical"
    # and the reverse — the notice says bare ground, the portal a building
    assert conflict_severity(LAND, HOUSE) == "critical"


def test_two_kinds_of_building_disagreeing_is_ordinary():
    """Both sides agree something is built and differ on what. Worth fixing,
    but it does not put a house in a land search."""
    assert conflict_severity(COMMERCIAL, HOUSE) == "med"
    assert conflict_severity(INDUSTRIAL, COMMERCIAL) == "med"


def test_agreement_has_no_severity():
    assert conflict_severity(HOUSE, HOUSE) is None
    assert conflict_severity(PLOT, LAND) is None
    assert conflict_severity(UNKNOWN, LAND) is None
