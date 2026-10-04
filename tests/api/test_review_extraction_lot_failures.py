"""Which lots a failure pill is about (pipeline/validators.validate "lots" +
api/review/extraction.lot_failures), and the stitched pages the notice pane
shows. The pills say a notice has a problem; on a 50-lot notice the key table
has to say which lots, or the reviewer hunts for them."""
from __future__ import annotations

import json

import api.review.extraction as ex
from pipeline.validators import validate_stored

_MD = ("Lot 1: Borrower Mr. Arul. Vacant land of 2400 sq.ft at Ponmeni Village. "
       "Reserve Rs.9,50,000 EMD Rs.95,000. "
       "Lot 2: Borrower Mr. Bala. A flat of 900 sq.ft at Anna Nagar. "
       "Reserve Rs.40,00,000 EMD Rs.20,00,000. Lot 3: A house.")


def _ents():
    md = _MD

    def at(i, cls, text, lot, **attrs):
        s = md.index(text)
        return {"id": i, "cls": cls, "text": text, "start": s, "end": s + len(text),
                "attrs": {"lot_index": lot, **attrs}}
    return [
        at("p1", "property", "Vacant land", "1", property_type="vacant land"),
        at("l1", "location", "Ponmeni Village", "1"),
        at("f1", "full_description", "Vacant land of 2400 sq.ft at Ponmeni Village.", "1"),
        at("e1", "extent", "2400 sq.ft", "1", extent_sqft="2400"),
        at("b1", "borrower", "Mr. Arul", "1"),
        at("r1", "auction_terms", "Reserve Rs.9,50,000", "1",
           reserve_price_num="950000", emd_num="95000"),
        at("p2", "property", "A flat", "2", property_type="flat"),  # no UDS
        at("l2", "location", "Anna Nagar", "2"),
        at("f2", "full_description", "A flat of 900 sq.ft at Anna Nagar.", "2"),
        at("e2", "extent", "900 sq.ft", "2", extent_sqft="900"),
        at("b2", "borrower", "Mr. Bala", "2"),
        at("r2", "auction_terms", "Reserve Rs.40,00,000", "2",
           reserve_price_num="4000000", emd_num="2000000"),        # EMD 50%
        # lot 3 was made up: nothing it says is on the page
        {"id": "p3", "cls": "property", "text": "Property lot 3", "start": None, "end": None,
         "attrs": {"lot_index": "3"}},                              # no type
        {"id": "l3", "cls": "location", "text": "Location for lot 3", "start": None,
         "end": None, "attrs": {"lot_index": "3"}},
    ]


def test_validator_says_which_lot_each_issue_is_about():
    lots = validate_stored(_ents(), source_text=_MD)["lots"]
    codes = {li: {i["code"] for i in items} for li, items in lots.items()}
    assert "missing_uds" in codes["2"] and "missing_property_type" in codes["3"]
    assert "emd_ratio_off" in codes["2"] and "emd_ratio_off" not in codes.get("1", set())
    assert "lot_missing_reserve" in codes["3"]
    assert "lot_missing_reserve" not in codes.get("1", set()) | codes["2"]
    ung = next(i for i in lots["3"] if i["code"] == "ungrounded")
    assert "likely made up" in ung["msg"] and ung["id"] == "p3"
    pt = next(i for i in lots["3"] if i["code"] == "missing_property_type")
    assert pt["id"] == "p3"


def test_per_lot_findings_do_not_change_the_score():
    """One flag per defect kind still: the lots are where, not how much."""
    v = validate_stored(_ents(), source_text=_MD)
    assert len([i for i in v["issues"] if i["code"] == "missing_property_type"]) == 1


def test_lot_failures_maps_codes_to_pills_in_bar_order():
    f = ex.lot_failures(json.dumps(_ents()), "{}", _MD)
    assert [x["failure"] for x in f["2"]] == ["uds", "odd-values"]
    assert f["2"][0]["field_id"] == "p2" and f["2"][1]["field_id"] == "r2"
    assert "1" not in f
    assert {x["failure"] for x in f["3"]} >= {"reserve", "borrower", "property-type",
                                              "ungrounded"}


def test_a_reviewer_added_entity_drops_the_lot_tag():
    cj = json.dumps({"add:u1": {"cls": "extent", "text": "900 sq.ft", "start": _MD.index("900 sq.ft"),
                                "end": _MD.index("900 sq.ft") + 9,
                                "attrs": {"lot_index": "2", "undivided_share": "300 sq.ft"},
                                "by": "a@b.c", "at": "t"}})
    f = ex.lot_failures(json.dumps(_ents()), cj, _MD)
    assert "uds" not in {x["failure"] for x in f["2"]}


def test_evidence_and_place_pills_name_their_lots():
    ents = _ents()
    by_id = {e["id"]: e for e in ents}
    by_id["l1"]["attrs"]["evidence"] = "FUZZY_GROUNDED"
    by_id["l2"]["attrs"]["evidence"] = "CONTESTED"
    f = ex.lot_failures(json.dumps(ents), "{}", _MD,
                        [{"lot": "1", "village": "Ponmeni", "district": "Madurai"},
                         {"lot": "2", "village": None, "district": None}])
    assert {x["failure"] for x in f["1"]} == {"fuzzy", "district-only"}
    assert {"contested", "not-placed"} <= {x["failure"] for x in f["2"]}
    assert "Ponmeni, Madurai" in next(x["why"] for x in f["1"] if x["failure"] == "district-only")


def _row(**over):
    row = {"filename": "a.jpg", "markdown": _MD, "extraction_json": json.dumps(_ents()),
           "corrections_json": "{}", "status": "pending", "expected_lot_count": 3}
    row.update(over)
    return row


def test_detail_tags_each_lot(monkeypatch):
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row())
    lots = {lot.lot_index: lot for lot in ex.extraction_detail("a.jpg", None).keys.lots}
    assert lots["1"].failures == []
    assert lots["2"].failures[0].failure == "uds"
    assert "ungrounded" in {x.failure for x in lots["3"].failures}


def test_detail_lists_every_stitched_page_source(monkeypatch):
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row(
        stitched_pages=["a.jpg", "b.jpg"], stitched_page_offsets=[0, 40],
        stitched_page_sources=[{"filename": "b.jpg", "public_url": "https://r2/b.jpg",
                                "doc_type": "image", "content_type": "image/jpeg"}]))
    out = ex.extraction_detail("a.jpg", None)
    assert [p["filename"] for p in out.stitched_page_sources] == ["a.jpg", "b.jpg"]
    assert out.stitched_page_sources[1]["public_url"] == "https://r2/b.jpg"
    assert out.stitched_page_sources[0]["public_url"] is None   # proxy fallback


def test_a_single_page_notice_lists_no_page_sources(monkeypatch):
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row(stitched_pages=["a.jpg"]))
    assert ex.extraction_detail("a.jpg", None).stitched_page_sources == []
