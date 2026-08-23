"""pipeline.place_resolution: matching a notice's place to the official record.

Every string here came off the live corpus or the gazetteer — the matches that
must happen, and the near-misses that must not.
"""
from __future__ import annotations

import pytest

from pipeline.place_resolution import (
    VILLAGE_NOT_APPLICABLE, Gazetteer, normalize_place, resolve_place,
)


@pytest.fixture
def gaz() -> Gazetteer:
    return Gazetteer(
        districts=["Kancheepuram", "Chengalpattu", "Chennai", "Tiruvallur",
                   "Thiruchirappalli", "Thanjavur", "Thoothukudi", "Salem",
                   "Dharmapuri", "Vellore", "Sivagangai", "Coimbatore"],
        taluks=[
            ("Sriperumbudur", "Kancheepuram"),
            ("Kundrathur", "Kancheepuram"),
            ("Pallavaram", "Chengalpattu"),      # moved here in 2019
            ("Tambaram", "Chengalpattu"),
            ("Thuraiyur", "Thiruchirappalli"),
            ("Poonamallee", "Tiruvallur"),
            ("Katpadi", "Vellore"),
            ("Ambattur", "Chennai"),             # urban: no revenue villages
        ],
        villages=[
            ("Nazarathpettai", "Poonamallee", "Tiruvallur"),
            ("Mookkanur", "Poonamallee", "Tiruvallur"),
            ("Ayyappanthangal", "Sriperumbudur", "Kancheepuram"),
            ("Dharapadavedu", "Katpadi", "Vellore"),
            ("Erukkampattu", "Katpadi", "Vellore"),
            ("Kengarai 1", "Katpadi", "Vellore"),
            ("Kengarai 2", "Katpadi", "Vellore"),
            ("Nallur", "Tambaram", "Chengalpattu"),
            ("Nallur", "Thuraiyur", "Thiruchirappalli"),
        ],
    )


def test_spelling_axes_that_separate_the_two_sources(gaz):
    """The portal and the notice disagree on these, and they are one place."""
    for a, b in [("Kanchipuram", "Kancheepuram"),
                 ("Tiruvallur", "Thiruvallur"),
                 ("Pudukkottai", "Pudukottai"),
                 ("Tiruppur", "Tirupur")]:
        assert normalize_place(a) == normalize_place(b), f"{a!r} vs {b!r}"


def test_tamil_transliteration_alternates_are_one_name():
    """One Tamil letter, two English spellings. Both pairs are single taluks
    written two ways in the corpus and the gazetteer."""
    assert normalize_place("Mannarkudi") == normalize_place("Mannargudi")
    assert normalize_place("Edappadi") == normalize_place("Edappady")


def test_a_leading_initial_is_not_folded_away():
    """G.Pappankulam and K.Pappankulam are two villages in Madurai East. The
    g/k fold applies inside a word only, so the initials survive it."""
    assert normalize_place("G.Pappankulam") != normalize_place("K.Pappankulam")


def test_roman_numeral_sub_villages_stay_apart():
    """Jeyamangalam Bit I and Bit II share a taluk. Left as letters the
    doubled-letter collapse would merge them."""
    assert normalize_place("Jeyamangalam Bit I") != \
        normalize_place("Jeyamangalam Bit II")


def test_qualifier_words_are_not_part_of_the_name(gaz):
    # A notice writes "Sriperumbudur Taluk"; the gazetteer says "Sriperumbudur".
    assert gaz.taluk("Sriperumbudur Taluk") == ("Sriperumbudur", "Kancheepuram")
    assert gaz.district("Vellore District") == "Vellore"


def test_taluk_repairs_a_damaged_district(gaz):
    """Bottom-up, the whole point. The district string is wrong or outdated and
    the taluk beneath it knows better."""
    r = resolve_place(gaz, district="Tiuchirapalli", taluk="Thuraiyur")
    assert r["district"] == "Thiruchirappalli"
    assert r["district_source"] == "taluk"


def test_taluk_settles_the_2019_district_split(gaz):
    """Pallavaram moved from Kancheepuram to Chengalpattu. A notice naming the
    old district is not wrong so much as stale — and resolvable without a
    human, because the taluk sits on one side only."""
    r = resolve_place(gaz, district="Kanchipuram", taluk="Pallavaram")
    assert r["district"] == "Chengalpattu"
    assert r["conflict"] is True          # surfaced, not hidden


def test_district_agreeing_with_its_taluk_is_no_conflict(gaz):
    r = resolve_place(gaz, district="Kanchipuram", taluk="Sriperumbudur",
                      village="Ayyappanthangal")
    assert (r["district"], r["taluk"], r["village"]) == \
        ("Kancheepuram", "Sriperumbudur", "Ayyappanthangal")
    assert r["conflict"] is False
    assert r["village_status"] == "resolved"


def test_historic_names_come_from_the_alias_table_not_similarity(gaz):
    """Edit distance is actively wrong here: asked which district "Trichy" is,
    similarity answers Kallakurichi. These share too few letters to guess."""
    assert gaz.district("Trichy") == "Thiruchirappalli"
    assert gaz.district("Tanjore") == "Thanjavur"
    assert gaz.district("Tuticorin") == "Thoothukudi"


def test_fuzzy_fixes_ocr_damage_in_a_village_name(gaz):
    r = resolve_place(gaz, taluk="Poonamallee", village="Nazarathpet")
    assert r["village"] == "Nazarathpettai"
    r = resolve_place(gaz, taluk="Poonamallee", village="Mockanur")
    assert r["village"] == "Mookkanur"


def test_fuzzy_refuses_a_different_village_in_the_same_taluk(gaz):
    """Murukampattu scores 86 against Erukkampattu — high, and wrong. The
    first-letter guard is what keeps neighbouring villages apart."""
    r = resolve_place(gaz, taluk="Katpadi", village="Murukampattu")
    assert r["village"] is None
    assert r["village_status"] == "unmatched"


def test_numbered_sub_villages_are_distinct_places(gaz):
    """Kengari-2 matched Kengarai 1 at 93 before digits were compared."""
    r = resolve_place(gaz, taluk="Katpadi", village="Kengari-2")
    assert r["village"] == "Kengarai 2"


def test_a_village_is_only_looked_up_inside_its_taluk(gaz):
    """"Nallur" exists 22 times across the state. The taluk decides which."""
    a = resolve_place(gaz, taluk="Tambaram", village="Nallur")
    b = resolve_place(gaz, taluk="Thuraiyur", village="Nallur")
    assert a["village"] == b["village"] == "Nallur"
    assert a["district"] == "Chengalpattu"
    assert b["district"] == "Thiruchirappalli"


def test_a_village_without_a_taluk_is_not_guessed(gaz):
    r = resolve_place(gaz, district="Chengalpattu", village="Nallur")
    assert r["village"] is None
    assert r["village_status"] == "no-parent-taluk"
    assert r["district"] == "Chengalpattu"      # the district still resolves


def test_urban_taluks_report_a_reference_gap_not_a_failure(gaz):
    """All 12 Chennai taluks hold zero revenue villages — the city uses wards.
    Reporting these as unmatched would blame the notice for a gap in the
    gazetteer."""
    r = resolve_place(gaz, taluk="Ambattur", village="Menambedu")
    assert r["village"] is None
    assert r["village_status"] == VILLAGE_NOT_APPLICABLE
    assert r["taluk"] == "Ambattur"


def test_a_village_under_the_neighbouring_taluk_is_still_found(gaz):
    """Taluk boundaries are redrawn more often than district ones. The notice
    puts Ayyappanthangal under Kundrathur; the gazetteer has it under
    Sriperumbudur. Same district, one candidate, so the taluk is corrected."""
    r = resolve_place(gaz, district="Kancheepuram", taluk="Kundrathur",
                      village="Ayyappanthangal")
    assert r["village"] == "Ayyappanthangal"
    assert r["taluk"] == "Sriperumbudur"      # corrected, not rejected
    assert r["village_source"] == "district"


def test_the_district_scan_never_guesses(gaz):
    """Widening the search widens the chance of collision, so the district
    scan is exact-match only — no fuzzy at this scope."""
    r = resolve_place(gaz, district="Tiruvallur", taluk="Katpadi",
                      village="Nazarathpet")
    assert r["village"] is None


def test_a_village_field_holding_a_taluk_name_is_labelled_as_such(gaz):
    """Kundrathur and Madhavaram were villages before they were promoted to
    taluks, and notices still write them in the village field. That is not an
    unmatched village — there is simply no village to match."""
    r = resolve_place(gaz, district="Kancheepuram", taluk="Sriperumbudur",
                      village="Kundrathur")
    assert r["village"] is None
    assert r["village_status"] == "names-a-taluk"


def test_a_village_repeating_its_own_taluk_is_not_fuzzed_into_a_sub_village(gaz):
    """The gazetteer holds "Kundrathur B" inside Kundrathur taluk, and fuzzy
    matched a bare "Kundrathur" to it at 95. The notice named the taluk twice;
    which sub-village it means is unknown, so nothing is guessed."""
    sub = Gazetteer(
        districts=["Kancheepuram"],
        taluks=[("Kundrathur", "Kancheepuram")],
        villages=[("Kundrathur B", "Kundrathur", "Kancheepuram")],
    )
    r = resolve_place(sub, taluk="Kundrathur", village="Kundrathur")
    assert r["village"] is None
    assert r["village_status"] == "names-a-taluk"


def test_a_village_genuinely_sharing_its_taluk_name_still_resolves(gaz):
    """The guard above must not block the real case: some villages do carry
    their taluk's name, and an exact hit is taken before it."""
    same = Gazetteer(
        districts=["Chennai"],
        taluks=[("Madhavaram", "Chennai")],
        villages=[("Madhavaram", "Madhavaram", "Chennai")],
    )
    r = resolve_place(same, taluk="Madhavaram", village="Madhavaram")
    assert r["village"] == "Madhavaram"


def test_one_name_covering_several_villages_in_a_taluk_is_refused():
    """Tiruvallur holds three distinct villages called Karanai, each with its
    own village code. Attaching to whichever came back first would fan the
    property out across all three."""
    dupes = Gazetteer(
        districts=["Tiruvallur"],
        taluks=[("Tiruvallur", "Tiruvallur")],
        villages=[("Karanai", "Tiruvallur", "Tiruvallur"),
                  ("Karanai", "Tiruvallur", "Tiruvallur"),
                  ("Perambakkam", "Tiruvallur", "Tiruvallur")],
    )
    r = resolve_place(dupes, taluk="Tiruvallur", village="Karanai")
    assert r["village"] is None
    assert r["taluk"] == "Tiruvallur"          # the taluk still resolves
    # An unambiguous neighbour in the same taluk is unaffected.
    ok = resolve_place(dupes, taluk="Tiruvallur", village="Perambakkam")
    assert ok["village"] == "Perambakkam"


def test_a_district_name_in_the_taluk_field_still_yields_a_district(gaz):
    """Coimbatore is a district whose taluks are Coimbatore North and South;
    notices write the district name into both fields. A right district beats
    nothing."""
    r = resolve_place(gaz, district=None, taluk="Coimbatore")
    assert r["district"] == "Coimbatore"
    assert r["district_source"] == "taluk-field-names-a-district"
    assert r["taluk"] is None


def test_two_taluks_sharing_a_folded_name_resolve_to_neither(gaz):
    """Tirupathur (Tirupathur district) and Thiruppattur (Sivagangai) fold to
    the same key. Picking one would be a coin flip, so both are refused and
    the notice falls back to its district."""
    ambiguous = Gazetteer(
        districts=["Tirupathur", "Sivagangai"],
        taluks=[("Tirupathur", "Tirupathur"), ("Thiruppattur", "Sivagangai")],
        villages=[],
    )
    r = resolve_place(ambiguous, district="Sivagangai", taluk="Thiruppattur")
    assert r["taluk"] is None
    assert r["district"] == "Sivagangai"
    assert r["district_source"] == "district"


def test_nothing_recognised_resolves_to_nothing(gaz):
    r = resolve_place(gaz, district="Pondicherry", taluk=None, village=None)
    assert r["district"] is None                # out of state, not forced in
    assert r["village_status"] == "absent"
