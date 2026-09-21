"""Unit tests for pipeline/key_entities.py and the reviewer-added entity
overlay in pipeline/apply_extractions.py."""
from __future__ import annotations

import json

import pipeline.apply_extractions as AX
from pipeline.key_entities import (KEYS, absent_key, absent_marks,
                                   key_checklist, key_checklist_from_stored)


def ent(cls, text="x", attrs=None, id="0", start=0):
    return {"id": id, "cls": cls, "text": text, "start": start,
            "end": start + len(text), "attrs": attrs or {}}


def full_lot(li="1"):
    """One lot with all seven keys filled."""
    return [
        ent("auction_terms", "Reserve Rs.9,50,000", id=f"at{li}",
            attrs={"lot_index": li, "reserve_price_num": "950000",
                   "auction_start_dt": "2026-05-11T11:00"}),
        ent("property", "Vacant land", id=f"p{li}",
            attrs={"lot_index": li, "property_type": "vacant land",
                   "possession_type": "physical"}),
        ent("location", "Situated at X Village", id=f"l{li}",
            attrs={"lot_index": li, "village": "X", "taluk": "Y", "district": "Z"}),
        ent("extent", "1242 sq.ft", id=f"e{li}",
            attrs={"lot_index": li, "extent_sqft": "1242.25"}),
        ent("full_description", "All that piece and parcel ...", id=f"fd{li}",
            attrs={"lot_index": li}),
    ]


def cells(out, li="1"):
    return next(l for l in out["lots"] if l["lot_index"] == li)["cells"]


# ── checklist ────────────────────────────────────────────────────────────────

def test_full_lot_scores_100():
    out = key_checklist(full_lot())
    assert out["score"] == 100
    assert out["missing"] == 0 and out["filled"] == 7 and out["total"] == 7
    c = cells(out)
    assert set(c) == set(KEYS)
    assert c["reserve_price"]["value"] == "₹9,50,000"
    assert c["reserve_price"]["field_id"] == "at1"
    assert c["location"]["value"] == "X, Y, Z"
    assert c["extent"]["value"] == "1242.25 sq.ft"
    assert c["possession_type"]["value"] == "physical"


def test_missing_cells_are_named():
    ents = [e for e in full_lot() if e["cls"] not in ("extent", "location")]
    out = key_checklist(ents)
    c = cells(out)
    assert c["extent"]["status"] == "missing"
    assert c["location"]["status"] == "missing"
    assert out["missing"] == 2
    assert out["missing_labels"] == ["lot 1: location", "lot 1: extent"]
    assert out["score"] == round(100 * 5 / 7)


def test_attribute_without_value_is_missing():
    """A property entity with no possession_type attr leaves the cell empty —
    the entity's presence is not the value."""
    ents = full_lot()
    ents[1]["attrs"].pop("possession_type")
    ents[0]["attrs"]["reserve_price_num"] = "null"
    c = cells(key_checklist(ents))
    assert c["possession_type"]["status"] == "missing"
    assert c["reserve_price"]["status"] == "missing"


def test_absent_mark_counts_as_done():
    ents = full_lot()
    ents[1]["attrs"].pop("possession_type")
    corr = {absent_key("1", "possession_type"): {"by": "a@b.c", "at": "t"}}
    out = key_checklist(ents, corr)
    assert cells(out)["possession_type"]["status"] == "absent"
    assert out["missing"] == 0 and out["absent"] == 1 and out["score"] == 100


def test_absent_mark_never_hides_a_filled_cell():
    corr = {absent_key("1", "extent"): {"by": "a@b.c", "at": "t"}}
    c = cells(key_checklist(full_lot(), corr))
    assert c["extent"]["status"] == "filled"


def test_absent_marks_parser_ignores_junk():
    corr = {"absent:1:extent": {"by": "x"}, "absent:1:nope": {"by": "x"},
            "absent:bad": {}, "3": {"value": "v"}, "absent:2:location": "str"}
    assert absent_marks(corr) == {("1", "extent")}
    assert absent_marks("not a dict") == set()


def test_auction_date_is_inherited_across_lots():
    """One notice-level date, tagged on lot 1 by the model, fills lot 2 too —
    flagged inherited so the UI can say so."""
    ents = full_lot("1") + [e for e in full_lot("2") if e["cls"] != "auction_terms"]
    ents.append(ent("auction_terms", "Reserve 7,73,000", id="at2",
                    attrs={"lot_index": "2", "reserve_price_num": "773000"}))
    out = key_checklist(ents)
    c2 = cells(out, "2")
    assert c2["auction_date"]["status"] == "filled"
    assert c2["auction_date"]["inherited"] is True
    assert c2["auction_date"]["value"] == "2026-05-11T11:00"
    assert cells(out, "1")["auction_date"]["inherited"] is False
    assert out["score"] == 100


def test_expected_lot_count_adds_unextracted_lots():
    """The reviewer counted 3 lots at classification; the model emitted 1. The
    two dropped lots show as all-missing rows — the biggest miss there is."""
    out = key_checklist(full_lot("1"), expected_lot_count=3)
    assert [l["lot_index"] for l in out["lots"]] == ["1", "2", "3"]
    assert [l["extracted"] for l in out["lots"]] == [True, False, False]
    assert out["total"] == 21 and out["filled"] == 7 and out["missing"] == 14
    assert out["score"] == round(100 * 7 / 21)
    assert "lot 2: reserve price" in out["missing_labels"]


def test_expected_lot_count_does_not_remove_extra_lots():
    ents = full_lot("1") + full_lot("2")
    out = key_checklist(ents, expected_lot_count=1)
    assert [l["lot_index"] for l in out["lots"]] == ["1", "2"]


def test_empty_extraction_is_one_missing_lot():
    out = key_checklist([])
    assert out["score"] == 0 and out["total"] == 7 and out["missing"] == 7
    assert out["lots"][0]["lot_index"] == "1"


def test_notice_level_classes_do_not_make_a_lot():
    ents = [ent("secured_creditor", "Bank", attrs={"bank_name": "B"}),
            ent("full_terms", "terms...", id="1")]
    out = key_checklist(ents)
    assert len(out["lots"]) == 1 and out["score"] == 0


def test_lots_sort_numerically():
    ents = full_lot("10") + full_lot("2") + full_lot("1")
    out = key_checklist(ents)
    assert [l["lot_index"] for l in out["lots"]] == ["1", "2", "10"]


def test_extent_falls_back_to_text_and_location_to_text():
    ents = [ent("extent", "2.35 Acres", attrs={"lot_index": "1"}, id="e"),
            ent("location", "Situated at Somewhere", attrs={"lot_index": "1"}, id="l")]
    c = cells(key_checklist(ents))
    assert c["extent"]["value"] == "2.35 Acres"
    assert c["location"]["value"] == "Situated at Somewhere"


# ── reviewer-added entities ──────────────────────────────────────────────────

def test_added_entities_appended_by_overlay():
    ej = json.dumps([ent("property", "Flat", id="0",
                         attrs={"lot_index": "1", "property_type": "flat"})])
    cj = json.dumps({
        "add:abc": {"cls": "extent", "text": "805 sq.ft", "start": 40, "end": 49,
                    "attrs": {"lot_index": "1", "extent_sqft": "805"},
                    "by": "a@b.c", "at": "t"},
        "add:bad": {"cls": "extent", "text": ""},            # no text -> skipped
        "add:ung": {"cls": "location", "text": "X Village",   # start/end junk
                    "start": "12", "end": None, "attrs": {"lot_index": "1"}},
        "absent:1:possession_type": {"by": "a@b.c", "at": "t"},
    })
    out = AX.entities_with_corrections(ej, cj)
    ids = [e["id"] for e in out]
    assert ids == ["0", "add:abc", "add:ung"]
    added = out[1]
    assert added["added"] is True and added["start"] == 40 and added["end"] == 49
    assert added["attrs"] == {"lot_index": "1", "extent_sqft": "805"}
    assert out[2]["start"] is None and out[2]["end"] is None


def test_checklist_from_stored_uses_added_and_absent():
    ej = json.dumps(full_lot())
    ents = json.loads(ej)
    ents = [e for e in ents if e["cls"] != "extent"]
    ents[1]["attrs"].pop("possession_type")
    ej = json.dumps(ents)
    cj = json.dumps({
        "add:1": {"cls": "extent", "text": "1200 sq.ft", "start": 5, "end": 15,
                  "attrs": {"lot_index": "1", "extent_sqft": "1200"}},
        "absent:1:possession_type": {"by": "a@b.c", "at": "t"},
    })
    out = key_checklist_from_stored(ej, cj, expected_lot_count=1)
    c = cells(out)
    assert c["extent"]["status"] == "filled" and c["extent"]["field_id"] == "add:1"
    assert c["possession_type"]["status"] == "absent"
    assert out["score"] == 100
