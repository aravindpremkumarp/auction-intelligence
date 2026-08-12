"""Unit tests for pipeline/apply_extractions.py (pure logic + write guards)."""
from __future__ import annotations

import json

import pipeline.apply_extractions as AX


# ── helpers ──────────────────────────────────────────────────────────────────

def ent(cls, text="", attrs=None, id=None):
    return {"id": id or "0", "cls": cls, "text": text,
            "start": 0, "end": len(text), "attrs": attrs or {}}


# ── parse_money ──────────────────────────────────────────────────────────────

def test_parse_money_int_passthrough():
    assert AX.parse_money(950000) == 950000


def test_parse_money_string_with_grouping():
    assert AX.parse_money("Rs. 12,50,000/-") == 1250000


def test_parse_money_garbage_and_none():
    assert AX.parse_money(None) is None
    assert AX.parse_money("not a price") is None
    assert AX.parse_money(True) is None


# ── entities_with_corrections ────────────────────────────────────────────────

def test_corrections_override_text():
    ents = json.dumps([{"id": "3", "cls": "full_description",
                        "text": "wrong text", "attrs": {}}])
    corr = json.dumps({"3": {"value": "reviewer fixed text", "by": "a@b.c"}})
    out = AX.entities_with_corrections(ents, corr)
    assert out[0]["text"] == "reviewer fixed text"
    assert out[0]["corrected"] is True


def test_corrections_bad_json_tolerated():
    ents = json.dumps([{"id": "0", "cls": "location", "attrs": {}}])
    assert AX.entities_with_corrections(ents, "{not json") != []
    assert AX.entities_with_corrections("{not json", None) == []


# ── group_lots ───────────────────────────────────────────────────────────────

def test_group_lots_builds_flat_fields():
    ents = [
        ent("location", "Situated at X",
            {"village": "Padur", "taluk": "Chengalpattu",
             "district": "Kancheepuram", "lot_index": "1"}),
        ent("location", "Reg clause",
            {"registration_district": "Chengalpattu",
             "registration_sub_district": "Thiruporur", "lot_index": "1"}),
        ent("boundary", "Plot No.4",
            {"side": "north", "adjacency": "Plot No.4",
             "measurement": "40 Feet", "lot_index": "1"}),
        ent("extent", "1200 sq.ft",
            {"total_area": "1200 sq.ft", "lot_index": "1"}),
        ent("extent", "509 sq.ft UDS",
            {"undivided_share": "509 sq.ft UDS", "lot_index": "1"}),
        ent("identifier", "Door No 2/22",
            {"kind": "door_old", "value": "2/22", "lot_index": "1"}),
        ent("identifier", "Door No 2/79/C",
            {"kind": "door_new", "value": "2/79/C", "lot_index": "1"}),
        ent("auction_terms", "Rs.9,50,000",
            {"reserve_price_num": 950000, "lot_index": "1"}),
        ent("full_description", "All that piece and parcel...",
            {"lot_index": "1"}),
    ]
    lots = AX.group_lots(ents)
    assert set(lots) == {"1"}
    lot = lots["1"]
    f = lot["fields"]
    assert f["village"] == "Padur"
    assert f["registration_sub_district"] == "Thiruporur"
    assert f["boundary_north"] == "Plot No.4"
    assert f["boundary_measurement_north"] == "40 Feet"
    assert f["total_area"] == "1200 sq.ft"
    assert f["undivided_share"] == "509 sq.ft UDS"
    assert f["door_numbers_old"] == "2/22"
    assert f["door_numbers_new"] == "2/79/C"
    assert lot["reserve"] == 950000
    assert lot["description"] == "All that piece and parcel..."


def test_group_lots_separates_lot_indexes():
    ents = [
        ent("location", "", {"village": "A", "lot_index": "1"}),
        ent("location", "", {"village": "B", "lot_index": "2"}),
        ent("auction_terms", "", {"reserve_price_num": 100000, "lot_index": "1"}),
        ent("auction_terms", "", {"reserve_price_num": 200000, "lot_index": "2"}),
    ]
    lots = AX.group_lots(ents)
    assert lots["1"]["fields"]["village"] == "A"
    assert lots["2"]["fields"]["village"] == "B"
    assert lots["2"]["reserve"] == 200000


def test_group_lots_missing_lot_index_defaults_to_1():
    ents = [ent("location", "", {"village": "A"})]
    assert AX.group_lots(ents)["1"]["fields"]["village"] == "A"


def test_group_lots_boundary_falls_back_to_text():
    ents = [ent("boundary", "Road", {"side": "east"})]
    assert AX.group_lots(ents)["1"]["fields"]["boundary_east"] == "Road"


def test_group_lots_first_non_null_wins():
    ents = [
        ent("location", "", {"village": "First"}),
        ent("location", "", {"village": "Second"}),
    ]
    assert AX.group_lots(ents)["1"]["fields"]["village"] == "First"


# ── match_lots_to_listings ───────────────────────────────────────────────────

def _lot(reserve, desc="d", emd=None):
    return {"description": desc, "fields": {"village": "V"},
            "reserve": reserve, "emd": emd}


def test_match_single_lot_applies_to_all_listings():
    lots = {"1": _lot(500000)}
    listings = [{"aid": "a1", "price": 500000}, {"aid": "a2", "price": None}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert [m[2] for m in matches] == ["single", "single"]
    assert unmatched == []


def test_match_exact_and_tolerance():
    lots = {"1": _lot(500000), "2": _lot(1000000)}
    listings = [{"aid": "a1", "price": 500000},
                {"aid": "a2", "price": 1004000}]   # within 1%
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    reasons = {m[0]["aid"]: m[2] for m in matches}
    assert reasons == {"a1": "exact", "a2": "tolerance"}
    assert unmatched == []


def test_match_same_price_lots_are_ambiguous_not_guessed():
    lots = {"1": _lot(1250000), "2": _lot(1250000)}
    listings = [{"aid": "a1", "price": 1250000}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_match_unique_remainder_pairs_last_lot_and_listing():
    lots = {"1": _lot(500000), "2": _lot(None)}
    listings = [{"aid": "a1", "price": 500000},
                {"aid": "a2", "price": 750000}]    # no price match anywhere
    matches, _ = AX.match_lots_to_listings(lots, listings)
    reasons = {m[0]["aid"]: m[2] for m in matches}
    assert reasons["a2"] == "remainder"


def test_match_no_price_no_emd_multi_lot_unmatched():
    lots = {"1": _lot(500000), "2": _lot(600000)}
    listings = [{"aid": "a1", "price": None}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "no_listing_price"


def test_match_emd_rescues_listing_without_reserve_price():
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": None, "emd": 60000}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert unmatched == []
    assert matches[0][1]["reserve"] == 600000
    assert matches[0][2] == "emd"


def test_match_emd_tolerance():
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": None, "emd": 60300}]  # within 1%
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][2] == "emd_tolerance"


def test_match_emd_rescues_price_that_matches_no_lot():
    # portal price is a 10x typo, but its EMD still pins the lot
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": 5_000_000, "emd": 50000}]
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][1]["reserve"] == 500000
    assert matches[0][2] == "emd"


def test_match_equal_emds_stay_ambiguous():
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=50000)}
    listings = [{"aid": "a1", "price": None, "emd": 50000}]
    matches, unmatched = AX.match_lots_to_listings(lots, listings)
    assert matches == []
    assert unmatched[0][1] == "ambiguous"


def test_match_price_still_wins_over_emd():
    # price matches lot 1 exactly; a misleading emd points at lot 2
    lots = {"1": _lot(500000, emd=50000), "2": _lot(600000, emd=60000)}
    listings = [{"aid": "a1", "price": 500000, "emd": 60000}]
    matches, _ = AX.match_lots_to_listings(lots, listings)
    assert matches[0][1]["reserve"] == 500000
    assert matches[0][2] == "exact"


def test_group_lots_captures_emd():
    ents = [ent("auction_terms", "",
                {"reserve_price_num": "500000", "emd_num": "50000",
                 "lot_index": "1"})]
    lots = AX.group_lots(ents)
    assert lots["1"]["reserve"] == 500000
    assert lots["1"]["emd"] == 50000


# ── write behavior ───────────────────────────────────────────────────────────

def test_write_descriptions_overwrites_all_but_backs_up_human(monkeypatch):
    """Notice text is the sole source: no human/verified guard remains, and a
    human-entered description is stashed once into description_human_backup."""
    captured = {}

    def _cap(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.write_descriptions([{"aid": "a1", "desc": "D"}])
    assert n == 1
    assert "description_verified" not in captured["cypher"]
    assert "description_human_backup" in captured["cypher"]
    # backup only fills once — a second run must not clobber the stash
    assert "description_human_backup IS NULL" in captured["cypher"]
    # a reviewer's correction outranks the automated write
    assert "<> 'reviewer'" in captured["cypher"]


def test_write_fields_sets_provenance(monkeypatch):
    captured = {}

    def _cap(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"aid": "a1"}]

    monkeypatch.setattr(AX, "run_query", _cap)
    n = AX.write_fields([{"aid": "a1", "filename": "f.pdf",
                          "props": {"village": "Padur"}}])
    assert n == 1
    assert "grounded_extraction" in captured["cypher"]
    assert captured["params"]["rows"][0]["props"] == {"village": "Padur"}
