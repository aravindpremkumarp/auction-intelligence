"""pipeline/lot_order: lots numbered in the notice's own order."""
from __future__ import annotations

import pipeline.lot_order as O


def _e(cls, text, md, lot, **attrs):
    s = md.index(text)
    return {"cls": cls, "text": text, "start": s, "end": s + len(text),
            "attrs": {"lot_index": lot, **attrs}}


def _notice(texts):
    return "SALE NOTICE\n\n" + "\n\n".join(texts) + "\n"


def test_reversed_lots_are_numbered_in_reading_order():
    md = _notice([f"Property at Survey {c}" for c in "ABC"])
    ents = [_e("full_description", "Property at Survey A", md, "3"),
            _e("full_description", "Property at Survey B", md, "2"),
            _e("full_description", "Property at Survey C", md, "1")]
    mapping, why = O.plan(ents, md)
    assert mapping == {"3": "1", "1": "3"} and why.startswith("reading order")


def test_a_gap_is_kept_not_closed():
    # lot 2 was never read: 1, 3, 4 in order stays 1, 3, 4
    md = _notice([f"Property at Survey {c}" for c in "ACD"])
    ents = [_e("full_description", f"Property at Survey {c}", md, li)
            for c, li in zip("ACD", ("1", "3", "4"))]
    assert O.plan(ents, md) == ({}, "already in order")
    # and a swap within a gapped set only swaps
    ents[1]["attrs"]["lot_index"], ents[2]["attrs"]["lot_index"] = "4", "3"
    assert O.plan(ents, md)[0] == {"4": "3", "3": "4"}


def test_printed_numbers_win_over_reading_order():
    # two columns: the notice prints 1, 3, 2, 4 down the page
    md = _notice([f"<td>{n}</td> Property at Survey {c}"
                  for n, c in zip((1, 3, 2, 4), "ABCD")])
    ents = [_e("full_description", f"Property at Survey {c}", md, str(n))
            for n, c in zip((1, 3, 2, 4), "ABCD")]
    # reading order says B is lot 2, but the notice prints 3 beside it
    assert O.plan(ents, md) == ({}, "already in order")


def test_partial_printed_numbers_leave_the_lots_alone():
    md = _notice(["<td>1</td> Property at Survey A", "<td>3</td> Property at Survey B",
                  "<td>2</td> Property at Survey C", "Property at Survey D"])
    ents = [_e("full_description", f"Property at Survey {c}", md, li)
            for c, li in zip("ABCD", ("1", "3", "2", "4"))]
    mapping, why = O.plan(ents, md)
    assert mapping == {} and "printed lot numbers found for 3 of 4" in why


def test_pages_joined_out_of_order_are_flagged_not_renumbered():
    md = _notice([f"Property at Survey {c}" for c in "ABCDEF"])
    ents = [_e("full_description", f"Property at Survey {c}", md, li)
            for c, li in zip("ABCDEF", ("4", "5", "6", "1", "2", "3"))]
    mapping, why = O.plan(ents, md)
    assert mapping == {} and "page order" in why


def test_lots_sharing_one_description_are_refused():
    md = _notice(["Property at Survey A"])
    ents = [_e("full_description", "Property at Survey A", md, "1"),
            _e("full_description", "Property at Survey A", md, "2")]
    assert O.plan(ents, md)[0] == {}


def test_apply_moves_marks_and_added_entities_with_their_lot():
    md = _notice(["Property at Survey A", "Property at Survey B"])
    ents = [_e("full_description", "Property at Survey A", md, "2"),
            _e("full_description", "Property at Survey B", md, "1")]
    corr = {"absent:2:extent": {"by": "rev"}, "unfound:1:reserve_price": {"by": "auto"},
            "add:x": {"cls": "property", "attrs": {"lot_index": "2", "possession_type": "physical"}},
            "f7": {"value": "corrected text"}}
    mapping, _ = O.plan(ents, md)
    new_ents, new_corr = O.apply(ents, corr, mapping)
    assert [e["attrs"]["lot_index"] for e in new_ents] == ["1", "2"]
    assert set(new_corr) == {"absent:1:extent", "unfound:2:reserve_price", "add:x", "f7"}
    assert new_corr["add:x"]["attrs"]["lot_index"] == "1"
    assert ents[0]["attrs"]["lot_index"] == "2"        # inputs untouched
