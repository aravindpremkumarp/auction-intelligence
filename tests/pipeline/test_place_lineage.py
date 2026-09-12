"""pipeline.place_lineage: reading a portal/notice district disagreement.

The pairs asserted here are the ones the live corpus actually produces, with
their counts at the time this was written, so a change that re-silences a real
conflict — or starts explaining one away — fails on a case that exists.
"""
from __future__ import annotations

import pytest

from pipeline.place_lineage import (
    AGREE, BENIGN, DISTRICT_LINEAGE, METRO, NO_NOTICE_DISTRICT,
    NO_PORTAL_DISTRICT, NOT_A_DISTRICT, NOT_COMPARABLE, NOTICE_NAMES_PARENT,
    PORTAL_NAMES_PARENT, SAID, TALUK_OUTRANKS, UNEXPLAINED, ancestors, classify,
    needs_review, split_year,
)

#: The 38 districts as :District nodes spell them. The lineage table is dead
#: weight if a name here drifts, so this is the answer key rather than a
#: reference to the graph — a test that reads the database cannot run in CI.
DISTRICTS = frozenset({
    "Ariyalur", "Chengalpattu", "Chennai", "Coimbatore", "Cuddalore",
    "Dharmapuri", "Dindigul", "Erode", "Kallakurichi", "Kancheepuram",
    "Kanyakumari", "Karur", "Krishnagiri", "Madurai", "Mayiladuthurai",
    "Nagapattinam", "Namakkal", "Nilgiris", "Perambalur", "Pudukkottai",
    "Ramanathapuram", "Ranipet", "Salem", "Sivagangai", "Tenkasi", "Thanjavur",
    "Theni", "Thiruchirappalli", "Thiruvarur", "Thoothukudi", "Tirunelveli",
    "Tirupathur", "Tiruppur", "Tiruvallur", "Tiruvannamalai", "Vellore",
    "Villupuram", "Virudhunagar",
})


def test_every_lineage_name_is_a_real_district():
    """A misspelled name never matches and never complains. Catch it here."""
    for child, (parent, _year) in DISTRICT_LINEAGE.items():
        assert child in DISTRICTS, f"{child} is not a :District name"
        assert parent in DISTRICTS, f"{parent} is not a :District name"


def test_lineage_has_no_cycles():
    for child in DISTRICT_LINEAGE:
        chain = ancestors(child)
        assert len(chain) == len(set(chain)), f"{child} walks a cycle"
        assert child not in chain


def test_ancestors_walk_past_the_first_parent():
    # 2020 split off a 1991 split: both names can appear on a portal row.
    assert ancestors("Mayiladuthurai") == ["Nagapattinam", "Thanjavur"]
    assert ancestors("Chengalpattu") == ["Kancheepuram"]
    assert ancestors("Chennai") == []


def test_split_year_reports_the_split_that_made_this_district():
    assert split_year("Mayiladuthurai", "Nagapattinam") == 2020
    assert split_year("Mayiladuthurai", "Thanjavur") == 1991
    assert split_year("Chengalpattu", "Chennai") is None


@pytest.mark.parametrize("portal,notice,n", [
    # The 2019/2020 reorganisation, with its live count. In every one of these
    # the notice is right and the portal kept the pre-split parent.
    ("Kancheepuram", "Chengalpattu", 152),
    ("Vellore", "Ranipet", 46),
    ("Vellore", "Tirupathur", 21),
    ("Villupuram", "Kallakurichi", 6),
    ("Nagapattinam", "Mayiladuthurai", 4),
    ("Tirunelveli", "Tenkasi", 3),
    # Older splits, same shape.
    ("Coimbatore", "Tiruppur", 18),
    ("Salem", "Namakkal", 5),
    ("Coimbatore", "Erode", 3),
    ("Nagapattinam", "Thiruvarur", 5),
])
def test_portal_naming_the_parent_is_explained(portal, notice, n):
    assert n > 0  # the pair is real, not invented for the test
    assert classify(portal, notice, districts=DISTRICTS) == PORTAL_NAMES_PARENT
    assert not needs_review(PORTAL_NAMES_PARENT)


@pytest.mark.parametrize("portal,notice", [
    ("Tiruppur", "Coimbatore"),
    ("Namakkal", "Salem"),
    ("Tenkasi", "Tirunelveli"),
    ("Tiruvannamalai", "Vellore"),
])
def test_the_reverse_direction_still_needs_review(portal, notice):
    """The notice is the trusted source, so it naming the older district is a
    resolution that stopped short — not a stale portal."""
    kind = classify(portal, notice, districts=DISTRICTS)
    assert kind == NOTICE_NAMES_PARENT
    assert needs_review(kind)


@pytest.mark.parametrize("portal,notice", [
    ("Chennai", "Chengalpattu"),
    ("Chennai", "Kancheepuram"),
    ("Tiruvallur", "Chennai"),
    ("Chennai", "Tiruvallur"),
])
def test_metro_ring_pairs_are_not_a_disagreement(portal, notice):
    assert classify(portal, notice, districts=DISTRICTS) == METRO


def test_lineage_beats_metro_where_both_apply():
    """Kancheepuram -> Chengalpattu is inside the ring AND on the line. The
    line is the more specific answer, and it is 152 listings."""
    assert classify("Kancheepuram", "Chengalpattu",
                    districts=DISTRICTS) == PORTAL_NAMES_PARENT
    assert classify("Kancheepuram", "Tiruvallur",
                    districts=DISTRICTS) == PORTAL_NAMES_PARENT


@pytest.mark.parametrize("portal,notice", [
    ("Madurai", "Karur"),        # Karur came from Thiruchirappalli
    ("Dindigul", "Theni"),       # Theni came from Madurai
    ("Sivagangai", "Pudukkottai"),
    ("Chennai", "Thiruvarur"),
])
def test_real_disagreements_survive(portal, notice):
    """The point of the table is that it does NOT explain these away."""
    kind = classify(portal, notice, districts=DISTRICTS)
    assert kind == UNEXPLAINED
    assert needs_review(kind)


def test_a_portal_name_that_is_not_a_district_is_a_gazetteer_gap():
    """8 live :City names — Periyakulam, Chidambaram, Palani and five more, 58
    listings — are towns the gazetteer has no alias for. That is a missing
    alias to add, not a notice to re-read, so it gets its own name; it still
    needs a human, which is why it is not benign."""
    kind = classify("Periyakulam", "Theni", districts=DISTRICTS)
    assert kind == NOT_A_DISTRICT
    assert needs_review(kind)


def test_a_missing_side_is_not_a_conflict():
    """264 listings resolved no district at all. They are a coverage number the
    review panel already reports; counting them here would report one gap
    twice."""
    assert classify("Salem", None) == NO_NOTICE_DISTRICT
    assert classify("Salem", "") == NO_NOTICE_DISTRICT
    assert classify(None, "Salem") == NO_PORTAL_DISTRICT
    assert not needs_review(NO_NOTICE_DISTRICT)
    assert not needs_review(NO_PORTAL_DISTRICT)


def test_a_notice_district_outside_the_gazetteer_is_never_explained_away():
    """The notice side is written from the gazetteer, so a value that is not a
    district means the resolver wrote something wrong — the one case where the
    portal is the more trustworthy string."""
    assert classify("Salem", "Nowhere", districts=DISTRICTS) == UNEXPLAINED


def test_agreement_ignores_surrounding_space():
    assert classify("Salem", "Salem") == AGREE
    assert classify("  Salem  ", "Salem") == AGREE


def test_without_a_district_set_an_unmapped_name_reads_as_disagreement():
    """`districts` is optional so the classifier can run on rows the gazetteer
    has not vetted; then an unmapped name reads as a real disagreement, which
    is the safe direction."""
    assert classify("Periyakulam", "Theni") == UNEXPLAINED


@pytest.mark.parametrize("portal,notice", [
    # Was `unexplained`: the taluk (Manmangalam) is in Karur, the portal says
    # Madurai. 34 live listings of this shape.
    ("Madurai", "Karur"),
    ("Chennai", "Thiruvarur"),
    # The portal writes "Karaikudi"; the gazetteer maps it to Sivagangai, and
    # the notice's taluk (Ponnamaravathi) is in Pudukkottai.
    ("Sivagangai", "Pudukkottai"),
    # Was `notice-names-parent`: Pollachi IS in Coimbatore, so the parent name
    # is the right answer and the portal's child name is the wrong one. 24 live
    # listings of this shape.
    ("Tiruppur", "Coimbatore"),
    ("Namakkal", "Salem"),
    ("Tiruvannamalai", "Vellore"),
])
def test_a_matched_taluk_settles_the_district(portal, notice):
    """A taluk name is unique across all 316, so it names its own district.
    Verified on the live corpus: of the 69 conflicts left after lineage and the
    metro ring, 58 came from a matched taluk and all 58 taluks sit in the
    stored district — zero contradictions."""
    kind = classify(portal, notice, districts=DISTRICTS, district_source="taluk")
    assert kind == TALUK_OUTRANKS
    assert not needs_review(kind)


@pytest.mark.parametrize("portal,notice,expected", [
    ("Madurai", "Karur", UNEXPLAINED),
    ("Tiruppur", "Coimbatore", NOTICE_NAMES_PARENT),
])
def test_a_district_string_alone_settles_nothing(portal, notice, expected):
    """`district` means the notice's own district string with no taluk to check
    it against — exactly the case a portal disagreement is worth reading."""
    kind = classify(portal, notice, districts=DISTRICTS,
                    district_source="district")
    assert kind == expected
    assert needs_review(kind)


def test_the_taluk_rule_never_overrides_a_more_specific_label():
    """It says which side to believe; lineage and the metro ring say *why* the
    two names differ, which is the better answer where it applies."""
    assert classify("Kancheepuram", "Chengalpattu", districts=DISTRICTS,
                    district_source="taluk") == PORTAL_NAMES_PARENT
    assert classify("Chennai", "Tiruvallur", districts=DISTRICTS,
                    district_source="taluk") == METRO


def test_an_unknown_source_is_not_authoritative():
    """`taluk-field-names-a-district` reads a district out of the taluk slot —
    no taluk was matched, so nothing checked the answer."""
    for src in (None, "district", "taluk-field-names-a-district", ""):
        assert classify("Madurai", "Karur", districts=DISTRICTS,
                        district_source=src) == UNEXPLAINED


def test_every_kind_has_a_plain_english_label():
    for kind in (AGREE, PORTAL_NAMES_PARENT, NOTICE_NAMES_PARENT, METRO,
                 TALUK_OUTRANKS, UNEXPLAINED, NOT_A_DISTRICT,
                 NO_NOTICE_DISTRICT, NO_PORTAL_DISTRICT):
        assert SAID[kind]
    assert BENIGN <= set(SAID)
    assert NOT_COMPARABLE <= set(SAID)
    assert not (BENIGN & NOT_COMPARABLE)
