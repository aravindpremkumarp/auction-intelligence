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
    r = resolve_place(gaz, district="Pondicherry", taluk=None, village=None)
    assert r["district"] is None                # out of state, not forced in
    # Both things are true of this row — no village was named, and the
    # property is in another state — and the second is the one a reviewer can
    # act on: there is nothing to find, so nothing to queue. It is reported
    # ahead of "absent" for that reason.
    assert r["village_status"] == "outside-tamil-nadu"


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


# ── the three ways to earn a taluk the notice did not state ──────────────────
#
# Every string below came off a lot that sat in `no-parent-taluk`: a village
# named, no taluk to place it in, and 1,411 of them in the corpus.


@pytest.fixture
def split_gaz() -> Gazetteer:
    """A gazetteer holding the two shapes these rules turn on: a taluk the
    state has split, and a district that keeps no revenue villages."""
    return Gazetteer(
        districts=["Tiruppur", "Coimbatore", "Chennai", "Cuddalore",
                   "Villupuram"],
        taluks=[
            ("Tiruppur North", "Tiruppur"),
            ("Tiruppur South", "Tiruppur"),
            ("Coimbatore North", "Coimbatore"),
            ("Coimbatore South", "Coimbatore"),
            ("Chidambaram", "Cuddalore"),
            ("Vanur", "Villupuram"),
            # Chennai keeps both shapes: city taluks with no revenue villages,
            # which the gazetteer does not hold at all, and outer ones that do.
            ("Sholinganallur", "Chennai"),
        ],
        villages=[
            ("Perumbakkam", "Sholinganallur", "Chennai"),
            ("Kanakkampalayam", "Tiruppur North", "Tiruppur"),
            ("Velampalayam", "Tiruppur North", "Tiruppur"),
            ("Thiruppaninatham", "Chidambaram", "Cuddalore"),
            ("Pulichapallam", "Vanur", "Villupuram"),
            # One name, two taluks of one district — the tie the rules refuse.
            ("Agaram", "Tiruppur North", "Tiruppur"),
            ("Agaram", "Tiruppur South", "Tiruppur"),
        ],
    )


def test_the_sub_registrars_office_names_the_taluk(split_gaz):
    """"Villupuram R.D, Vanur SRD at Pulichapallam Village" — the notice states
    the registration hierarchy and never the revenue one. 734 lots read this
    way: no taluk string at all, a Sub-Registrar's Office right there."""
    r = resolve_place(split_gaz, village="Pulichapallam",
                      sub_registrar="Vanur")
    assert r["taluk"] == "Vanur"
    assert r["district"] == "Villupuram"
    assert r["village"] == "Pulichapallam"
    assert r["village_status"] == "resolved"
    assert r["district_source"] == "sub-registrar"


def test_the_office_bookkeeping_around_the_place_name_is_ignored(split_gaz):
    """SROs are numbered where one town has several. "Chidambaram Joint -II"
    is still the office at Chidambaram."""
    r = resolve_place(split_gaz, village="Thirupaninatham",
                      sub_registrar="Chidambaram Joint -II")
    assert r["taluk"] == "Chidambaram"
    assert r["village"] == "Thiruppaninatham"      # reached by the fuzzy floor


def test_a_sub_registrar_without_its_village_is_refused(split_gaz):
    """Registration and revenue are two hierarchies that merely share names.
    The village is the evidence; without it the coincidence proves nothing."""
    r = resolve_place(split_gaz, village="Nowhere At All",
                      sub_registrar="Vanur")
    assert r["taluk"] is None
    assert r["village_status"] == "no-parent-taluk"


def test_a_split_taluk_is_chosen_by_its_village(split_gaz):
    """51 lots write "Coimbatore" where the gazetteer holds North and South.
    The village says which half — and nothing else can."""
    r = resolve_place(split_gaz, district="Coimbatore", taluk="Tirupur",
                      village="Kanakkampalayam")
    assert r["taluk"] == "Tiruppur North"
    assert r["district"] == "Tiruppur"
    assert r["village"] == "Kanakkampalayam"
    assert r["village_status"] == "resolved"


def test_a_split_taluk_whose_siblings_both_hold_the_village_is_refused(
        split_gaz):
    """Agaram sits in both halves of Tiruppur. Choosing between them is a coin
    flip, so the lot stays in the queue."""
    r = resolve_place(split_gaz, taluk="Tiruppur", village="Agaram")
    assert r["taluk"] is None
    assert r["village"] is None


def test_a_village_carried_by_one_village_in_the_state_places_itself(
        split_gaz):
    """The last resort, for a notice naming no parent at all."""
    r = resolve_place(split_gaz, village="Velampalayam")
    assert r["village"] == "Velampalayam"
    assert r["taluk"] == "Tiruppur North"
    assert r["district"] == "Tiruppur"
    assert r["village_source"] == "state"


def test_a_shared_village_name_never_places_itself(split_gaz):
    """Two Agarams, so the name alone says nothing. 1,150 village names are
    shared this way — which is why this rule is exact and unique-or-nothing."""
    r = resolve_place(split_gaz, village="Agaram")
    assert r["village"] is None
    assert r["village_status"] == "no-parent-taluk"


def test_a_known_parent_outranks_the_bare_village_name(split_gaz):
    """The state-wide rule must never overrule a district the notice states:
    it is a last resort, not a shortcut."""
    r = resolve_place(split_gaz, district="Cuddalore", village="Velampalayam")
    assert r["village"] is None          # not the Tiruppur one
    assert r["district"] == "Cuddalore"


def test_a_chennai_city_taluk_has_no_village_to_find(split_gaz):
    """"Mambalam-Guindy" is a registration taluk. The revenue record does not
    hold it, and the city proper keeps no revenue villages, so 290 lots read
    as extraction failures when the extraction was right and there is no
    answer to find."""
    for raw in ["Mambalam Guindy", "Mambalam-Gandy", "Egmore – Nungambakkam",
                "Fort - Tondiarpet", "Perumbur – Purasaiwakkam", "Saidapet"]:
        r = resolve_place(split_gaz, district="Chennai", taluk=raw,
                          village="Kodambakkam")
        assert r["district"] == "Chennai"
        assert r["village_status"] == VILLAGE_NOT_APPLICABLE, raw


def test_chennais_outer_taluks_keep_their_villages(split_gaz):
    """Sholinganallur, Madhavaram, Maduravoyal, Thiruvottiyur and Alandur hold
    48 revenue villages between them. A lot naming one has a real answer, so
    it must stay in the queue rather than be written off as urban."""
    from pipeline.place_resolution import names_a_chennai_city_taluk
    for raw in ["Sholinganallur", "Madhavaram", "Maduravoyal",
                "Thiruvottiyur", "Alandur"]:
        assert not names_a_chennai_city_taluk(raw), raw
    # "Andali" is a live corpus string that names nothing the gazetteer holds
    # and nothing in the city table either, so it stays work.
    r = resolve_place(split_gaz, district="Chennai", taluk="Andali",
                      village="Kottur")
    assert r["village_status"] == "no-parent-taluk"


def test_a_property_in_another_state_is_refused_before_matching(split_gaz):
    """167 lots name a Kerala district. A Tamil Nadu gazetteer has no answer,
    and at the fuzzy floor it would invent one."""
    from pipeline.place_resolution import OUTSIDE_STATE
    r = resolve_place(split_gaz, district="Ernakulam", village="Kakkanad")
    assert r["village_status"] == OUTSIDE_STATE
    assert r["district"] is None and r["village"] is None
    r = resolve_place(split_gaz, district="Tiruppur", state="Kerala",
                      village="Velampalayam")
    assert r["village_status"] == OUTSIDE_STATE


# The 38 districts of Tamil Nadu, as the gazetteer in the graph names them.
# Stated here rather than read from the database because the property below is
# what makes the out-of-state table safe, and a test that needs a connection
# to prove it would not run where it matters.
TN_DISTRICTS = (
    "Ariyalur", "Chengalpattu", "Chennai", "Coimbatore", "Cuddalore",
    "Dharmapuri", "Dindigul", "Erode", "Kallakurichi", "Kancheepuram",
    "Kanyakumari", "Karur", "Krishnagiri", "Madurai", "Mayiladuthurai",
    "Nagapattinam", "Namakkal", "Nilgiris", "Perambalur", "Pudukkottai",
    "Ramanathapuram", "Ranipet", "Salem", "Sivagangai", "Tenkasi",
    "Thanjavur", "Theni", "Thiruchirappalli", "Thiruvarur", "Thoothukudi",
    "Tirunelveli", "Tirupathur", "Tiruppur", "Tiruvallur", "Tiruvannamalai",
    "Vellore", "Villupuram", "Virudhunagar",
)


def test_no_tamil_nadu_district_is_listed_as_foreign():
    """The out-of-state table works by name alone and refuses before any
    matching runs, so a Tamil Nadu district folding to one of its keys would
    blank every property in that district."""
    from pipeline.place_resolution import _NON_TN_DISTRICTS
    for d in TN_DISTRICTS:
        assert normalize_place(d) not in _NON_TN_DISTRICTS, d


def test_a_tamil_nadu_district_spelled_loosely_is_not_read_as_foreign():
    """The spellings the corpus actually carries, checked against the same
    table — "Tuticorin" and "Trichy" must not look like another state."""
    from pipeline.place_resolution import outside_tamil_nadu
    for raw in ["Tuticorin", "Trichy", "Kanchipuram", "Chengalpet",
                "Tiruchirapalli", "Virudunagar", "Nilgiri", "Madras"]:
        assert not outside_tamil_nadu(district=raw), raw


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
