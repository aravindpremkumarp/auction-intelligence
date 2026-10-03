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


def test_udumalpet_reaches_udumalaipettai_and_its_village():
    """21 listings write the taluk "Udumalpet"; it scores 82 against
    Udumalaipettai, below the fuzzy floor, and 15 of them name a village of
    that taluk — the evidence that earned the alias."""
    from pipeline.place_resolution import TALUK_ALIASES
    assert TALUK_ALIASES["udumalpet"] == "Udumalaipettai"
    local = Gazetteer(
        districts=["Tiruppur"],
        taluks=[("Udumalaipettai", "Tiruppur"), ("Madathukulam", "Tiruppur")],
        villages=[("Kurichikottai", "Udumalaipettai", "Tiruppur")],
    )
    r = resolve_place(local, district="Tiruppur", taluk="Udumalpet",
                      village="Kurichikottai")
    assert (r["taluk"], r["village"], r["village_status"]) == \
        ("Udumalaipettai", "Kurichikottai", "resolved")


def test_no_alias_turns_a_two_taluk_composite_into_one_half():
    """"Mambalam - Guindy" and "Fort - Tondiarpet" name two taluks each;
    compound_taluk settles them to the district, and an alias must not pick."""
    from pipeline.place_resolution import TALUK_ALIASES
    for raw in ("mambalam - guindy", "mambalam guindy", "fort - tondiarpet",
                "fort tondiarpet", "natham pernambut"):
        assert raw not in TALUK_ALIASES


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


def test_a_known_district_holds_the_state_wide_rule_back_entirely():
    """It fires only when the district is unknown too.

    With a district in hand the caller has a narrower, safer search —
    `village_in_district`, which `lot_place` runs next — and firing first would
    pre-empt it: 299 lots came out stamped `state` that the district-scoped rule
    would have placed anyway. Nothing is lost by waiting, because a village both
    unique state-wide AND inside the known district is exactly what that rule
    finds; one in a different district is refused here either way.
    """
    r = resolve_place(_state(), district="Chengalpattu", village="Mookkanur")
    assert r["village_status"] == "no-parent-taluk"
    assert r["district"] == "Chengalpattu"     # kept, not overwritten
    assert r["taluk"] is None                 # never guessed from the village

    # Same even when the unique village agrees with the stated district — this
    # is the district-scoped rule's job, and its provenance.
    r2 = resolve_place(_state(), district="Chengalpattu", village="Madurapakam")
    assert r2["village_source"] != "state"


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


def test_the_state_wide_rule_still_answers_when_no_district_is_known():
    gaz = Gazetteer(
        districts=["Tiruvallur"],
        taluks=[("Poonamallee", "Tiruvallur")],
        villages=[("Mookkanur", "Poonamallee", "Tiruvallur")])
    r = resolve_place(gaz, village="Mookkanur")
    assert r["village_source"] == "state"


# ── matching by sound, against the register's Tamil ─────────────────────────

def _sound_gaz(villages, names_ta):
    from pipeline.place_resolution import Gazetteer
    return Gazetteer(districts=["Chengalpattu"], taluks=[("Chengalpattu", "Chengalpattu")],
                     villages=[(v, "Chengalpattu", "Chengalpattu") for v in villages],
                     village_names_ta=[(v, "Chengalpattu", ta) for v, ta in names_ta])


def test_tamil_read_into_latin_shares_a_sound_key_with_english_spellings():
    from pipeline.place_resolution import sound_key, tamil_latin
    ta = sound_key(tamil_latin("071  செட்டிபுண்ணியம்"))       # register code dropped
    assert ta == sound_key("Chettipunniyam") == sound_key("Chettypunniyam")
    assert sound_key("Alampadi") == sound_key("Alambadi")        # voiced = unvoiced
    assert sound_key("Rajakilpakkam") == sound_key("Rajakizhpakkam")   # zh = l


def test_a_village_is_heard_through_its_tamil_name():
    """19 listings write "Chettipunniyam"; the register holds it as
    Chettypunniyam (செட்டிபுண்ணியம்) beside an LGD copy "Chettipunyam", and the
    two near-twins made every spelling rule refuse."""
    gaz = _sound_gaz(["Chettypunniyam", "Chettipunyam", "Kolavai"],
                     [("Chettypunniyam", "செட்டிபுண்ணியம்"), ("Kolavai", "கொளவாய்")])
    r = resolve_place(gaz, district="Chengalpattu", taluk="Chengalpattu",
                      village="Chettipunniyam")
    assert (r["village"], r["village_status"], r["village_source"]) == \
        ("Chettypunniyam", "resolved", "tamil-sound")


def test_two_villages_a_sound_apart_are_not_guessed_between():
    """Madambakkam and Madapakkam are two real villages; at a narrow margin the
    sound rule read one as the other six times on the live corpus."""
    gaz = _sound_gaz(["Madapakkam", "Madambakkam"],
                     [("Madapakkam", "மாடப்பாக்கம்"), ("Madambakkam", "மாடம்பாக்கம்")])
    assert gaz.village_by_sound("Madambakam", "Chengalpattu") in (None, "Madambakkam")
    assert gaz.village_by_sound("Madapakam", "Chengalpattu") in (None, "Madapakkam")


def test_sound_never_maps_a_numbered_sub_village_onto_the_plain_one():
    gaz = _sound_gaz(["Elavur"], [("Elavur", "எளாவூர்")])
    assert gaz.village_by_sound("Elavur II", "Chengalpattu") is None
    assert gaz.village_by_sound("Elavoor", "Chengalpattu") == "Elavur"


def test_without_tamil_names_the_sound_rule_is_off():
    from pipeline.place_resolution import Gazetteer
    gaz = Gazetteer(districts=["Chengalpattu"], taluks=[("Chengalpattu", "Chengalpattu")],
                    villages=[("Chettypunniyam", "Chengalpattu", "Chengalpattu")])
    assert gaz.village_by_sound("Chettipuniam", "Chengalpattu") is None


# ── Villages the register keeps in parts ─────────────────────────────────────

def _split_gaz(extra=()):
    from pipeline.place_resolution import Gazetteer
    villages = [("Pammal - I", "Pallavaram", "Chengalpattu"),
                ("Pammal - II", "Pallavaram", "Chengalpattu"),
                # an LGD-added copy of part II under another spelling
                ("Pammal-II", "Pallavaram", "Chengalpattu"),
                ("Sevilimedu A", "Pallavaram", "Chengalpattu"),
                ("Sevilimedu - B", "Pallavaram", "Chengalpattu"),
                ("Konerikuppam - A", "Pallavaram", "Chengalpattu"),
                ("Badur R.F.", "Pallavaram", "Chengalpattu"),
                ("Badur", "Pallavaram", "Chengalpattu"), *extra]
    return Gazetteer(districts=["Chengalpattu"], taluks=[("Pallavaram", "Chengalpattu")],
                     villages=villages,
                     village_names_ta=[("Pammal - II", "Pallavaram", "பம்மல் 2")])


def test_the_whole_of_a_split_village_is_linked_to_every_part():
    from pipeline.place_resolution import VILLAGE_ONE_OF_PARTS, resolve_place
    res = resolve_place(_split_gaz(), district="Chengalpattu", taluk="Pallavaram",
                        village="PAMMAL")
    assert res["village"] is None and res["taluk"] == "Pallavaram"
    # one entry per part; the copy with the Tamil name speaks for part II
    assert res["village_parts"] == ["Pammal - I", "Pammal - II"]
    assert (res["village_status"], res["village_source"]) == (VILLAGE_ONE_OF_PARTS, "split-village")
    ab = resolve_place(_split_gaz(), district="Chengalpattu", taluk="Pallavaram",
                       village="Sevilimedu")
    assert ab["village_parts"] == ["Sevilimedu A", "Sevilimedu - B"]


def test_a_notice_naming_the_part_gets_that_part():
    from pipeline.place_resolution import resolve_place
    res = resolve_place(_split_gaz(), district="Chengalpattu", taluk="Pallavaram",
                        village="Pammal-I")
    assert (res["village"], res["village_parts"]) == ("Pammal - I", [])


def test_no_parts_when_the_whole_exists_one_part_exists_or_it_is_no_part():
    from pipeline.place_resolution import resolve_place
    whole = _split_gaz(extra=[("Pammal", "Pallavaram", "Chengalpattu")])
    assert resolve_place(whole, district="Chengalpattu", taluk="Pallavaram",
                         village="Pammal")["village"] == "Pammal"
    lone = resolve_place(_split_gaz(), district="Chengalpattu", taluk="Pallavaram",
                         village="Konnerikuppam")
    assert lone["village_parts"] == []                   # a spelling question, not a split
    # a reserve forest and a village code are not parts
    from pipeline.place_resolution import village_part
    assert village_part("Badur R.F.") is None and village_part("Nelli    (013)") is None
    assert village_part("Maduranthakam Part 1") == ("Maduranthakam", "1")


def test_a_village_split_too_many_ways_is_not_linked():
    from pipeline.place_resolution import MAX_VILLAGE_PARTS, resolve_place
    many = _split_gaz(extra=[(f"Vedaranyam Part - {i}", "Pallavaram", "Chengalpattu")
                             for i in range(1, MAX_VILLAGE_PARTS + 2)])
    assert resolve_place(many, district="Chengalpattu", taluk="Pallavaram",
                         village="Vedaranyam")["village_parts"] == []


# ── Register rows that copy an original ──────────────────────────────────────

def _copy_gaz(linked=True):
    from pipeline.place_resolution import Gazetteer
    villages = [("Arasoor", "Vandavasi", "Tiruvannamalai"),
                ("Kilkodungalur", "Vandavasi", "Tiruvannamalai")]
    copy = [("Arasur", "Vandavasi", "Tiruvannamalai")]
    return Gazetteer(districts=["Tiruvannamalai"], taluks=[("Vandavasi", "Tiruvannamalai")],
                     villages=villages if linked else villages + copy,
                     village_copies=[("Arasur", "Vandavasi", "Arasoor"),
                                     ("Ghost", "Vandavasi", "Nowhere")] if linked else [])


def test_a_copy_spelling_lands_on_the_original():
    from pipeline.place_resolution import resolve_place
    res = resolve_place(_copy_gaz(), district="Tiruvannamalai", taluk="Vandavasi",
                        village="Arasur")
    assert (res["village"], res["village_source"]) == ("Arasoor", "taluk")
    assert _copy_gaz().village_in_district("Arasur", "Tiruvannamalai") == ("Arasoor", "Vandavasi")
    assert _copy_gaz().village_in_state("Arasur") == ("Arasoor", "Vandavasi", "Tiruvannamalai")


def test_a_copy_no_longer_ties_against_its_original():
    """Two spellings of one village used to score within the fuzzy margin of
    each other, and the resolver refused a spelling between them."""
    from pipeline.place_resolution import resolve_place
    kw = dict(district="Tiruvannamalai", taluk="Vandavasi", village="Arasour")
    assert resolve_place(_copy_gaz(linked=False), **kw)["village"] is None
    assert resolve_place(_copy_gaz(), **kw)["village"] == "Arasoor"


def test_a_stale_copy_link_is_ignored():
    from pipeline.place_resolution import resolve_place
    res = resolve_place(_copy_gaz(), district="Tiruvannamalai", taluk="Vandavasi",
                        village="Ghost")
    assert res["village"] is None and res["village_status"] == "unmatched"


# ── Taluk hints: the registration office and the city ────────────────────────

def _hint_gaz():
    from pipeline.place_resolution import Gazetteer
    return Gazetteer(
        districts=["Chengalpattu", "Coimbatore"],
        taluks=[("Chengalpattu", "Chengalpattu"), ("Tambaram", "Chengalpattu"),
                ("Mettupalayam", "Coimbatore")],
        villages=[("Kattankulathur", "Chengalpattu", "Chengalpattu"),
                  ("Nallur", "Chengalpattu", "Chengalpattu"),
                  ("Nallur", "Tambaram", "Chengalpattu"),
                  ("Madambakkam", "Tambaram", "Chengalpattu"),
                  ("Sikkadasampalayam", "Mettupalayam", "Coimbatore")])



def _sros():
    from pipeline.place_resolution import sro_key
    return {sro_key("Chengalpet"): "Chengalpattu", sro_key("Tambaram"): "Tambaram",
            sro_key("Mettupalayam"): "Mettupalayam"}


def _hinted(village, *, district="Chengalpattu", taluk=None, sro=None, city=None):
    from pipeline.place_resolution import resolve_place, taluk_hint_place
    gaz = _hint_gaz()
    res = resolve_place(gaz, district=district, taluk=taluk, village=village)
    return res, taluk_hint_place(gaz, res, village, sro=sro, city=city,
                                 sro_taluks=_sros())


def test_one_office_one_key_whatever_the_suffix():
    from pipeline.place_resolution import sro_key
    assert sro_key("Chengalpet Joint-II SRO") == sro_key("Chengalpet") \
        == sro_key("S.R.O. Chengalpet") == sro_key("Sub Registrar Office, Chengalpet")
    assert sro_key("Joint II") == sro_key(None) == ""


def test_the_sro_names_the_taluk_a_notice_left_out():
    res, out = _hinted("Kattankalathur", sro="Chengalpet Joint-II SRO")
    assert res["village_status"] == "no-parent-taluk"
    assert (out["village"], out["taluk"], out["district"]) == \
        ("Kattankulathur", "Chengalpattu", "Chengalpattu")
    assert (out["village_status"], out["village_source"]) == ("resolved", "sro-taluk")


def test_the_sro_rescues_a_village_the_named_taluk_does_not_hold():
    res, out = _hinted("Kattankalathur", taluk="Tambaram", sro="Chengalpet")
    assert res["village_status"] == "unmatched"
    assert (out["village"], out["taluk"], out["village_source"]) == \
        ("Kattankulathur", "Chengalpattu", "sro-taluk")


def test_the_city_names_the_taluk_when_it_is_the_taluk_town():
    res, out = _hinted("Sikkadasampalaiyam", district="Coimbatore",
                       city="Mettupalayam Town")
    assert (out["village"], out["taluk"], out["village_source"]) == \
        ("Sikkadasampalayam", "Mettupalayam", "city-taluk")


def test_a_hint_outside_the_known_district_is_ignored():
    res, out = _hinted("Sikkadasampalaiyam", sro="Mettupalayam")
    assert res["village_status"] == "no-parent-taluk" and out == res


def test_the_village_must_be_in_the_hinted_taluk_itself():
    """The district-wide search inside resolve_place may find the village in a
    third taluk; that answer is not the hint's."""
    from pipeline.place_resolution import taluk_hint_place
    res = {"district": "Chengalpattu", "taluk": None, "village": None,
           "village_parts": [], "village_status": "no-parent-taluk",
           "village_source": None, "district_source": "district"}
    out = taluk_hint_place(_hint_gaz(), res, "Kattankulathur", sro="Tambaram",
                           sro_taluks=_sros())
    assert out == res


def test_two_hints_naming_different_places_cancel_out():
    # "Nallur" is in both taluks: the SRO says one, the city the other
    res, out = _hinted("Nallur", sro="Chengalpet", city="Tambaram")
    assert res["village_status"] == "no-parent-taluk" and out == res
    # agreeing hints are one answer
    _, agree = _hinted("Nallur", sro="Tambaram", city="Tambaram")
    assert (agree["village"], agree["taluk"]) == ("Nallur", "Tambaram")


def test_a_placed_or_ruled_village_is_never_re_hinted():
    from pipeline.place_resolution import taluk_hint_place
    res, out = _hinted("Madambakkam", taluk="Tambaram", sro="Chengalpet")
    assert res["village"] == "Madambakkam" and out == res
    ruled = {"district": "Chengalpattu", "taluk": None, "village": None,
             "village_parts": [], "village_status": "not-a-revenue-village",
             "village_source": "human-skip"}
    assert taluk_hint_place(_hint_gaz(), ruled, "Kattankalathur", sro="Chengalpet",
                            sro_taluks=_sros()) == ruled


def test_no_table_no_sro_hint(tmp_path):
    from pipeline.place_resolution import load_sro_taluks
    assert load_sro_taluks(tmp_path / "missing.json") == {}
    (tmp_path / "t.json").write_text('{"kenkalpet": {"taluk": "Chengalpattu", "lots": 9}}')
    assert load_sro_taluks(tmp_path / "t.json") == {"kenkalpet": "Chengalpattu"}
    from pipeline.place_resolution import resolve_place, taluk_hint_place
    gaz = _hint_gaz()
    res = resolve_place(gaz, district="Chengalpattu", village="Kattankalathur")
    assert taluk_hint_place(gaz, res, "Kattankalathur", sro="Chengalpet",
                            sro_taluks={}) == res


# ── One village of the district by sound; the PIN code's taluk ───────────────

def _sound_district_gaz():
    from pipeline.place_resolution import Gazetteer
    return Gazetteer(
        districts=["Pudukkottai"],
        taluks=[("Gandarvakottai", "Pudukkottai"), ("Alangudi", "Pudukkottai")],
        villages=[("Meikudipatti", "Gandarvakottai", "Pudukkottai"),
                  ("Kengarai 1", "Gandarvakottai", "Pudukkottai"),
                  ("Badur R.F.", "Alangudi", "Pudukkottai"),
                  ("Nallur", "Gandarvakottai", "Pudukkottai"),
                  ("Nalloor", "Alangudi", "Pudukkottai"),
                  ("Arasoor", "Alangudi", "Pudukkottai")],
        village_copies=[("Arasur", "Alangudi", "Arasoor")])


def test_a_village_the_district_holds_once_by_sound_is_found():
    gaz = _sound_district_gaz()
    assert gaz.village_by_district_sound("Meykudipatti", "Pudukkottai") == \
        ("Meikudipatti", "Gandarvakottai")
    # a copy's spelling sounds for its original
    assert gaz.village_by_district_sound("Arazur", "Pudukkottai") == ("Arasoor", "Alangudi")


def test_the_sound_rule_refuses_twins_numbers_forests_and_exact_names():
    gaz = _sound_district_gaz()
    # "Nallur" / "Nalloor" sound alike in two taluks: no guess
    assert gaz.village_by_district_sound("Nalur", "Pudukkottai") is None
    # another number is another village
    assert gaz.village_by_district_sound("Kengarai-2", "Pudukkottai") is None
    # the village is not its forest
    assert gaz.village_by_district_sound("Baddur", "Pudukkottai") is None
    # an exact name belongs to the exact district search
    assert gaz.village_by_district_sound("Meikudipatti", "Pudukkottai") is None
    assert gaz.village_by_district_sound("Meykudipatti", None) is None


def test_the_sound_rule_only_places_a_notice_that_gave_no_taluk():
    from pipeline.place_resolution import district_sound_place, resolve_place
    gaz = _sound_district_gaz()
    res = resolve_place(gaz, district="Pudukkottai", village="Meykudipatti")
    assert res["village_status"] == "no-parent-taluk"
    out = district_sound_place(gaz, res, "Meykudipatti")
    assert (out["village"], out["taluk"], out["village_status"], out["village_source"]) == \
        ("Meikudipatti", "Gandarvakottai", "resolved", "district-sound")
    # a stated taluk that does not hold it is a different question
    named = resolve_place(gaz, district="Pudukkottai", taluk="Alangudi", village="Meykudipatti")
    assert district_sound_place(gaz, named, "Meykudipatti") == named
    # no district, nowhere to look
    bare = resolve_place(gaz, village="Meykudipatti")
    assert district_sound_place(gaz, bare, "Meykudipatti") == bare


def test_the_property_pin_is_the_one_its_text_gives():
    from pipeline.place_resolution import property_pin
    assert property_pin("Plot 12, Ashok Nagar, Chennai-600083") == "600083"
    assert property_pin("Madambakkam, Chennai 600 126", None) == "600126"
    assert property_pin("near bus stand", "Tambaram") is None
    # two PINs: a neighbour's or an office's is in there; neither is trusted
    assert property_pin("Chennai-600083", "office at Chennai-600001") is None
    # not a Tamil Nadu PIN, and not a survey or phone number
    assert property_pin("Bengaluru 560001, S.No. 612/345, ph 9600012345") is None


def test_the_pin_names_the_taluk_to_look_in():
    res, out = _hinted("Kattankalathur", sro=None)
    from pipeline.place_resolution import taluk_hint_place
    out = taluk_hint_place(_hint_gaz(), res, "Kattankalathur", pin="603203",
                           pin_taluks={"603203": "Chengalpattu"})
    assert (out["village"], out["taluk"], out["village_source"]) == \
        ("Kattankulathur", "Chengalpattu", "pin-taluk")
    # a PIN and an SRO that disagree cancel out
    both = taluk_hint_place(_hint_gaz(), *_nallur_no_taluk(), sro="Chengalpet",
                            sro_taluks=_sros(), pin="600073",
                            pin_taluks={"600073": "Tambaram"})
    assert both["village"] is None


def _nallur_no_taluk():
    from pipeline.place_resolution import resolve_place
    return resolve_place(_hint_gaz(), district="Chengalpattu", village="Nallur"), "Nallur"


def test_a_village_shape_names_what_makes_two_names_two_places():
    from pipeline.place_resolution import village_shape
    assert village_shape("Kengarai 1") != village_shape("Kengarai-2")
    assert village_shape("Badur R.F.") != village_shape("Badur")
    assert village_shape("V. Pudur") != village_shape("Pudur")
    assert village_shape("Padappai (Ct)") != village_shape("Padappai")
    assert village_shape("Meykudipatti") == village_shape("Meikudipatti")


# ── A village field holding more than a name; neighbouring taluks ────────────

def test_a_village_field_is_split_into_the_names_it_holds():
    from pipeline.place_resolution import village_pieces
    assert village_pieces("Kanthalur and Pulipakkam") == ["Kanthalur", "Pulipakkam"]
    assert village_pieces("Ambur Municipal Town") == ["Ambur"]
    assert village_pieces("Kethunaickenpattipudhur Natham") == ["Kethunaickenpattipudhur"]
    # streets, wards and colonies sit inside a village; they are not one
    assert village_pieces("Mathigiri (Kurubatti Ward)") == ["Mathigiri"]
    assert village_pieces("Colachel Revenue Village (Now simon colony)") == \
        ["Colachel Revenue Village"]
    # "A hamlet of B" is B
    assert village_pieces("Kodur Village hamlet of Thadaperumbakkam Village") == \
        ["Thadaperumbakkam Village"]
    # one plain name is left to resolve_place, which has already tried it
    assert village_pieces("Pulipakkam") == []
    assert village_pieces("Pulipakkam Village") == []
    assert village_pieces(None) == []


def _pieces_gaz():
    return Gazetteer(districts=["Chengalpattu"], taluks=[("Chengalpattu", "Chengalpattu")],
                     villages=[("Kanthalur", "Chengalpattu", "Chengalpattu"),
                               ("Pulipakkam", "Chengalpattu", "Chengalpattu")])


def test_every_piece_must_name_the_same_one_village():
    from pipeline.place_resolution import village_pieces_place
    gaz = _pieces_gaz()

    def place(village):
        res = resolve_place(gaz, taluk="Chengalpattu", village=village)
        return village_pieces_place(gaz, res, village)

    out = place("Pulipakkam Village Natham")
    assert (out["village"], out["village_status"], out["village_source"]) == \
        ("Pulipakkam", "resolved", "village-pieces")
    assert place("Pulipakkam (Anna Nagar)")["village"] == "Pulipakkam"
    # two villages are two answers
    assert place("Kanthalur and Pulipakkam")["village"] is None
    # a piece that matches nothing may be the real village, misspelt
    assert place("Pulipakkam, Sporal")["village"] is None
    # only a village the notice's own taluk does not hold as written
    bare = resolve_place(gaz, district="Chengalpattu", village="Pulipakkam Natham")
    assert village_pieces_place(gaz, bare, "Pulipakkam Natham") == bare


def _neighbour_gaz():
    return Gazetteer(
        districts=["Kancheepuram", "Chengalpattu"],
        taluks=[("Sriperumbudur", "Kancheepuram"), ("Kundrathur", "Kancheepuram"),
                ("Walajabad", "Kancheepuram"), ("Vandalur", "Chengalpattu")],
        villages=[("Irungattukottai", "Sriperumbudur", "Kancheepuram"),
                  ("Varatharajapuram", "Kundrathur", "Kancheepuram"),
                  ("Patapai", "Kundrathur", "Kancheepuram"),
                  ("Padappai (Ct)", "Kundrathur", "Kancheepuram"),
                  ("Madambakkam (Ct)", "Kundrathur", "Kancheepuram"),
                  ("Nallur", "Kundrathur", "Kancheepuram"),
                  ("Nallur", "Walajabad", "Kancheepuram"),
                  ("Adhanur", "Vandalur", "Chengalpattu")],
        village_names_ta=[("Patapai", "Kundrathur", "படப்பை")])


_NEIGHBOURS = {"Sriperumbudur": ("Kundrathur", "Vandalur", "Walajabad"),
               "Kundrathur": ("Sriperumbudur",), "Walajabad": ("Sriperumbudur",),
               "Vandalur": ("Sriperumbudur",)}


def _from_sriperumbudur(village):
    from pipeline.place_resolution import neighbour_taluk_place
    gaz = _neighbour_gaz()
    res = resolve_place(gaz, taluk="Sriperumbudur", village=village)
    return res, neighbour_taluk_place(gaz, res, village, _NEIGHBOURS)


def test_a_village_filed_under_the_taluk_it_was_split_from_is_found_next_door():
    res, out = _from_sriperumbudur("Varadarajapuram")
    assert res["village_status"] == "unmatched"
    assert (out["village"], out["taluk"], out["district"], out["village_status"],
            out["village_source"]) == \
        ("Varatharajapuram", "Kundrathur", "Kancheepuram", "resolved", "neighbour-taluk")


def test_the_neighbour_rule_refuses_ties_census_towns_and_other_districts():
    # two neighbours both hold a Nallur
    assert _from_sriperumbudur("Nallur")[1]["village"] is None
    # "Padappai" sounds like Patapai, but the taluk also holds "Padappai (Ct)"
    res, out = _from_sriperumbudur("Padappai")
    assert out == res
    # the one match a census-town row
    res, out = _from_sriperumbudur("Madambakam")
    assert res["village_status"] == "unmatched" and out == res
    # a neighbour in another district is not this rule's
    assert _from_sriperumbudur("Adhanur")[1]["village"] is None


def test_the_neighbour_rule_only_places_an_unmatched_village():
    from pipeline.place_resolution import neighbour_taluk_place
    gaz = _neighbour_gaz()
    placed = resolve_place(gaz, taluk="Sriperumbudur", village="Irungattukottai")
    assert neighbour_taluk_place(gaz, placed, "Irungattukottai", _NEIGHBOURS) == placed
    bare = resolve_place(gaz, district="Kancheepuram", village="Varadarajapuram")
    assert neighbour_taluk_place(gaz, bare, "Varadarajapuram", _NEIGHBOURS) == bare
    res = resolve_place(gaz, taluk="Sriperumbudur", village="Varadarajapuram")
    assert neighbour_taluk_place(gaz, res, "Varadarajapuram", {}) == res


def test_a_census_town_twin_is_the_ct_row_or_its_plain_name():
    gaz = _neighbour_gaz()
    assert gaz.census_town_twin("Padappai (Ct)", "Kundrathur")
    assert gaz.census_town_twin("padappai", "Kundrathur")
    assert not gaz.census_town_twin("Patapai", "Kundrathur")
    assert not gaz.census_town_twin("Padappai", "Walajabad")


def test_no_table_no_neighbours(tmp_path):
    from pipeline.place_resolution import load_taluk_neighbours
    assert load_taluk_neighbours(tmp_path / "missing.json") == {}
    table = tmp_path / "n.json"
    table.write_text('{"Sriperumbudur": {"Kundrathur": 80, "Alandur": 3}}')
    assert load_taluk_neighbours(table) == {"Sriperumbudur": ("Alandur", "Kundrathur")}


# ── Towns: the urban register ────────────────────────────────────────────────

_TOWNS = {
    "1": {"name": "Attur", "type": "Municipality", "district": "Salem",
          "taluks": ["Attur"], "wards": 33},
    "2": {"name": "Pernambut", "type": "Municipality", "district": "Vellore",
          "taluks": ["Gudiyatham", "Pernambut"], "wards": 21},
    "3": {"name": "Arani", "type": "Town Panchayat", "district": "Thiruvallur",
          "taluks": ["Ponneri"], "wards": 15},
    "4": {"name": "Arani", "type": "Municipality", "district": "Tiruvannamalai",
          "taluks": ["Arani"], "wards": 18},
    "5": {"name": "Natham", "type": "Town Panchayat", "district": "Dindigul",
          "taluks": ["Natham"], "wards": 18},
    "6": {"name": "Erode", "type": "Municipal Corporation", "district": "Erode",
          "taluks": ["Erode"], "wards": 60},
}


def _town_gaz():
    from pipeline.place_resolution import Gazetteer
    return Gazetteer(
        districts=["Salem", "Vellore", "Tiruvallur", "Tiruvannamalai",
                   "Dindigul", "Erode"],
        taluks=[("Attur", "Salem"), ("Gangavalli", "Salem"),
                ("Gudiyatham", "Vellore"), ("Pernambut", "Vellore"),
                ("Ponneri", "Tiruvallur"), ("Arani", "Tiruvannamalai"),
                ("Natham", "Dindigul"), ("Athoor", "Dindigul"),
                ("Erode", "Erode"), ("Bhavani", "Erode")],
        villages=[("Kothampadi", "Attur", "Salem"),
                  ("Pillaiyar Natham", "Athoor", "Dindigul")])


def _bare(district=None, status="absent"):
    return {"district": district, "taluk": None, "village": None,
            "village_parts": [], "village_status": status,
            "village_source": None, "district_source": "district" if district else None}


def test_a_town_called_a_town_gives_its_one_taluk():
    from pipeline.place_resolution import place_level, town_place
    out = town_place(_town_gaz(), _bare("Salem"), None, _TOWNS,
                     "Old S.No. 486/5, Attur Town, Ward No. 4, Block 12")
    assert (out["town"], out["town_type"], out["taluk"], out["taluk_source"]) == \
        ("Attur", "Municipality", "Attur", "lgd-town")
    assert place_level(out) == "town"


def test_a_town_name_without_the_town_word_is_not_read():
    """Most town names are also a taluk's or a district's, and every notice
    names those for its registration office: "Attur Taluk", "Erode District"."""
    from pipeline.place_resolution import town_place
    gaz = _town_gaz()
    for text in ("Kothampadi Village, Attur Taluk", "Erode Registration District",
                 "Sub Registrar Office, Attur"):
        res = _bare("Salem" if "Attur" in text else "Erode")
        assert town_place(gaz, res, None, _TOWNS, text) == res


def test_town_and_country_planning_is_not_a_town():
    from pipeline.place_resolution import town_place
    res = _bare("Erode")
    assert town_place(_town_gaz(), res, None, _TOWNS,
                      "layout approved by Erode Town and Country Planning") == res


def test_a_village_named_after_a_town_is_not_the_town():
    """normalize_place drops "village", so "Natham Village" folded to "Natham"."""
    from pipeline.place_resolution import town_place
    res = _bare("Dindigul")
    assert town_place(_town_gaz(), res, None, _TOWNS,
                      "Pillaiyar Natham Village Municipality Workers Colony") == res


def test_a_town_outside_the_known_district_or_taluk_is_not_this_property():
    from pipeline.place_resolution import town_place
    gaz = _town_gaz()
    res = _bare("Erode")
    assert town_place(gaz, res, None, _TOWNS, "Attur Town") == res
    in_taluk = {**_bare("Salem"), "taluk": "Gangavalli"}
    assert town_place(gaz, in_taluk, None, _TOWNS, "Attur Town") == in_taluk


def test_a_name_two_towns_share_needs_the_district():
    from pipeline.place_resolution import town_place
    gaz = _town_gaz()
    res = _bare()
    assert town_place(gaz, res, None, _TOWNS, "Arani Town") == res
    out = town_place(gaz, _bare("Tiruvannamalai"), None, _TOWNS, "Arani Town")
    assert (out["town"], out["taluk"]) == ("Arani", "Arani")


def test_with_no_district_a_town_word_still_places_it():
    from pipeline.place_resolution import town_place
    out = town_place(_town_gaz(), _bare(), None, _TOWNS, "Attur Municipality limits")
    assert (out["district"], out["taluk"], out["district_source"]) == \
        ("Salem", "Attur", "lgd-town")


def test_a_town_across_two_taluks_labels_but_does_not_choose():
    from pipeline.place_resolution import place_level, town_place
    out = town_place(_town_gaz(), _bare("Vellore"), None, _TOWNS, "Pernambut Town")
    assert out["town"] == "Pernambut" and out["taluk"] is None
    assert "taluk_source" not in out and place_level(out) == "town"


def test_two_towns_named_is_two_answers():
    from pipeline.place_resolution import town_place
    towns = {**_TOWNS, "7": {"name": "Gangavalli", "type": "Town Panchayat",
                             "district": "Salem", "taluks": ["Gangavalli"]}}
    res = _bare("Salem")
    assert town_place(_town_gaz(), res, None, towns,
                      "Attur Town", "Gangavalli Town Panchayat") == res


def test_the_village_is_looked_for_inside_the_town_taluk():
    from pipeline.place_resolution import town_place
    res = _bare("Salem", status="no-parent-taluk")
    out = town_place(_town_gaz(), res, "Kothampadi", _TOWNS, "Kothampadi, Attur Town")
    assert (out["village"], out["village_source"], out["taluk"]) == \
        ("Kothampadi", "lgd-town", "Attur")


def test_a_placed_village_or_an_out_of_state_property_is_left_alone():
    from pipeline.place_resolution import OUTSIDE_TAMIL_NADU, town_place
    gaz = _town_gaz()
    placed = {**_bare("Salem"), "taluk": "Attur", "village": "Kothampadi",
              "village_status": "resolved"}
    assert town_place(gaz, placed, None, _TOWNS, "Attur Town") == placed
    away = _bare(status=OUTSIDE_TAMIL_NADU)
    assert town_place(gaz, away, None, _TOWNS, "Attur Town") == away
    assert town_place(gaz, _bare("Salem"), None, {}, "Attur Town") == _bare("Salem")


def test_place_level_is_the_finest_place_known():
    from pipeline.place_resolution import place_level
    assert place_level({"village_parts": ["Pammal - I", "Pammal - II"]}) == "village"
    assert place_level({"town": "Attur", "taluk": "Attur"}) == "town"
    assert place_level({"taluk": "Attur", "district": "Salem"}) == "taluk"
    assert place_level({"district": "Salem"}) == "district"
    assert place_level({}) is None


def test_no_town_register_no_towns(tmp_path):
    from pipeline.place_resolution import load_towns
    assert load_towns(tmp_path / "missing.json") == {}


def test_the_two_pin_tables_merge_and_a_disagreement_drops_the_pin(tmp_path):
    from pipeline.place_resolution import load_pin_taluks
    learned, posted = tmp_path / "learned.json", tmp_path / "post.json"
    learned.write_text('{"603203": {"taluk": "Chengalpattu"}, '
                       '"602025": {"taluk": "Sriperumbudur"}}')
    posted.write_text('{"605701": {"taluk": "Vridhachalam"}, '
                      '"603203": {"taluk": "Chengalpattu"}, '
                      '"602025": {"taluk": "Tiruvallur"}}')
    assert load_pin_taluks(learned, india_post=posted) == {
        "603203": "Chengalpattu", "605701": "Vridhachalam"}
    assert load_pin_taluks(tmp_path / "none.json",
                           india_post=tmp_path / "none2.json") == {}
