"""The gap report's pure half: core-field detection on both sides and the
per-source arithmetic. Records are shaped as the Cypher in
``scripts/gap_report.py`` returns them (779491 is a real graph record from
2026-09-12, trimmed); rows are real harvested rows.
"""
from __future__ import annotations

from scripts import gap_report as gr
from sources.match import Ambiguity, candidate_from_row, match_listings

GRAPH_779491 = {
    "auction_id": "779491", "source": "eauctionsindia", "bank": "Krazybee Services Limited",
    "reserve_price_num": 7268400.0, "auction_start_dt": "2026-07-15T10:00:00Z", "borrower": "Anandhi J",
    "doc_shas": ["594503be14aaf3b778ba2c6442ee493531c6d0bf9069d9f3d39d85137ba17774"], "n_docs": 1,
    "identifiers": [["block", "3"], ["survey_old", "30/3a"], ["survey_new", "97"]],
    "lot_bounds": [["north", "Property of Bakkhavachala Naidu", 110.0], ["south", "Road & Vacant land", 110.0],
                   ["east", "Property of Chinna Pilla", 40.0], ["west", "Property of Kesaval Naidu", 40.0]],
    "n_extents": 2, "possession": "physical", "property_type": "land", "district": "Ranipet",
    "total_area": "408.77 sq.m (or) 4400 sq.ft",
    "boundaries": {"east": "Property of Chinna Pilla", "south": "Road & Vacant land",
                   "north": "Property of Bakkhavachala Naidu", "west": "Property of Kesaval Naidu"},
    "boundary_measurements": ["110 Ft.", "110 Ft.", "40 Ft", "40 Ft"], "photo_urls": None,
}

BN_359826 = {
    "auction_id": "bn-359826", "source": "baanknet", "bank_name": "Indian Overseas Bank",
    "title": "D no 81A BY 1 81A BY 2, S.No.381 BY 5A, ward no 11, chatthiram kudiyirupu, naranammalpuram village, "
             "tirunelveli, total extent 1215 sqft",
    "description": "", "reserve_price_num": 4626500.0, "auction_start_dt": "2026-09-24T11:00:00",
    "borrower_name": "N MARIAPPAN", "district": "Tirunelveli", "city": "", "property_types": ["House"],
    "property_type_raw": "Residential / Individual House", "possession_type": "symbolic", "extent_raw": "",
    "has_photos": True, "downloads_found": [],
}

BE_236961 = {
    "auction_id": "be-236961", "source": "bankeauctions", "bank_name": "Hinduja Housing Finance Limited",
    "title": "Thirupathur district Vellore registration district",
    "description": "Tirupattur village s.no 300/3, in this plot bounded on west by property belongs to Narayanan, "
                   "east by road, south by property belongs to Palani, north by property belongs to P.Kumar, in this "
                   "east to west northernside 37 %1⁄2 feet, southern side 34% feet, north to south easternside 19 feet, "
                   "westernside 19 feet, in total 684 sq.ft or 63.54 sq.mts",
    "reserve_price_num": 1090000.0, "auction_start_dt": "2026-09-15T11:00:00", "borrower_name": "Mr. VINOTHKUMAR P",
    "district": "", "city": "Tirupathur", "property_types": ["Land"], "property_type_raw": "Immovable / Land",
    "possession_type": "", "extent_raw": "", "has_photos": False, "downloads_found": [],
}


def test_core_from_graph_reads_lot_and_listing_shapes():
    assert gr.core_from_graph(GRAPH_779491) == {
        "property_type": True, "location": True, "extent": True, "measurement": True, "possession": True,
        "boundaries": True, "reserve_price": True, "auction_date": True, "has_photos": False}
    bare = {"auction_id": "1", "possession": "not stated", "boundaries": {"north": "x"},
            "lot_bounds": [["south", "y", None]], "boundary_measurements": [None, None, None, None]}
    assert sum(gr.core_from_graph(bare).values()) == 0
    assert gr.graph_boundaries(bare) == {"north": "x", "south": "y"}


def test_unread_graph_listing_is_credited_with_what_its_text_states():
    """856115 on 2026-09-12: a city, no district, no price, no lots — but a
    description that names R.S.No. 418/2 and the extent."""
    rec = {"auction_id": "856115", "city": "Pudukkottai", "district": None, "reserve_price_num": None,
           "auction_start_dt": "2026-09-16T11:00:00Z", "bank": "Grihum Housing Finance Limited", "borrower": "VIJAYAKUMAR J",
           "description": "Item No. 1 Pudukkottai District, R.S.No. 418/2 Divided In Plot No 27, an extent of 1200 sq.ft, "
                          "bounded North by Plot No 28, South by Plot No 26, East by 9.00 mts Road, West by scheme boundary",
           "lot_bounds": [], "identifiers": [], "boundaries": {}, "boundary_measurements": [], "n_extents": 0}
    core = gr.core_from_graph(rec)
    assert [f for f, v in core.items() if v] == ["location", "extent", "boundaries", "auction_date"]
    cand = gr.graph_candidate(rec)
    assert cand.identifiers == {("survey", "418/2"), ("plot", "27")}
    assert cand.boundaries["north"] == "plot no 28" and not cand.has_price


def test_core_from_row_on_both_portals():
    bn = gr.core_from_row(BN_359826)
    assert [f for f, v in bn.items() if v] == ["property_type", "location", "extent", "possession",
                                               "reserve_price", "auction_date", "has_photos"]
    be = gr.core_from_row(BE_236961)
    assert [f for f, v in be.items() if v] == ["property_type", "location", "extent", "measurement",
                                               "boundaries", "reserve_price", "auction_date"]


def _graph_841207():
    return {"auction_id": "841207", "source": "eauctionsindia", "bank": "Indian Overseas Bank", "reserve_price_num": 4626500.0,
            "auction_start_dt": "2026-09-24T11:00:00Z", "borrower": "Mr. N. Mariappan", "doc_shas": [], "identifiers": [],
            "lot_bounds": [], "n_extents": 0, "possession": None, "property_type": "house", "district": "Tirunelveli",
            "total_area": None, "boundaries": {}, "boundary_measurements": [], "photo_urls": None}


def test_build_report_counts_confirmed_review_new_fills_and_photos():
    graph = [GRAPH_779491, _graph_841207()]
    rows = {"baanknet": [BN_359826, {**BN_359826, "auction_id": "bn-1", "bank_name": "Nobody Bank", "has_photos": True}],
            "bankeauctions": [BE_236961, {**BE_236961, "auction_id": "779491"}]}   # a re-run of a loaded id
    result = match_listings([candidate_from_row(r) for rs in rows.values() for r in rs],
                            [gr.graph_candidate(g) for g in graph])
    assert [(p.a_id, p.b_id, p.method) for p in result.pairs] == [("bn-359826", "841207", "four_fields")]

    rep = gr.build_report(rows, graph, result.pairs, result.ambiguous)
    bn = rep["sources"]["baanknet"]
    assert (bn["rows"], bn["already_loaded"], bn["new"], bn["review"], bn["confirmed"]) == (2, 0, 1, 0, 1)
    assert bn["confirmed_by_method"] == {"four_fields": 1, "unit_number": 0, "decision": 0}
    assert {f for f, n in bn["fills"].items() if n} == {"extent", "possession", "has_photos"}
    assert bn["photos_gained"] == {"new": 1, "confirmed": 1}
    [m] = bn["confirmed_listings"]
    assert (m["matches"], m["method"], m["fills"]) == ("841207", "four_fields", ["extent", "possession", "has_photos"])

    be = rep["sources"]["bankeauctions"]
    assert (be["rows"], be["already_loaded"], be["new"], be["review"], be["confirmed"]) == (2, 1, 1, 0, 0)

    text = gr.format_report(rep)
    assert "bn-359826 ~ 841207  four_fields" in text
    assert "confirmed 1 (four_fields 1, unit_number 0, decision 0)" in text


def test_review_listings_are_neither_new_nor_confirmed():
    rows = {"baanknet": [BN_359826, {**BN_359826, "auction_id": "bn-2"}]}
    ambiguous = [Ambiguity("bn-359826", "baanknet", "eauctionsindia", ("1", "2"), "batch")]
    rep = gr.build_report(rows, [], [], ambiguous)
    s = rep["sources"]["baanknet"]
    assert (s["rows"], s["new"], s["review"], s["confirmed"]) == (2, 1, 1, 0)
    assert s["review_by_reason"] == {"batch": 1}
    assert rep["ambiguous"] == [{"auction_id": "bn-359826", "source": "baanknet", "other_source": "eauctionsindia",
                                 "candidates": ("1", "2"), "reason": "batch"}]
    assert "review 1 (batch 1)" in gr.format_report(rep)


def test_graph_candidate_reads_unit_numbers_from_text_even_with_lot_identifiers():
    rec = {"auction_id": "855475", "bank": "Indian Bank", "reserve_price_num": 13500000.0,
           "auction_start_dt": "2026-09-28T11:00:00Z", "borrower": "M/s Futuristic Global Resources Private Limited",
           "description": "Property No.1: All that piece and parcel of Villa No.18 having super built up area of 2705 Sq.ft",
           "identifiers": [["survey_old", "123/4"]], "lot_bounds": [], "boundaries": {}}
    assert gr.graph_candidate(rec).identifiers == {("survey", "123/4"), ("villa", "18")}


def test_download_shas_hashes_found_files_only(tmp_path):
    d = tmp_path / "baanknet"
    d.mkdir()
    (d / "bn-1.pdf").write_bytes(b"%PDF")
    row = {"source": "baanknet", "downloads_found": ["bn-1.pdf", "bn-gone.pdf"]}
    import hashlib
    assert gr.download_shas(row, tmp_path) == [hashlib.sha256(b"%PDF").hexdigest()]
