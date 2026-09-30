"""scripts/rerun_missing_lots.py: the merge only ever adds lots the new run
does not already have — matched by content, never by lot number — and
reviewer input rides along. Pure logic — no graph, no model."""
from __future__ import annotations

from scripts.rerun_missing_lots import carry_corrections, merge_lots

DESCS = {
    "A": "All that part of 5 cents of land in Sy.No.67/8 of Sreenarayanapuram village",
    "B": "Residential flat No 4B on the second floor of Green Towers, Palamel village",
    "C": "Vacant house site measuring 2400 sq.ft in Survey No 220/5, Block 19 Mavelikkara",
    "D": "Commercial building with 3 shops in TS No 12/4 of Kodungallur town ward 7",
}
RESERVES = {"A": "3273000", "B": "2626000", "C": "1160000", "D": "4550000"}
MD = " ".join(DESCS.values()) + " Bank: Indian Overseas Bank."


def _lot(prop: str, lot: str, id0: int) -> list[dict]:
    text = DESCS[prop]
    s = MD.find(text)
    return [
        {"id": str(id0), "cls": "full_description", "text": text,
         "start": s, "end": s + len(text), "attrs": {"lot_index": lot}},
        {"id": str(id0 + 1), "cls": "auction_terms", "text": "Reserve",
         "start": None, "end": None,
         "attrs": {"lot_index": lot, "reserve_price_num": RESERVES[prop]}},
    ]


def _lots(ents):
    return sorted({e["attrs"].get("lot_index") for e in ents
                   if e["cls"] == "full_description"}, key=int)


def test_carries_a_lot_the_new_run_missed():
    old = _lot("A", "1", 0) + _lot("B", "2", 2)
    new = _lot("A", "1", 0)
    merged, id_map = merge_lots(old, new, MD)
    assert _lots(merged) == ["1", "2"]
    assert set(id_map) == {"2", "3"}
    carried = [e for e in merged if e["id"] == "p2"][0]
    assert MD[carried["start"]:carried["end"]] == DESCS["B"]


def test_renumbered_properties_are_not_duplicated():
    """The notice that broke the first version: the old run gave each
    property its own lot, the new run grouped several under one branch
    number. Old lots 2 and 3 are properties B and C, which the new run holds
    under lot 1 — carrying them by number would list them twice."""
    old = _lot("A", "1", 0) + _lot("B", "2", 2) + _lot("C", "3", 4)
    new = _lot("A", "1", 0) + _lot("B", "1", 2) + _lot("C", "1", 4)
    merged, id_map = merge_lots(old, new, MD)
    assert id_map == {}
    assert len(merged) == len(new)


def test_description_match_catches_a_changed_reserve():
    old = _lot("B", "2", 0)
    new = _lot("B", "5", 0)
    new[1]["attrs"]["reserve_price_num"] = "2500000"   # re-read differently
    merged, id_map = merge_lots(old, new, MD)
    assert id_map == {}


def test_carried_lot_colliding_with_a_new_number_is_renumbered():
    old = _lot("D", "1", 0)
    new = _lot("A", "1", 0) + _lot("B", "2", 2)
    merged, _ = merge_lots(old, new, MD)
    assert _lots(merged) == ["1", "2", "3"]
    d = [e for e in merged if e["text"] == DESCS["D"]][0]
    assert d["attrs"]["lot_index"] == "3"


def test_lot_with_nothing_to_compare_is_not_carried():
    old = [{"id": "0", "cls": "extent", "text": "2 acres", "start": None,
            "end": None, "attrs": {"lot_index": "9"}}]
    merged, id_map = merge_lots(old, _lot("A", "1", 0), MD)
    assert id_map == {}


def test_notice_level_class_carried_only_when_new_run_lacks_it():
    bank = {"id": "9", "cls": "secured_creditor", "text": "Indian Overseas Bank",
            "start": None, "end": None, "attrs": {}}
    merged, id_map = merge_lots([bank], _lot("A", "1", 0), MD)
    assert id_map == {"9": "p9"}
    merged, id_map = merge_lots([bank], _lot("A", "1", 0) + [dict(bank, id="7")], MD)
    assert id_map == {}


def test_span_that_no_longer_lands_is_dropped_not_misplaced():
    old = _lot("D", "4", 0)
    old[0]["text"] = "A description that is nowhere in this markdown at all"
    merged, _ = merge_lots(old, _lot("A", "1", 0), MD, markdown_changed=True)
    carried = [e for e in merged if e["id"] == "p0"][0]
    assert carried["start"] is None


def test_corrections_follow_carried_entities_and_keep_lot_keyed_marks():
    corr = {"1": {"value": "fixed"}, "7": {"value": "gone"},
            "add:ab": {"cls": "extent"}, "absent:2:extent": {"by": "x"}}
    out = carry_corrections(corr, {"1": "p1"})
    assert out == {"p1": {"value": "fixed"}, "add:ab": {"cls": "extent"},
                   "absent:2:extent": {"by": "x"}}
