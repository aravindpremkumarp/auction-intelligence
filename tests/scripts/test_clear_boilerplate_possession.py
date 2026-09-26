"""scripts/clear_boilerplate_possession: what it decides to clear, offline."""
from __future__ import annotations

import json

import scripts.clear_boilerplate_possession as C

TAIL = ("For the properties which are in symbolic possession of the bank, the "
        "Auction purchaser has to comply with the following. 1. The bidder is "
        "purchasing the property in Symbolic Possession at his own risk. 2. Bank "
        "will not be responsible for handing over of physical possession. 5. the "
        "bid EMD amount will be forfeited.")
MD = ("the Symbolic / Constructive / Physical Possession of which has been taken. "
      "Property No.1 land at Sy No 1. Property No.2 house at Door no.419. " + TAIL)


def _e(text, lot, eid, **a):
    s = MD.index(text)
    return {"id": eid, "cls": "property", "text": text, "start": s,
            "end": s + len(text), "attrs": {"lot_index": lot, **a}}


def _row(ents, corr=None, status="pending"):
    return {"filename": "n.jpg", "md": MD, "ej": json.dumps(ents),
            "cj": json.dumps(corr or {}), "status": status, "prior": "[]"}


def test_the_boilerplate_value_is_cleared_and_the_lot_marked_automatically():
    ents = [_e("land at Sy No 1", "1", "a"),
            _e("house at Door no.419", "2", "b", possession_type="symbolic")]
    corr = {"absent:1:possession_type": {"by": "auto", "rule": "not_found"}}
    p = C.plan_doc(_row(ents, corr))
    assert [c["value"] for c in p["cleared"]] == ["symbolic"]
    assert p["lots"] == ["2"] and p["absent"] == ["2"]
    assert all("possession_type" not in e["attrs"] for e in p["ents"])
    mark = p["corrections"]["absent:2:possession_type"]
    assert mark["by"] == "auto" and mark["rule"] == "boilerplate_only"
    # both lots now read the same way
    assert "absent:1:possession_type" in p["corrections"]


def test_a_notice_a_person_has_touched_is_left_alone():
    ents = [_e("house at Door no.419", "2", "b", possession_type="symbolic")]
    fixed = {"b": {"value": "house", "by": "reviewer@example.com"}}
    assert "skip" in C.plan_doc(_row(ents, fixed))
    assert "skip" in C.plan_doc(_row(ents, status="verified"))


def test_nothing_to_clear_when_the_notice_states_the_type():
    md = MD.replace("Symbolic / Constructive / Physical Possession", "Symbolic Possession")
    ents = [_e("house at Door no.419", "2", "b", possession_type="symbolic")]
    row = _row(ents)
    row["md"] = md
    for e in ents:
        e["start"] = md.index(e["text"])
    row["ej"] = json.dumps(ents)
    assert C.plan_doc(row) == {"cleared": []}
