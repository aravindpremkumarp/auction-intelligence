"""scripts/rerun_missing_lots.py: the merge only ever adds lots, and reviewer
input rides along. Pure logic — no graph, no model."""
from __future__ import annotations

from scripts.rerun_missing_lots import carry_corrections, merge_lots

MD = "Lot 1 flat A. Lot 2 flat B. Lot 3 flat C. Bank: SBI."


def _e(i, cls, text, lot=None):
    s = MD.find(text)
    attrs = {"lot_index": lot} if lot else {}
    return {"id": str(i), "cls": cls, "text": text,
            "start": s if s >= 0 else None, "end": s + len(text) if s >= 0 else None,
            "attrs": attrs}


def test_keeps_new_lots_and_carries_only_missing_ones():
    old = [_e(0, "property", "flat A", "1"), _e(1, "property", "flat B", "2"),
           _e(2, "secured_creditor", "SBI")]
    new = [_e(0, "property", "flat A", "1"), _e(1, "property", "flat C", "3"),
           _e(2, "secured_creditor", "SBI")]
    merged, id_map = merge_lots(old, new, MD)
    lots = sorted({(e["attrs"] or {}).get("lot_index") for e in merged if e["cls"] == "property"})
    assert lots == ["1", "2", "3"]
    assert id_map == {"1": "p1"}                    # only old lot 2 carried
    carried = [e for e in merged if e["id"] == "p1"][0]
    assert MD[carried["start"]:carried["end"]] == "flat B"
    assert sum(e["cls"] == "secured_creditor" for e in merged) == 1


def test_notice_level_class_carried_when_new_run_lacks_it():
    old = [_e(0, "secured_creditor", "SBI")]
    new = [_e(0, "property", "flat A", "1")]
    merged, id_map = merge_lots(old, new, MD)
    assert id_map == {"0": "p0"} and len(merged) == 2


def test_span_that_no_longer_lands_is_dropped_not_misplaced():
    old = [{"id": "5", "cls": "property", "text": "flat Z", "start": 0, "end": 6,
            "attrs": {"lot_index": "9"}}]
    merged, _ = merge_lots(old, [], MD, markdown_changed=True)
    assert merged[0]["start"] is None and merged[0]["text"] == "flat Z"


def test_corrections_follow_carried_entities_and_keep_lot_keyed_marks():
    corr = {"1": {"value": "fixed"}, "7": {"value": "gone"},
            "add:ab": {"cls": "extent"}, "absent:2:extent": {"by": "x"}}
    out = carry_corrections(corr, {"1": "p1"})
    assert out == {"p1": {"value": "fixed"}, "add:ab": {"cls": "extent"},
                   "absent:2:extent": {"by": "x"}}
