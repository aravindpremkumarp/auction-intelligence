"""The cross-portal matcher, on values seen in the 2026-09-12 harvest.

The four-field rule: listings of different sources are compared only within
one bank and auction day; exact reserve price and borrower on exactly one
listing confirm; a unit number only one listing holds settles a batch; every
partial agreement waits for review; same-source listings and graph-vs-graph
are never compared.
"""
from __future__ import annotations

import pytest

from pipeline.match_confidence import CONFIRMED, PENDING
from sources.match import (
    Candidate, boundary_matches, candidate_from_graph, candidate_from_row, day_of,
    extract_boundaries, extract_identifiers, find_same_listing_pairs, match_listings, normalize_identifier_value,
)

# be-236961, Hinduja Housing Finance, Tirupattur — the description carries all four sides
BE_TEXT = ("Thirupathur district Vellore registration district, Tirupathur sub district, Tirupattur taluk, "
           "Tirupattur village s.no 300/3, in this plot bounded on west by property belongs to Narayanan, "
           "east by road, south by property belongs to Palani, north by property belongs to P.Kumar, in this "
           "east to west northernside 37 %1⁄2 feet, southern side 34% feet, north to south easternside 19 feet, "
           "westernside 19 feet, in total 684 sq.ft or 63.54 sq.mts")
# bn-359826, Indian Overseas Bank, Tirunelveli — identifiers only
BN_TEXT = ("D no 81A BY 1 81A BY 2, S.No.381 BY 5A, ward no 11, chatthiram kudiyirupu, naranammalpuram village, "
           "tirunelveli, total extent 1215 sqft")


# ── extractors ──────────────────────────────────────────────────────────────

def test_extract_boundaries_from_bankeauctions_description():
    assert extract_boundaries(BE_TEXT) == {
        "west": "property belongs to narayanan",
        "east": "road",
        "south": "property belongs to palani",
        "north": "property belongs to p kumar",
    }


def test_extract_boundaries_colon_and_dash_styles():
    assert extract_boundaries("North : Property of Mr. Kumar, South : 20 feet road, East : Plot No.12, West : Plot No.10") == {
        "north": "property of mr kumar", "south": "20 feet road", "east": "plot no 12", "west": "plot no 10"}
    assert extract_boundaries("North-Vacant land South-Road East-House of Raman West-Lane") == {
        "north": "vacant land", "south": "road", "east": "house of raman", "west": "lane"}
    assert extract_boundaries("") == {} and extract_boundaries(None) == {}
    # be-238476 / be-236952: compound directions and dimensions are not neighbours
    assert extract_boundaries("North-East by Plot No C7, South-West by Plot No C5") == {}
    assert extract_boundaries("north to south 19 feet, east to west 37 feet, west by 19 feet road") == {"west": "19 feet road"}


def test_boundary_matches_needs_three_sides_and_tolerates_containment():
    portal = extract_boundaries(BE_TEXT)
    graph = {"north": "property belongs to p kumar", "south": "property belongs to palani",
             "east": "20 feet road", "west": "property of narayanan"}
    assert boundary_matches(portal, graph)                       # north, south exact; east by containment
    assert not boundary_matches(portal, {"north": "property belongs to p kumar", "east": "road"})
    assert not boundary_matches({}, {})


@pytest.mark.parametrize("text, expected", [
    (BN_TEXT, {("door", "81a/1"), ("survey", "381/5a")}),
    (BE_TEXT, {("survey", "300/3")}),
    ("Old S.No. 328/1, New S.No 240/21, Plot No. 143/3, Flat No. S2, Door No. 617", {
        ("survey", "328/1"), ("survey", "240/21"), ("plot", "143/3"), ("flat", "s2"), ("door", "617")}),
    ("T.S.No.42/6 Ward No.6", {("survey", "42/6")}),
    ("R.S.No. 418/2 Divided In Plot No 27, bounded North by Plot No 28, South by Plot No 26", {
        ("survey", "418/2"), ("plot", "27")}),                     # neighbours' numbers are not ours
    ("S.No 3 in the village", set()),                              # a bare digit is not evidence
    ("", set()), (None, set()),
    # Futuristic Global Resources (bn-353994 / 855475): villas separate a batch sale
    ("Residential Villa at Phoenix The Village Residential Villa No.18,Fabiola Block", {("villa", "18")}),
    # Gunasekaran (bn-350805 / 853781): "Flat FF1" has no "No." and a two-letter unit
    ("All the piece and parcel of Residential Flat FF1 measuring 1100 Sq.ft. in First Floor", {("flat", "ff1")}),
    # an area after "flat" is not a flat number
    ("2 BHK flat 1100 sq.ft in the first floor", set()),
    # ordinal floor/phase numbers are not unit identifiers
    ("Residential flat 5th floor of the building, door no 45", {("door", "45")}),
    ("Villa 3rd phase of the layout", set()),
])
def test_extract_identifiers(text, expected):
    assert extract_identifiers(text) == expected


def test_extract_extent_states_the_first_area_phrase():
    from sources.match import extract_extent
    assert extract_extent(BN_TEXT) == "1215 sqft"
    assert extract_extent(BE_TEXT) == "684 sq.ft"
    assert extract_extent("2.17 Cents (balance 1.802 Cents)") == "2.17 Cents"
    assert extract_extent("no size here") is None and extract_extent(None) is None


def test_normalize_identifier_value():
    assert normalize_identifier_value("381 BY 5A") == "381/5a"
    assert normalize_identifier_value(" 300 / 3 ") == "300/3"
    assert normalize_identifier_value("143-3") == "143/3"


def test_day_of_accepts_strings_and_temporals():
    from datetime import datetime
    assert day_of("2026-09-24T11:00:00") == "2026-09-24"
    assert day_of(datetime(2026, 9, 24, 11)) == "2026-09-24"
    assert day_of("24-09-2026") is None and day_of(None) is None and day_of("") is None


# ── candidates ──────────────────────────────────────────────────────────────

def _row(aid, source, *, bank, reserve, day, borrower="", text="", **extra):
    return {"auction_id": aid, "source": source, "bank_name": bank, "reserve_price_num": reserve,
            "auction_start_dt": f"{day}T11:00:00", "borrower_name": borrower, "title": text, "description": "", **extra}


def _graph(aid, *, bank, reserve, day, borrower="", boundaries=None, identifiers=(), shas=(), source="eauctionsindia"):
    return {"auction_id": aid, "source": source, "bank": bank, "reserve_price_num": reserve,
            "auction_start_dt": f"{day}T11:00:00", "borrower": borrower, "boundaries": boundaries or {},
            "identifiers": [list(i) for i in identifiers], "doc_shas": list(shas)}


def test_candidate_from_row_reads_text_and_shas():
    c = candidate_from_row(_row("bn-359826", "baanknet", bank="Indian Overseas Bank", reserve=4626500.0,
                                day="2026-09-24", borrower="N MARIAPPAN", text=BN_TEXT), doc_shas=["abc", ""])
    assert c.identifiers == {("door", "81a/1"), ("survey", "381/5a")}
    assert c.doc_shas == {"abc"} and c.bank_key == "bank indian overseas" and c.day == "2026-09-24"


def test_candidate_from_graph_collapses_identifier_kinds_and_defaults_source():
    c = candidate_from_graph(_graph("841207", bank="Indian Overseas Bank", reserve=4626500, day="2026-09-24",
                                    identifiers=[("survey_old", "381/5A"), ("door_new", "81A/1"), ("patta", "881")],
                                    boundaries={"north": "Property of P. Kumar"}))
    assert c.source == "eauctionsindia"
    assert c.identifiers == {("survey", "381/5a"), ("door", "81a/1")}
    assert c.boundaries == {"north": "property of p kumar"}


def test_bucket_key_needs_bank_and_day_but_not_price():
    assert Candidate("x", "s", bank="SBI", reserve_price_num=1.0, auction_start_dt="2026-01-01").bucket_key
    assert Candidate("x", "s", bank="", reserve_price_num=1.0, auction_start_dt="2026-01-01").bucket_key is None
    assert Candidate("x", "s", bank="SBI", reserve_price_num=1.0, auction_start_dt=None).bucket_key is None
    unpriced = Candidate("x", "s", bank="SBI", reserve_price_num=0, auction_start_dt="2026-01-01")
    assert unpriced.bucket_key and not unpriced.has_price


# ── the matcher ─────────────────────────────────────────────────────────────

def _bn(aid, *, bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="N MARIAPPAN", text="", source="baanknet"):
    return candidate_from_row(_row(aid, source, bank=bank, reserve=reserve, day=day, borrower=borrower, text=text))


def _ea(aid, *, bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24", borrower="Mr. N. Mariappan", identifiers=()):
    return candidate_from_graph(_graph(aid, bank=bank, reserve=reserve, day=day, borrower=borrower, identifiers=identifiers))


def test_bank_day_price_and_borrower_on_one_listing_is_confirmed():
    [p] = find_same_listing_pairs([_bn("bn-359826")], [_ea("841207")])
    assert (p.a_id, p.b_id, p.method, p.confidence) == ("bn-359826", "841207", "four_fields", CONFIRMED)


def test_price_agrees_but_borrower_differs_waits_for_a_person():
    """ARR Tex (bn-359756 / 853518): same price, 'A R R TEX' vs 'M/s ARR Tex'."""
    result = match_listings(
        [_bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")],
        [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")])
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in result.pairs] == [("bn-359756", "853518", "review", PENDING)]
    assert [(a.auction_id, a.other_source, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-359756", "eauctionsindia", ("853518",), "price_only")]


def test_borrower_agrees_but_price_differs_waits_for_a_person():
    result = match_listings([_bn("bn-1")], [_ea("841207", reserve=4700000.0)])
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-1", "borrower_only")]


def test_a_batch_sale_no_unit_number_separates_waits_for_a_person():
    """Ekadanta Enterprises (bn-359636): three of ours agree on all four."""
    ours = [_ea(aid, bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15", borrower="M/s. Ekadanta Enterprises")
            for aid in ("842118", "842546", "844968")]
    result = match_listings([_bn("bn-359636", bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15",
                                 borrower="Ekadanta Enterprises", text="Sy.No.788/2, Dry. Ext. Hec. 0.40.5")], ours)
    assert [(a.auction_id, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-359636", ("842118", "842546", "844968"), "batch")]
    assert {p.confidence for p in result.pairs} == {PENDING} and len(result.pairs) == 3


def test_a_unit_number_that_picks_one_listing_settles_a_batch():
    ours = [_ea("856500", identifiers=[("flat", "f3")]), _ea("856501", identifiers=[("flat", "f4")])]
    [p] = find_same_listing_pairs([_bn("bn-1", text="Residential Flat No. F3, Second Floor")], ours)
    assert (p.b_id, p.method, p.confidence) == ("856500", "unit_number", CONFIRMED)
    assert "same flat number 3" in p.evidence


def test_a_unit_number_only_one_listing_holds_settles_a_shared_door_batch():
    """A block of flats: every listing quotes the building's door number 12; only one also says flat F3."""
    ours = [_ea("1", identifiers=[("flat", "f3"), ("door", "12")]), _ea("2", identifiers=[("door", "12")])]
    [p] = find_same_listing_pairs([_bn("bn-1", text="Flat No. F3, Door No. 12")], ours)
    assert (p.b_id, p.method) == ("1", "unit_number")
    assert "same flat number 3" in p.evidence


def test_all_four_agree_but_plot_numbers_differ_waits_for_a_person():
    result = match_listings([_bn("bn-352470", text="Plot No. 45, S.No. 73/7")],
                            [_ea("866338", identifiers=[("plot", "44"), ("plot", "47")])])
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-352470", "units_disagree")]


def test_two_portal_listings_claiming_one_of_ours_are_contested():
    result = match_listings([_bn("bn-1"), _bn("bn-2")], [_ea("841207")])
    assert [(a.auction_id, a.candidates, a.reason) for a in result.ambiguous] == [
        ("bn-1", ("841207",), "contested"), ("bn-2", ("841207",), "contested")]
    assert all(p.confidence == PENDING for p in result.pairs)


def test_price_on_one_listing_and_borrower_on_another_is_split():
    result = match_listings([_bn("bn-1")], [_ea("1", borrower="Mr. Haridas P"), _ea("2", reserve=5000000.0)])
    assert [(a.candidates, a.reason) for a in result.ambiguous] == [(("1", "2"), "split")]


def test_nothing_agreeing_is_new():
    result = match_listings([_bn("bn-1")], [_ea("1", reserve=100000.0, borrower="Mr. Haridas P")])
    assert result.pairs == [] and result.ambiguous == []


def test_another_bank_or_day_is_never_compared():
    result = match_listings([_bn("bn-1")], [_ea("1", bank="Canara Bank"), _ea("2", day="2026-09-25")])
    assert result.pairs == [] and result.ambiguous == []


def test_a_missing_price_never_agrees():
    result = match_listings([_bn("bn-1", reserve=None)], [_ea("1", reserve=None)])
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-1", "borrower_only")]


def test_same_source_listings_are_never_compared():
    assert match_listings([_bn("bn-1"), _bn("bn-2")], []).pairs == []


def test_the_graph_is_never_matched_with_itself():
    assert match_listings([], [_ea("1"), _ea("2")]).pairs == []


def test_duplicate_postings_of_one_unit_link_together():
    ours = [_ea(aid, identifiers=[("villa", "18")]) for aid in ("855475", "855589")]
    pairs = find_same_listing_pairs([_bn("bn-353994", text="Residential Villa No.18, Fabiola Block")], ours)
    assert sorted((p.b_id, p.method) for p in pairs) == [("855475", "four_fields"), ("855589", "four_fields")]


def test_the_better_ranked_portal_is_the_subject():
    [p] = find_same_listing_pairs([_bn("be-7", source="bankeauctions", borrower="Mr. N Mariappan"), _bn("bn-7")], [])
    assert (p.a_id, p.b_id, p.a_source, p.b_source) == ("bn-7", "be-7", "baanknet", "bankeauctions")


def test_display_fields_ride_along_but_never_decide():
    row = candidate_from_row(_row("bn-1", "baanknet", bank="Indian Bank", reserve=1.0, day="2026-09-25",
                                  text="Land", source_url="https://baanknet.com/x", emd_num=100.0, city="Salem"))
    assert row.info["url"] == "https://baanknet.com/x" and row.info["emd"] == 100.0 and row.info["city"] == "Salem"
    graph = candidate_from_graph({**_graph("1", bank="Indian Bank", reserve=1.0, day="2026-09-25"),
                                  "title": "House", "url": "https://eauctionsindia.com/1", "emd_num": 5.0,
                                  "public_url": "https://r2/n.pdf", "description": "desc", "city": "Salem"})
    assert graph.info == {"title": "House", "description": "desc", "city": "Salem", "district": None,
                          "url": "https://eauctionsindia.com/1", "emd": 5.0, "public_url": "https://r2/n.pdf"}


def test_unit_key_normalises_notation():
    from sources.match import _unit_key
    assert [_unit_key(v) for v in ("f/1", "f1", "b/510", "86/b", "86b", "ff12", "S-2")] == ["1", "1", "510", "86b", "86b", "ff12", "2"]


from sources.match import snapshot_of  # noqa: E402


def _decided(subject, verdict, linked=(), rejected=(), **snapshot_changes):
    return {(subject.auction_id, "eauctionsindia"): {"verdict": verdict, "linked_ids": set(linked),
                                                      "rejected_ids": set(rejected),
                                                      "snapshot": {**snapshot_of(subject), **snapshot_changes}}}


def test_a_person_confirming_links_with_method_decision():
    subject = _bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")
    ours = [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")]
    result = match_listings([subject], ours, decisions=_decided(subject, "approved", linked=["853518"]))
    assert [(p.a_id, p.b_id, p.method, p.confidence) for p in result.pairs] == [("bn-359756", "853518", "decision", CONFIRMED)]
    assert result.ambiguous == []


def test_ticking_two_duplicate_postings_links_both():
    subject = _bn("bn-359636", bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15", borrower="Ekadanta Enterprises")
    ours = [_ea(aid, bank="Bank of Baroda", reserve=2030000.0, day="2026-09-15", borrower="M/s. Ekadanta Enterprises")
            for aid in ("842118", "842546", "844968")]
    decisions = _decided(subject, "approved", linked=["842546", "844968"], rejected=["842118"])
    result = match_listings([subject], ours, decisions=decisions)
    assert sorted((p.b_id, p.method) for p in result.pairs) == [("842546", "decision"), ("844968", "decision")]
    assert result.ambiguous == []


def test_not_the_same_removes_the_pair_for_good():
    subject = _bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")
    ours = [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")]
    result = match_listings([subject], ours, decisions=_decided(subject, "rejected", rejected=["853518"]))
    assert result.pairs == [] and result.ambiguous == []


def test_a_decision_on_facts_that_changed_is_ignored():
    subject = _bn("bn-359756", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="A R R TEX")
    ours = [_ea("853518", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s ARR Tex")]
    stale = _decided(subject, "approved", linked=["853518"], reserve_price=2900000)
    result = match_listings([subject], ours, decisions=stale)
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-359756", "price_only")]


def test_a_rule_link_to_a_listing_a_person_already_linked_is_contested():
    decided = _bn("bn-1", borrower="Mr. Haridas P")
    other = _bn("bn-2")
    result = match_listings([decided, other], [_ea("841207")], decisions=_decided(decided, "approved", linked=["841207"]))
    assert [(p.a_id, p.method) for p in result.pairs if p.method == "decision"] == [("bn-1", "decision")]
    assert [(a.auction_id, a.reason) for a in result.ambiguous] == [("bn-2", "contested")]


def test_snapshot_of_reads_the_four_facts():
    assert snapshot_of(_bn("bn-1")) == {"bank": "bank indian overseas", "reserve_price": 4626500,
                                        "borrower": "n mariappan", "auction_day": "2026-09-24"}


def test_decisions_are_per_other_source():
    """bn-1 was settled against eauctionsindia only; its bankeauctions case stays open under its own key."""
    subject = _bn("bn-1", borrower="A R R TEX")
    be = _bn("be-1", source="bankeauctions", borrower="Mr. Haridas P")
    decisions = {("bn-1", "eauctionsindia"): {"verdict": "approved", "linked_ids": {"1"}, "rejected_ids": set(),
                                               "snapshot": snapshot_of(subject)}}
    result = match_listings([subject, be], [_ea("1", borrower="M/s ARR Tex")], decisions=decisions)
    assert ("bn-1", "1", "decision") in [(p.a_id, p.b_id, p.method) for p in result.pairs]
    assert [(a.auction_id, a.other_source, a.reason) for a in result.ambiguous] == [
        ("be-1", "eauctionsindia", "price_only"), ("bn-1", "bankeauctions", "price_only")]
