"""pipeline/widen_descriptions: a lot's full_description is stretched over the
details it stopped short of, and never past another lot, a price, or the next
property's heading. Pure — entities built over a page, no graph, no model."""
from __future__ import annotations

from pipeline.validators import validate_stored
from pipeline.widen_descriptions import MAX_GAP, widen

FD = ("All that piece and parcel of land bearing S.F.No.179/6, Ponmeni Village, "
      "Madurai South Taluk, measuring 1200 sq.ft.")
BOUNDS = "Bounded by: North by: Vacant Land, South by: 30 feet road."


def ent(page, cls, text, lot="1", **attrs):
    s = page.index(text)
    return {"id": None, "cls": cls, "text": text, "start": s, "end": s + len(text),
            "attrs": {"lot_index": lot, **attrs}}


def block(ents, lot="1"):
    return next(e for e in ents if e["cls"] == "full_description"
                and e["attrs"]["lot_index"] == lot)


def codes(ents, page):
    return {i["code"] for i in validate_stored(ents, page)["issues"]}


def test_boundaries_after_the_block_are_taken_in():
    page = FD + " " + BOUNDS + "\nAuction terms follow."
    ents = [ent(page, "full_description", FD),
            ent(page, "boundary", "North by: Vacant Land"),
            ent(page, "boundary", "South by: 30 feet road")]
    assert "full_description_incomplete" in codes(ents, page)
    new, rep = widen(ents, page)
    b = block(new)
    assert b["text"] == page[b["start"]:b["end"]]
    assert b["text"].endswith("30 feet road.")         # finished its line
    assert b["attrs"]["widened_from"] == [0, len(FD)]
    assert rep["1"]["reached"] == 2 and rep["1"]["left"] == 0
    assert "full_description_incomplete" not in codes(new, page)


def test_a_reworded_block_is_judged_by_the_page_text_under_it():
    """The model flattened the survey table (stored as HTML) into its own text,
    so its words name every survey number while its span stops before the
    table. The block is stretched over the table and takes the page's text."""
    head = "1) All that piece and parcel of Factory land, With the following Survey Nos."
    table = ("<table><tr><td>Survey no.</td><td>Cents</td></tr>"
             "<tr><td>199/1A</td><td>22</td></tr>"
             "<tr><td>206/1D5</td><td>02 $\\frac{1}{2}$</td></tr></table>")
    page = head + "\n\n" + table + "\n\nReserve Price : Rs. 58,66,560/-"
    fd = {"id": None, "cls": "full_description", "start": 0, "end": len(head),
          "text": head + " 199/1A 22; 206/1D5 02 ½.", "attrs": {"lot_index": "1"}}
    ents = [fd, ent(page, "identifier", "199/1A", kind="survey_old"),
            ent(page, "identifier", "206/1D5", kind="survey_old")]
    new, rep = widen(ents, page)
    b = block(new)
    assert b["start"] == 0 and b["end"] >= page.index("</table>")
    assert b["text"] == page[b["start"]:b["end"]]
    assert "Reserve Price" not in b["text"]
    assert rep["1"]["reached"] == 2


def test_a_schedule_before_the_block_is_taken_in():
    sched = "Schedule A: land in S.No.88/2, Ponmeni Village measuring 3 acres."
    page = sched + "\nThe property mortgaged:\n" + FD
    ents = [ent(page, "full_description", FD), ent(page, "schedule", sched)]
    new, _ = widen(ents, page)
    assert block(new)["start"] == 0


def test_a_detail_too_far_away_is_left_flagged():
    page = FD + " " + ("x" * (MAX_GAP + 50)) + " " + BOUNDS
    ents = [ent(page, "full_description", FD), ent(page, "boundary", "South by: 30 feet road")]
    new, rep = widen(ents, page)
    assert new is ents and rep["1"]["left"] == 1


def test_it_never_crosses_a_price():
    page = FD + " Reserve Price: Rs. 21,30,319/- " + BOUNDS
    ents = [ent(page, "full_description", FD), ent(page, "boundary", "South by: 30 feet road")]
    new, _ = widen(ents, page)
    assert new is ents


def test_it_never_crosses_the_next_property():
    page = FD + "\nItem No-02: house site " + BOUNDS
    ents = [ent(page, "full_description", FD), ent(page, "boundary", "South by: 30 feet road")]
    assert widen(ents, page)[0] is ents
    page = FD + "\nItem No. II: house site " + BOUNDS
    ents = [ent(page, "full_description", FD), ent(page, "boundary", "South by: 30 feet road")]
    assert widen(ents, page)[0] is ents


def test_it_never_crosses_another_lots_entities_or_a_borrower():
    fd2 = "Lot two: a flat in Block C."
    page = FD + " " + fd2 + " " + BOUNDS
    ents = [ent(page, "full_description", FD, lot="1"),
            ent(page, "full_description", fd2, lot="2"),
            ent(page, "boundary", "South by: 30 feet road", lot="1")]
    assert block(widen(ents, page)[0], "1")["end"] == len(FD)
    page = FD + " Borrower: Mr. Ravi. " + BOUNDS
    ents = [ent(page, "full_description", FD), ent(page, "borrower", "Mr. Ravi"),
            ent(page, "boundary", "South by: 30 feet road")]
    assert widen(ents, page)[0] is ents


def test_it_stops_at_the_first_detail_it_may_not_reach():
    """Nearest first: the near boundary is taken, the one past a price is not."""
    page = FD + " North by: Vacant Land. EMD Rs. 2,13,000 South by: 30 feet road."
    ents = [ent(page, "full_description", FD),
            ent(page, "boundary", "North by: Vacant Land"),
            ent(page, "boundary", "South by: 30 feet road")]
    new, rep = widen(ents, page)
    assert "North by: Vacant Land" in block(new)["text"]
    assert "South by" not in block(new)["text"]
    assert rep["1"]["reached"] == 1 and rep["1"]["left"] == 1


def test_excused_details_do_not_move_the_block():
    page = FD + " Property ID: IDIB6622770038"
    ents = [ent(page, "full_description", FD),
            ent(page, "identifier", "Property ID: IDIB6622770038", kind="property_id")]
    new, rep = widen(ents, page)
    assert new is ents and rep == {}


def test_a_multi_part_block_moves_its_outer_parts_only():
    part2 = "Plot No.12 in the same layout."
    page = FD + "\n" + part2 + " " + BOUNDS + "\n"
    ents = [ent(page, "full_description", FD), ent(page, "full_description", part2),
            ent(page, "boundary", "South by: 30 feet road")]
    new, _ = widen(ents, page)
    first, last = [e for e in new if e["cls"] == "full_description"]
    assert (first["start"], first["end"]) == (0, len(FD))       # untouched
    assert last["text"].endswith("30 feet road.")


def test_widening_is_idempotent_and_refreshes_ids():
    page = FD + " " + BOUNDS
    ents = [ent(page, "full_description", FD), ent(page, "boundary", "South by: 30 feet road")]
    once, _ = widen(ents, page)
    twice, _ = widen(once, page)
    assert twice is once
    assert all(e["id"] for e in once)
