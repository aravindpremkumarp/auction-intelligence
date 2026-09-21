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
                   "Dharmapuri", "Vellore", "Sivagangai", "Coimbatore",
                   "Thiruvarur", "Cuddalore", "Tiruppur"],
        taluks=[
            ("Sriperumbudur", "Kancheepuram"),
            ("Kundrathur", "Kancheepuram"),
            ("Kudavasal", "Thiruvarur"),
            ("Vridhachalam", "Cuddalore"),
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
            ("Manavalanallur", "Kudavasal", "Thiruvarur"),
            ("Manavalanallur", "Vridhachalam", "Cuddalore"),
        ],
    )


def test_spelling_axes_that_separate_the_two_sources(gaz):
    """The portal and the notice disagree on these, and they are one place."""
    for a, b in [("Kanchipuram", "Kancheepuram"),
                 ("Tiruvallur", "Thiruvallur"),
                 ("Pudukkottai", "Pudukottai"),
                 ("Tiruppur", "Tirupur")]:
        assert normalize_place(a) == normalize_place(b), f"{a!r} vs {b!r}"


def test_a_chengalpattu_spelling_the_fuzzy_floor_cannot_reach(gaz):
    """Four live notices write the district this way. Similarity gets nowhere
    near "Chengalpattu" from them, and no other district is a candidate."""
    for raw in ["Chenglepet", "Chengalpeta", "Chengalput", "Chengpaltu"]:
        assert gaz.district(raw) == "Chengalpattu", raw


def test_the_composite_district_is_not_aliased(gaz):
    """"Chengalpattu MGR" is the pre-1997 district that became Kancheepuram
    and Tiruvallur. It is not today's Chengalpattu, so it must not resolve to
    it — a wrong district is worse than a missing one."""
    assert gaz.district("Chengalpattu MGR") != "Chengalpattu"


def test_a_taluk_alias_beats_the_fuzzy_floor(gaz):
    """"Kodavasal" scores 88.9 against "Kudavasal" and misses FUZZY_MIN by 1.1.
    Without the alias the district falls back to the notice's own district
    string, which on the live listing said Thiruvallur — a different district
    from Thiruvarur, where the taluk actually sits."""
    assert gaz.taluk("Kodavasal") == ("Kudavasal", "Thiruvarur")
    r = resolve_place(gaz, district="Thiruvallur", taluk="Kodavasal",
                      village="Manavalanallur")
    assert r["district"] == "Thiruvarur"
    assert r["district_source"] == "taluk"
    # The taluk also places the village, which is ambiguous on its own: there
    # is a second Manavalanallur in Vridhachalam, Cuddalore.
    assert r["village"] == "Manavalanallur"
    assert r["village_status"] == "resolved"
    # ...and the notice disagreeing with its own taluk is still surfaced.
    assert r["conflict"] is True


def test_an_alias_naming_an_unknown_taluk_falls_through(gaz):
    """A stale entry must degrade to today's behaviour, not blank a taluk that
    would otherwise have matched."""
    from pipeline.place_resolution import TALUK_ALIASES
    TALUK_ALIASES["tambaramm"] = "Not A Taluk"
    try:
        assert gaz.taluk("Tambaramm") == ("Tambaram", "Chengalpattu")
    finally:
        del TALUK_ALIASES["tambaramm"]


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
    """Pondicherry is not forced into a Tamil Nadu district, and now says why.

    This asserted "absent" before there was a status for being out of state,
    which read the same as a notice that simply named no village. They are not
    the same finding: one is a gap worth chasing, the other is a property this
    gazetteer will never hold.
    """
    r = resolve_place(gaz, district="Pondicherry", taluk=None, village=None)
    assert r["district"] is None                # out of state, not forced in
    assert r["village_status"] == "outside-tamil-nadu"


def test_a_village_with_no_name_is_absent_not_out_of_state(gaz):
    """The status that test used to assert, on a case that really is absent —
    so the distinction stays covered from both sides."""
    r = resolve_place(gaz, district="Kancheepuram", taluk=None, village=None)
    assert r["village_status"] == "absent"


def test_a_harvested_taluk_spelling_reaches_its_gazetteer_name():
    """63 listings write the taluk "Chengalpet". Similarity cannot reach
    "Chengalpattu" from it, and the village beside it on those notices is a
    real village of that taluk — which is what earns the alias its place."""
    from pipeline.place_resolution import TALUK_ALIASES
    assert TALUK_ALIASES["chengalpet"] == "Chengalpattu"
    local = Gazetteer(
        districts=["Chengalpattu"],
        taluks=[("Chengalpattu", "Chengalpattu")],
        villages=[("Chettipunniyam", "Chengalpattu", "Chengalpattu")],
    )
    r = resolve_place(local, district="Kancheepuram", taluk="Chengalpet",
                      village="Chettipunniyam")
    assert r["taluk"] == "Chengalpattu"
    assert r["village"] == "Chettipunniyam"
    assert r["village_status"] == "resolved"


def test_no_alias_names_a_taluk_the_district_alone_can_settle():
    """Tirupathur and Thiruppattur fold to one key, so a global alias naming
    either would misfile every listing that meant the other. 14 listings spell
    it one of these ways and they stay in the review queue on purpose."""
    from pipeline.place_resolution import TALUK_ALIASES
    folded = normalize_place("Tirupattur")
    assert not [raw for raw in TALUK_ALIASES if normalize_place(raw) == folded]
    assert not [t for t in TALUK_ALIASES.values() if normalize_place(t) == folded]


def test_one_folded_spelling_never_names_two_taluks():
    """Several raw spellings fold to the same key ("Sriperumpudur" and
    "Sripurumbudur" both reach `sriperumbudur`). They must agree on the taluk,
    or the table's answer depends on dict order."""
    from pipeline.place_resolution import TALUK_ALIASES
    seen: dict[str, str] = {}
    for raw, official in TALUK_ALIASES.items():
        key = normalize_place(raw)
        assert seen.setdefault(key, official) == official, raw


def test_the_registration_district_places_a_lot_the_revenue_fields_cannot(gaz):
    """216 lots quote only the SRO district and leave the revenue hierarchy
    unwritten. Believing that field at district level is the difference
    between a lot that can be searched and one that cannot."""
    r = resolve_place(gaz, registration_district="Coimbatore",
                      village="Kavundampalayam")
    assert r["district"] == "Coimbatore"
    assert r["district_source"] == "registration-district"


def test_an_sro_district_the_alias_table_already_knows(gaz):
    """Most of these values name a taluk rather than a district, and the
    commonest of them are already in DISTRICT_ALIASES for the revenue side —
    "Chidambaram", "Tindivanam", "Palani", "Karaikudi". Reading the field at
    all is the whole change; the alias table does the rest."""
    r = resolve_place(gaz, registration_district="Chidambaram")
    assert r["district"] == "Cuddalore"
    assert r["district_source"] == "registration-district"


def test_an_sro_district_that_names_a_taluk_no_alias_covers():
    """"Cheranmahadevi" and "Salem West" are SRO districts on live notices
    with no district alias. They are taluks, and a taluk carries its district,
    so the taluk lookup catches what the alias table does not."""
    local = Gazetteer(
        districts=["Tirunelveli"],
        taluks=[("Cheranmahadevi", "Tirunelveli")],
        villages=[],
    )
    r = resolve_place(local, registration_district="Cheranmahadevi")
    assert r["district"] == "Tirunelveli"
    assert r["district_source"] == "registration-district-names-a-taluk"


def test_the_sro_taluk_itself_is_never_recorded(gaz):
    """Registration and revenue divisions do not share boundaries: the office
    named Vridhachalam serves land outside Vridhachalam taluk. Its district is
    reliable, its taluk is a guess — so the village stays unplaced rather than
    being looked up under a taluk nobody stated."""
    r = resolve_place(gaz, registration_district="Vridhachalam",
                      village="Manavalanallur")
    assert r["district"] == "Cuddalore"
    assert r["taluk"] is None
    assert r["village"] is None
    assert r["village_status"] == "no-parent-taluk"


def test_the_revenue_fields_always_outrank_the_registration_one(gaz):
    """The SRO district is the weakest source in the resolver. It is consulted
    only when the revenue fields reach nothing — never to overrule a taluk
    that resolved, even when the two name different districts."""
    r = resolve_place(gaz, taluk="Katpadi", village="Dharapadavedu",
                      registration_district="Coimbatore")
    assert r["district"] == "Vellore"
    assert r["district_source"] == "taluk"
    assert r["village"] == "Dharapadavedu"


def test_an_out_of_state_sro_district_is_still_refused(gaz):
    """"Puducherry" is a registration district on live notices and is not in
    Tamil Nadu. A weaker source is not a looser one — an unknown name resolves
    to nothing, exactly as it does on the revenue side."""
    r = resolve_place(gaz, registration_district="Puducherry",
                      village="Thirubhuvanai")
    assert r["district"] is None
    assert r["district_source"] is None


# ── out of state, and compound taluk strings ─────────────────────────────────

def test_the_state_field_alone_rules_a_property_out(gaz):
    """A notice that says Kerala is not describing Tamil Nadu, whatever its
    other fields say. 46 lots state a non-TN state outright."""
    r = resolve_place(gaz, district="Ernakulam", village="Kottuvally",
                      state="Kerala")
    assert r["village_status"] == "outside-tamil-nadu"
    assert r["district"] is None


def test_a_kerala_district_is_recognised_without_a_state_field(gaz):
    """Kerala borders three Tamil Nadu districts and the same banks auction on
    both sides, so most out-of-state notices name only the district — 115 lots
    of them."""
    r = resolve_place(gaz, district="Thiruvananthapuram", village="Kazhakuttam")
    assert r["village_status"] == "outside-tamil-nadu"


def test_a_misspelt_tamil_nadu_district_is_never_sent_out_of_state(gaz):
    """The guard is a list, not "the gazetteer could not map it". Of the 242
    unmappable district strings in the corpus, "Trichirapalli" is
    Tiruchirappalli and "Thiruvurur" is Thiruvarur — filing those abroad would
    be worse than leaving them unresolved."""
    for spelling in ("Trichirapalli", "Thiruvurur", "Tiuchirapalli"):
        r = resolve_place(gaz, district=spelling)
        assert r["village_status"] != "outside-tamil-nadu", spelling


def test_a_tamil_nadu_district_that_resolves_is_never_sent_out_of_state(gaz):
    """Belt and braces: a district the gazetteer places is a Tamil Nadu
    district, so the list is not even consulted for it."""
    r = resolve_place(gaz, district="Kancheepuram", state="Tamil Nadu")
    assert r["village_status"] != "outside-tamil-nadu"
    assert r["district"] == "Kancheepuram"


def _chennai() -> Gazetteer:
    return Gazetteer(
        districts=["Chennai", "Kancheepuram"],
        taluks=[("Egmore", "Chennai"), ("Mylapore", "Chennai"),
                ("Perambur", "Chennai"), ("Purasaivakkam", "Chennai"),
                ("Mambalam", "Chennai"), ("Guindy", "Chennai"),
                ("Sriperumbudur", "Kancheepuram"),
                ("Kundrathur", "Kancheepuram")],
        villages=[])


def test_an_old_composite_taluk_name_resolves_to_its_surviving_half():
    """"Egmore-Nungambakkam" is how notices still write the old Chennai
    composite. Neither half is the whole string, so the plain lookup finds
    nothing and the lot loses its geography to a hyphen."""
    r = resolve_place(_chennai(), taluk="Egmore-Nungambakkam")
    assert (r["taluk"], r["district"]) == ("Egmore", "Chennai")


def test_a_renamed_taluk_resolves_to_the_name_that_is_current():
    """"Sriperumbudur Taluk, Now Kundrathur Taluk" names both, and the
    gazetteer holds today's register — so the taluk is Kundrathur. Taking the
    first half would file the land under the name it no longer has."""
    r = resolve_place(_chennai(), taluk="Sriperumbudur Taluk, Now Kundrathur Taluk")
    assert r["taluk"] == "Kundrathur"
    r2 = resolve_place(_chennai(), taluk="Sriperumbudur / New Kundrathur")
    assert r2["taluk"] == "Kundrathur"


def test_two_real_taluks_in_one_string_keep_the_district_and_refuse_the_taluk():
    """"Perambur-Purasawalkam" is two real Chennai taluks. Picking one is a coin
    flip, so neither is taken — but they agree on Chennai, and that much lets
    the village be searched district-wide instead of not at all."""
    r = resolve_place(_chennai(), taluk="Perambur-Purasawalkam")
    assert r["taluk"] is None
    assert r["district"] == "Chennai"
    assert r["district_source"] == "compound-taluk-field"


def test_a_compound_naming_taluks_in_two_districts_yields_nothing():
    """Without a shared district there is nothing the two halves agree on."""
    r = resolve_place(_chennai(), taluk="Egmore-Sriperumbudur")
    assert r["taluk"] is None
    assert r["district"] is None


def test_an_ordinary_hyphenated_taluk_is_not_split_into_nonsense():
    """The split only runs after the whole string fails, so a real name
    containing a separator is never taken apart."""
    gaz = Gazetteer(districts=["Tiruvallur"], taluks=[("R.K. Pet", "Tiruvallur")])
    assert resolve_place(gaz, taluk="R.K. Pet")["taluk"] == "R.K. Pet"


# ── the state-wide last resort ───────────────────────────────────────────────

def _state() -> Gazetteer:
    return Gazetteer(
        districts=["Tiruvallur", "Kancheepuram", "Chengalpattu"],
        taluks=[("Poonamallee", "Tiruvallur"), ("Kundrathur", "Kancheepuram"),
                ("Tambaram", "Chengalpattu")],
        villages=[
            ("Mookkanur", "Poonamallee", "Tiruvallur"),        # unique
            ("Madurapakam", "Tambaram", "Chengalpattu"),       # unique
            # The near-twin pair: one letter apart, two districts.
            ("Varadharajapuram", "Poonamallee", "Tiruvallur"),
            ("Varadarajapuram", "Kundrathur", "Kancheepuram"),
        ])


def test_a_village_only_one_place_in_the_state_bears_names_its_own_taluk():
    """With no taluk there is nowhere to look a village up, and 920 lots end
    there. A name borne by exactly one village carries its taluk and district
    the same way a taluk carries its district."""
    r = resolve_place(_state(), village="Mookkanur")
    assert (r["village"], r["taluk"], r["district"]) == \
        ("Mookkanur", "Poonamallee", "Tiruvallur")
    assert r["village_status"] == "resolved"
    assert r["village_source"] == "state"      # separable from every other path


def test_a_name_with_a_near_twin_in_another_district_is_refused():
    """Varadharajapuram has exactly one exact-fold hit, in Poonamallee — and the
    notices naming it mean Kundrathur's Varadarajapuram, one letter away in
    another district. Exact uniqueness alone got these wrong every time; the
    near-twin check is what took the rule from 97.0% to 99.9%."""
    r = resolve_place(_state(), village="Varadharajapuram")
    assert r["village_status"] == "no-parent-taluk"
    assert r["taluk"] is None


def test_a_district_the_notice_stated_outranks_a_unique_name():
    """A lone unique name does not get to overrule a district the notice gave:
    that would be the wrong-district failure guarded against everywhere else."""
    r = resolve_place(_state(), district="Chengalpattu", village="Mookkanur")
    assert r["village_status"] != "resolved"
    assert r["district"] == "Chengalpattu"     # kept, not overwritten


def test_a_unique_name_agreeing_with_the_stated_district_is_taken():
    r = resolve_place(_state(), district="Chengalpattu", village="Madurapakam")
    assert (r["village"], r["taluk"]) == ("Madurapakam", "Tambaram")
    assert r["village_status"] == "resolved"


def test_the_state_wide_search_never_runs_when_a_taluk_is_known():
    """It is the LAST resort. With a taluk the ordinary scoped lookup answers,
    and a village absent from that taluk stays unmatched rather than being
    rehomed across the state."""
    r = resolve_place(_state(), taluk="Tambaram", village="Mookkanur")
    assert r["village_status"] == "unmatched"
    assert r["taluk"] == "Tambaram"


def test_the_incoming_name_is_matched_exactly_not_fuzzily():
    """Fuzzy at this scope would widen the search and the collision risk
    together — the reason village_in_district refuses it at a narrower scope.

    "Exactly" means on the fold, which is the resolver's idea of the same name:
    `Mookkanurr` is the same key and still matches, while `Mookkanpur` is a
    different name that similarity could otherwise have reached.
    """
    assert _state().village_in_state("Mookkanpur") is None
    assert _state().village_in_state("Mookanoor") is None
    assert _state().village_in_state("Mookkanurr") is not None   # same fold
    assert _state().village_in_state("Mookkanur") is not None
