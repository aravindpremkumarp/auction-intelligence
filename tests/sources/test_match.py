"""The cross-portal matcher, on values seen in the 2026-09-12 harvest.

What it must get right: the same-source trap (a BAANKNET batch sale is two
listings, never one), the strongest evidence wins, the bucket admits a 1%
price difference but not a different day, and the graph is never matched
against itself.
"""
from __future__ import annotations

import pytest

from pipeline.match_confidence import CONFIRMED, INFERRED, PROBABLE
from sources.match import (
    Candidate, boundary_matches, candidate_from_graph, candidate_from_row, day_of,
    extract_boundaries, extract_identifiers, find_same_listing_pairs, normalize_identifier_value,
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
])
def test_extract_identifiers(text, expected):
    assert extract_identifiers(text) == expected


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

def test_same_source_batch_sale_is_never_a_pair():
    """bn-351743 / bn-351740: same bank, borrower, day and reserve — two
    properties of one borrower sold in one sitting."""
    rows = [
        _row("bn-351743", "baanknet", bank="Indian Bank", reserve=2136000.0, day="2026-09-25", borrower="M/s Sri Vaaru Traders"),
        _row("bn-351740", "baanknet", bank="Indian Bank", reserve=2136000.0, day="2026-09-25", borrower="M/s Sri Vaaru Traders"),
    ]
    assert find_same_listing_pairs([candidate_from_row(r) for r in rows], []) == []


def test_borrower_match_across_portals_is_probable():
    inc = [candidate_from_row(_row("bn-359826", "baanknet", bank="Indian Overseas Bank", reserve=4626500.0,
                                   day="2026-09-24", borrower="N MARIAPPAN"))]
    ext = [candidate_from_graph(_graph("841207", bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24",
                                       borrower="Mr. N. Mariappan"))]
    [p] = find_same_listing_pairs(inc, ext)
    assert (p.a_id, p.b_id, p.method, p.confidence) == ("bn-359826", "841207", "borrower", PROBABLE)
    assert (p.a_source, p.b_source) == ("baanknet", "eauctionsindia")


def test_notice_bytes_is_confirmed_and_beats_borrower():
    inc = [candidate_from_row(_row("be-236961", "bankeauctions", bank="Hinduja Housing Finance Limited", reserve=1090000.0,
                                   day="2026-09-15", borrower="Mr. VINOTHKUMAR P"), doc_shas=["f" * 64])]
    ext = [candidate_from_graph(_graph("850001", bank="Hinduja Housing Finance Ltd", reserve=1090000.0, day="2026-09-15",
                                       borrower="Vinothkumar P", shas=["f" * 64, "0" * 64]))]
    [p] = find_same_listing_pairs(inc, ext)
    assert (p.method, p.confidence) == ("notice_bytes", CONFIRMED)
    assert "sha256" in p.evidence


def test_boundaries_confirm_without_a_borrower():
    inc = [candidate_from_row(_row("be-236961", "bankeauctions", bank="Hinduja Housing Finance Limited", reserve=1090000.0,
                                   day="2026-09-15", text=BE_TEXT))]
    ext = [candidate_from_graph(_graph("850001", bank="Hinduja Housing Finance", reserve=1090000.0, day="2026-09-15",
                                       boundaries={"north": "Property belongs to P.Kumar", "south": "Property belongs to Palani",
                                                   "east": "Road", "west": "Property of Narayanan"}))]
    [p] = find_same_listing_pairs(inc, ext)
    assert (p.method, p.confidence) == ("boundaries", CONFIRMED)


def test_identifier_match_is_probable():
    inc = [candidate_from_row(_row("bn-359826", "baanknet", bank="Indian Overseas Bank", reserve=4626500.0,
                                   day="2026-09-24", text=BN_TEXT))]
    ext = [candidate_from_graph(_graph("841207", bank="Indian Overseas Bank", reserve=4626500.0, day="2026-09-24",
                                       identifiers=[("survey_old", "381/5A")]))]
    [p] = find_same_listing_pairs(inc, ext)
    assert (p.method, p.confidence, p.evidence) == ("identifier", PROBABLE, "same survey number 381/5a")


def test_bucket_only_is_inferred_and_price_tolerance_is_one_percent():
    inc = [candidate_from_row(_row("bn-1", "baanknet", bank="State Bank of India", reserve=4100000.0, day="2026-10-22"))]
    near = candidate_from_graph(_graph("900001", bank="State Bank of India", reserve=4130000.0, day="2026-10-22"))   # +0.7%
    far = candidate_from_graph(_graph("900002", bank="State Bank of India", reserve=4200000.0, day="2026-10-22"))    # +2.4%
    other_day = candidate_from_graph(_graph("900003", bank="State Bank of India", reserve=4100000.0, day="2026-10-23"))
    other_bank = candidate_from_graph(_graph("900004", bank="Bank of India", reserve=4100000.0, day="2026-10-22"))
    pairs = find_same_listing_pairs(inc, [near, far, other_day, other_bank])
    assert [(p.b_id, p.method, p.confidence) for p in pairs] == [("900001", "bucket_only", INFERRED)]


def test_unpriced_graph_listing_matches_on_evidence_but_never_on_the_bucket_alone():
    """Hundreds of eauctionsindia listings carry no reserve price ("not
    published"). Indian Overseas Bank, 2026-09-24: the graph's copy has none."""
    inc = [candidate_from_row(_row("bn-359826", "baanknet", bank="Indian Overseas Bank", reserve=4626500.0,
                                   day="2026-09-24", borrower="N MARIAPPAN"))]
    named = candidate_from_graph(_graph("863619", bank="Indian Overseas Bank", reserve=None, day="2026-09-24", borrower="Mr. N Mariappan"))
    nameless = candidate_from_graph(_graph("863620", bank="Indian Overseas Bank", reserve=None, day="2026-09-24", borrower="Mr. Kiran Kumar Roka"))
    pairs = find_same_listing_pairs(inc, [named, nameless])
    assert [(p.b_id, p.method, p.confidence) for p in pairs] == [("863619", "borrower", PROBABLE)]


def test_disagreeing_prices_never_pair_even_with_a_borrower_match():
    inc = [candidate_from_row(_row("bn-1", "baanknet", bank="Indian Bank", reserve=2136000.0, day="2026-09-25", borrower="Sri Vaaru Traders"))]
    ext = [candidate_from_graph(_graph("1", bank="Indian Bank", reserve=2944000.0, day="2026-09-25", borrower="M/s. Sri Vaaru Traders"))]
    assert find_same_listing_pairs(inc, ext) == []


def test_two_new_portals_match_each_other_but_the_graph_never_matches_itself():
    inc = [
        candidate_from_row(_row("bn-7", "baanknet", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="R. Suresh")),
        candidate_from_row(_row("be-7", "bankeauctions", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="Mr. R Suresh")),
    ]
    ext = [
        candidate_from_graph(_graph("910001", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="Suresh R")),
        candidate_from_graph(_graph("910002", bank="Canara Bank", reserve=4890000.0, day="2026-10-16", borrower="Suresh R")),
    ]
    pairs = find_same_listing_pairs(inc, ext)
    ids = {tuple(sorted((p.a_id, p.b_id))) for p in pairs}
    assert ("910001", "910002") not in ids
    assert ("be-7", "bn-7") in ids
    assert all(p.method == "borrower" for p in pairs) and len(pairs) == 5


def test_pairs_are_sorted_strongest_first_and_unique():
    inc = [candidate_from_row(_row("bn-1", "baanknet", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B"))]
    ext = [candidate_from_graph(_graph("1", bank="Indian Bank", reserve=100000.0, day="2026-09-25")),
           candidate_from_graph(_graph("2", bank="Indian Bank", reserve=100000.0, day="2026-09-25", borrower="A B"))]
    pairs = find_same_listing_pairs(inc, ext)
    assert [(p.b_id, p.method) for p in pairs] == [("2", "borrower"), ("1", "bucket_only")]
