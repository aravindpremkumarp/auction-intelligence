from __future__ import annotations

from pipeline.reader.consistency import check


def _terms(lot, **attrs):
    return {"cls": "auction_terms", "text": "x", "start": 10, "end": 11,
            "attrs": {"lot_index": lot, **attrs}}


def test_emd_rules():
    e = _terms("1", reserve_price_num="5000000", emd_num="6000000")
    f = check([e])
    assert [x.rule for x in f] == ["emd_exceeds_reserve"]
    assert e["attrs"]["evidence"] == "CONTESTED" and "emd_num:emd_exceeds_reserve" in e["attrs"]["rule"]
    e = _terms("1", reserve_price_num="5000000", emd_num="5000")
    assert [x.rule for x in check([e])] == ["emd_ratio_off"]
    e = _terms("1", reserve_price_num="5000000", emd_num="500000")
    assert check([e]) == [] and "evidence" not in e["attrs"]


def test_date_rules():
    e = _terms("1", auction_start_dt="2026-10-15T11:00", auction_end_dt="2026-10-14",
               application_deadline_dt="2026-10-16")
    rules = {x.rule for x in check([e], notice_date="2026-09-01")}
    assert rules == {"end_before_start", "deadline_after_start"}
    e = _terms("1", auction_start_dt="2031-10-15")
    assert {x.rule for x in check([e], notice_date="2026-09-01")} == {"date_far_from_notice"}
    p = {"cls": "property", "text": "x", "start": 0, "end": 1,
         "attrs": {"lot_index": "1", "possession_date": "2027-01-01"}}
    assert {x.rule for x in check([p], notice_date="2026-09-01")} == {"possession_after_notice"}


def test_uds_and_extent_rules():
    uds = {"cls": "extent", "text": "600 sq.ft", "start": 0, "end": 9,
           "attrs": {"lot_index": "1", "undivided_share": "2600 sq.ft"}}
    parent = {"cls": "extent", "text": "2257 sq.ft", "start": 20, "end": 30,
              "attrs": {"lot_index": "1", "uds_parent_extent": "2257 sq.ft"}}
    assert {x.rule for x in check([uds, parent])} == {"uds_exceeds_parent"}
    fd = {"cls": "full_description", "start": 0, "end": 60, "attrs": {"lot_index": "1"},
          "text": "All that piece and parcel measuring 1200 sq.ft bounded by road"}
    ext = {"cls": "extent", "text": "2400 sq.ft", "start": 70, "end": 80,
           "attrs": {"lot_index": "1", "total_area": "2400 sq.ft", "extent_sqft": "2400"}}
    assert {x.rule for x in check([fd, ext])} == {"extent_contradicts_description"}
    ext["attrs"]["extent_sqft"] = "1210"
    ext["attrs"].pop("evidence", None)
    ext["attrs"].pop("rule", None)
    assert check([fd, ext]) == []


def test_outside_own_lot():
    e = {"cls": "borrower", "text": "x", "start": 500, "end": 510, "attrs": {"lot_index": "1"}}
    f = check([e], segments={"1": (0, 400)})
    assert [x.rule for x in f] == ["outside_own_lot"]
    sc = {"cls": "secured_creditor", "text": "x", "start": 500, "end": 510, "attrs": {}}
    assert check([sc], segments={"1": (0, 400)}) == []
