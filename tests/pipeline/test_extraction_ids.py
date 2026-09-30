from __future__ import annotations

import json

from pipeline.extraction_ids import (assign_ids, carry_corrections, lot_map,
                                     orphans, reanchor_corrections, stable_id)

MD = "Lot 1 Flat G-2 Reserve Rs.35,15,000 EMD Rs.3,51,500. Lot 2 Land Reserve Rs.28,17,600."


def _e(cls, text, start, lot="1", **attrs):
    return {"cls": cls, "text": text, "start": start, "end": start + len(text),
            "attrs": {"lot_index": lot, **attrs}}


OLD = assign_ids([
    _e("full_description", "Flat G-2 Reserve Rs.35,15,000", 6, "1"),
    _e("auction_terms", "Rs.35,15,000", 23, "1", reserve_price_num="3515000"),
    _e("borrower", "Komla SJ", 100, "1"),
    _e("full_description", "Land Reserve Rs.28,17,600", 58, "2"),
    _e("auction_terms", "Rs.28,17,600", 71, "2"),
])


def test_identical_reread_gives_identical_ids():
    again = assign_ids([dict(e, id=None) for e in OLD])
    assert [e["id"] for e in again] == [e["id"] for e in OLD]
    assert len({e["id"] for e in OLD}) == len(OLD)
    assert stable_id(OLD[0]) != stable_id({**OLD[0], "text": "other"})


def test_collisions_get_a_suffix():
    a = _e("extras", "x", 0)
    ids = [e["id"] for e in assign_ids([a, dict(a)])]
    assert ids[0] != ids[1] and ids[1].endswith("-2")


def test_correction_follows_a_moved_span_with_the_same_text():
    new = assign_ids([
        _e("auction_terms", "Rs.35,15,000", 23, "1"),          # unchanged
        _e("borrower", "Komla SJ", 104, "1"),                   # moved 4 chars, same text
        _e("full_description", "Flat G-2 Reserve Rs.35,15,000 EMD", 6, "1"),   # widened: overlaps
    ])
    corr = {OLD[2]["id"]: {"value": "Komala SJ", "by": "a@b"},
            OLD[0]["id"]: {"value": "fixed fd", "by": "a@b"},
            "add:x1": {"cls": "extent", "text": "1 sq.ft", "start": 5, "end": 12,
                       "attrs": {"lot_index": "1"}}}
    moved, report = reanchor_corrections(OLD, new, corr)
    assert moved[new[1]["id"]]["value"] == "Komala SJ"
    assert moved[new[2]["id"]]["value"] == "fixed fd"
    assert moved["add:x1"]["text"] == "1 sq.ft"
    assert report["moved"] == 2 and report["kept"] == 1 and not report.get("orphaned")


def test_renumbered_lot_moves_absent_marks_and_added_entities():
    # the new read numbers the lots the other way round
    new = assign_ids([
        _e("full_description", "Land Reserve Rs.28,17,600", 58, "1"),
        _e("auction_terms", "Rs.28,17,600", 71, "1"),
        _e("full_description", "Flat G-2 Reserve Rs.35,15,000", 6, "2"),
        _e("auction_terms", "Rs.35,15,000", 23, "2"),
    ])
    assert lot_map(OLD, new) == {"1": "2", "2": "1"}
    corr = {"absent:1:possession_type": {"by": "a@b"},
            "unfound:2:extent": {"by": "auto", "rule": "read_missed"},
            "add:y": {"cls": "extent", "text": "1 sq.ft", "attrs": {"lot_index": "2"}},
            OLD[1]["id"]: {"value": "3515000", "by": "a@b"}}
    moved, report = reanchor_corrections(OLD, new, corr)
    assert "absent:2:possession_type" in moved and "unfound:1:extent" in moved
    assert moved["add:y"]["attrs"]["lot_index"] == "1"
    assert moved[new[3]["id"]]["value"] == "3515000"
    assert report.get("orphaned", 0) == 0


def test_nothing_matching_is_orphaned_never_misapplied():
    new = assign_ids([_e("auction_terms", "Rs.99,00,000", 23, "1"),
                      _e("borrower", "Someone Else", 100, "1")])
    corr = {OLD[2]["id"]: {"value": "Komala SJ", "by": "a@b"},
            "absent:2:extent": {"by": "a@b"},
            "7": {"value": "stale positional key"}}
    moved, report = reanchor_corrections(OLD, new, corr)
    assert not any(k == new[1]["id"] for k in moved)          # Someone Else is NOT corrected
    o = moved[f"orphaned:{OLD[2]['id']}"]
    assert o["value"] == "Komala SJ" and o["orphaned_from"]["text"] == "Komla SJ"
    assert "orphaned:absent:2:extent" in moved and "orphaned:7" in moved
    assert report["orphaned"] == 3
    shown = orphans(moved)
    assert {x["key"] for x in shown} == {OLD[2]["id"], "absent:2:extent", "7"}
    # a second re-read keeps orphans as they are
    again, rep2 = reanchor_corrections(new, new, moved)
    assert again == moved and rep2["orphaned_kept"] == 3


def test_positional_old_ids_are_understood():
    old = [dict(e, id=str(i)) for i, e in enumerate(OLD)]
    new = assign_ids([dict(e, id=None) for e in OLD])
    cj = json.dumps({"2": {"value": "Komala SJ", "by": "a@b"}})
    out, report = carry_corrections(json.dumps(old), cj, new)
    assert json.loads(out)[new[2]["id"]]["value"] == "Komala SJ"
    assert report["moved"] == 1
    assert carry_corrections(None, None, new) == ("{}", {})
