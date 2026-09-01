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
    classify_from_schedule,
    classify_lot_type,
    classify_portal_type,
    classify_property_type,
    conflict_severity,
    effective_bucket,
    is_conflict,
    resolve_bucket,
    search_buckets,
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


# ── what a search reads ──────────────────────────────────────────────────────


def test_the_notice_wins_over_the_portal():
    """The notice is the legal document; the portal's Land/Plot default agrees
    with it only 34-54% of the time. 832 listings live are filed under a
    portal type their notice contradicts."""
    assert effective_bucket(FLAT, LAND) == FLAT
    assert effective_bucket(HOUSE, PLOT) == HOUSE


def test_the_portal_fills_a_gap_but_never_overrides():
    """99 listings no extraction reached would become unfindable by type if
    the portal value were simply discarded."""
    assert effective_bucket(None, LAND) == LAND
    assert effective_bucket(UNKNOWN, FLAT) == FLAT
    # nothing on either side is still nothing — not a guess
    assert effective_bucket(None, None) == UNKNOWN


def test_a_land_search_also_matches_plots():
    """The query side of the same equivalence `is_conflict` uses: someone
    filtering for land means bare ground, and whether a notice called it a
    plot is not a distinction they asked for."""
    assert search_buckets(LAND) == [LAND, PLOT]
    assert search_buckets(PLOT) == [LAND, PLOT]
    assert search_buckets(FLAT) == [FLAT]


def test_resolve_bucket_accepts_all_three_vocabularies():
    """Search callers speak bucket names (the facet), portal names (old
    bookmarks), and prose (hand-typed). All three must reach the same bucket
    or a saved search breaks for a rename nobody asked for."""
    assert resolve_bucket(HOUSE) == HOUSE                      # bucket name
    assert resolve_bucket("Land And Building") == HOUSE        # portal name
    assert resolve_bucket("Apartment") == FLAT                 # prose
    assert resolve_bucket("vacant house site") == PLOT         # prose


def test_resolve_bucket_refuses_to_guess():
    """UNKNOWN is what makes a filter match nothing rather than everything."""
    assert resolve_bucket("Spaceship") == UNKNOWN
    assert resolve_bucket("") == UNKNOWN
    assert resolve_bucket(None) == UNKNOWN


# ── the schedule text ────────────────────────────────────────────────────────


def test_a_flat_filed_as_bare_ground_is_corrected_by_its_schedule():
    """The extractor writes a six-word paraphrase; the schedule is the notice.
    Seven live lots read "Flat No. S1, 1149 sq.ft" under a stated type of
    "vacant house site" or "land", unfindable by anyone searching for a flat.
    """
    assert classify_lot_type(
        "vacant house site",
        "All that piece and parcel of Two bedroom Residential Flat No. S1 "
        "admeasuring 1149 Sq. ft. of Super Built up area along with Undivided "
        "Share of Land to an extent of 461 Sq.ft.") == FLAT
    assert classify_lot_type(
        "land and building",
        "together with 948 Sq. ft., Built up area in Second Floor bearing "
        "Flat No. A, including TNEB & CMWSSB Connections.") == FLAT


def test_a_schedule_naming_no_unit_changes_nothing():
    """99.8% of lots. The correction must be inert unless a unit is named."""
    assert classify_lot_type("vacant land", "All that piece and parcel of "
                             "land measuring 2400 sq ft in Sy No.159.") == LAND
    assert classify_lot_type("plot", None) == PLOT
    assert classify_lot_type("flat", "") == FLAT


def test_the_schedule_never_overrides_a_specific_built_form():
    """A stated built form is a deliberate claim, and overriding it destroys
    information. Both cases are live lots: one genuinely mixed-use, one a
    service apartment inside a commercial hotel complex."""
    assert classify_lot_type(
        "mixed-use",
        "Flat No. A @ Ground Floor, Block A. Residential Flat & Commercial "
        "Shop.") == MIXED
    assert classify_lot_type(
        "commercial",
        "Commercial Property being service Apartments No. 903 & 93/34, 9th "
        "Floor, situated at commercial complex & Hotel.") == COMMERCIAL


def test_a_boundary_clause_names_the_neighbours_not_this_property():
    """"South By : Villa No. 7" is the property NEXT DOOR. Read as the
    subject, a flat in a villa layout becomes a villa — one live lot did."""
    assert classify_from_schedule(
        "Bounded on the North By : 18' Wide Passage, South By : Villa No. 7, "
        "East By : Villa No. 17.") is None
    # ...and the unit's own identifier still reads, boundaries notwithstanding
    assert classify_from_schedule(
        "Flat No. F1 in the First Floor, 775 Sq.ft. Bounded on the North By : "
        "18' Wide Passage, South By : Flat No. 7.") == FLAT


def test_a_villa_identifier_is_deliberately_not_read():
    """"Villa No." is not a usable marker: this corpus OCRs "Village No." as
    "Villa No." often enough that the single lot it would reclassify is a
    survey-number list in Agamcherry Village. It earns no correct change
    anywhere, so it is not in the table — a marker that buys nothing and
    costs a false positive does not go in.

    The `len(hits) == 1` guard in `classify_from_schedule` is what keeps a
    second marker safe to add later: two forms would mean two properties, and
    picking either would be a guess.
    """
    assert classify_from_schedule("Villa No. 7, measuring 2543 sq.ft.") is None
    assert classify_lot_type("plot", "Agamcherry Village, Villa No.417/1, "
                             "Now Sub Divided as S. No. 418/2A") == PLOT


def test_the_prose_rules_are_never_run_over_a_schedule():
    """A schedule says land, building, plot and site about ONE property in
    consecutive clauses. Only the unit identifier is read, so a schedule full
    of those words but naming no unit yields nothing."""
    assert classify_from_schedule(
        "All that piece and parcel of vacant land, plot and house site "
        "together with the building and structures thereon.") is None
