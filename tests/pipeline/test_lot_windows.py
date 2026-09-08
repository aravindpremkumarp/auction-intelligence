"""Unit tests for pipeline/lot_windows.py (window-reset lot renumbering).

Pure functions over entity dicts — no model, no Neo4j. The invariant under test
is the one that matters downstream: two properties from different LangExtract
windows must not end up sharing a lot_index, because promote_extractions turns
that index into the lot_key it MERGEs :Lot on.

Offsets here are realistic, because the detection is: a document is only
treated as multi-window when it is longer than the window char_buffer_for would
have given it (30000 for anything long), and a reset only counts near a
multiple of that. `tail()` sets the document length the entities imply.
"""
from __future__ import annotations

import pipeline.apply_extractions as AX
import pipeline.promote_extractions as P
from pipeline.lot_windows import renumber_window_lots, window_offsets

BUFFER = 30000          # char_buffer_for's ceiling, i.e. the window size


def ent(cls, start, lot_index=None, text="x", **attrs):
    if lot_index is not None:
        attrs["lot_index"] = str(lot_index)
    return {"id": "0", "cls": cls, "text": text, "start": start,
            "end": start + len(text), "attrs": attrs}


def tail(end):
    """A notice-level entity that puts the end of the document at `end`."""
    return {"id": "z", "cls": "full_terms", "text": "t", "start": end - 1,
            "end": end, "attrs": {}}


def indices(entities):
    return [(e.get("attrs") or {}).get("lot_index") for e in entities]


# ── documents that never reset are left strictly alone ───────────────────────

def test_ascending_numbering_is_untouched():
    ents = [ent("property", 1000, 1), ent("property", 29000, 2),
            ent("property", 31000, 3), tail(45000)]
    assert renumber_window_lots(ents) is ents


def test_short_document_is_untouched():
    # Fits one window, so no reset is possible however the model numbered.
    ents = [ent("property", 100, 1), ent("property", 900, 1), tail(3000)]
    assert renumber_window_lots(ents) is ents


def test_gaps_in_numbering_are_not_treated_as_a_reset():
    ents = [ent("property", 1000, 1), ent("property", 20000, 2),
            ent("property", 31000, 5), tail(45000)]
    assert renumber_window_lots(ents) is ents


def test_reset_away_from_a_window_edge_is_ignored():
    # The model re-using lot_index mid-window is its own failure, not a split.
    # Splitting on it would mint lots the notice does not have.
    ents = [ent("property", 1000, 1), ent("property", 5000, 1),
            ent("property", 9000, 1), tail(45000)]
    assert renumber_window_lots(ents) is ents


def test_empty_input_is_untouched():
    assert renumber_window_lots([]) == []


# ── the reset itself ─────────────────────────────────────────────────────────

def test_second_window_continues_instead_of_colliding():
    ents = [ent("property", 1000, 1), ent("property", 20000, 2),
            ent("property", BUFFER + 100, 1), ent("property", 40000, 2),
            tail(45000)]
    assert indices(renumber_window_lots(ents))[:4] == ["1", "2", "3", "4"]


def test_first_window_keeps_its_original_indices():
    # Keys that already resolve must survive the fix untouched.
    ents = [ent("property", 1000, 1), ent("property", BUFFER + 100, 1),
            tail(45000)]
    out = renumber_window_lots(ents)
    assert out[0] is ents[0]
    assert indices(out)[:2] == ["1", "2"]


def test_three_windows_each_continue_from_the_last():
    ents = [ent("property", 100, 1), ent("property", 200, 2),
            ent("property", BUFFER + 100, 1), ent("property", BUFFER + 200, 2),
            ent("property", 2 * BUFFER + 100, 1), tail(75000)]
    assert indices(renumber_window_lots(ents))[:5] == ["1", "2", "3", "4", "5"]


def test_only_the_reset_nearest_an_edge_opens_a_window():
    # Two resets bracket one edge; splitting on both would produce one more
    # lot than the notice has properties.
    ents = [ent("property", 1000, 1), ent("property", 2000, 2),
            ent("property", BUFFER - 600, 1), ent("property", BUFFER + 100, 1),
            tail(50000)]
    out = renumber_window_lots(ents)
    assert len(set(indices(out)[:4])) == 3


def test_a_lots_header_moves_with_it_across_the_edge():
    # These notices head each lot with its borrower and account details, so
    # the property description sits some way into the block. The header must
    # cross the edge with its own property, not stay with the lot before —
    # otherwise one lot's borrower ends up on another's.
    ents = [
        ent("property", 1000, 1), ent("property", 28000, 2),
        ent("boundary", 28100, 2), ent("outstanding", 28200, 2),
        ent("borrower", 29600, 1, text="Second window's borrower"),
        ent("property", BUFFER + 300, 1),
        tail(45000),
    ]
    assert indices(renumber_window_lots(ents))[:6] == \
        ["1", "2", "2", "2", "3", "3"]


def test_a_trailing_child_of_the_previous_lot_does_not_cross():
    # Same gap, but the entity in it still carries the previous lot's index:
    # it is that lot's own tail and must stay put.
    ents = [
        ent("property", 1000, 1), ent("property", 28000, 2),
        ent("boundary", 29600, 2), ent("property", BUFFER + 300, 1),
        tail(45000),
    ]
    assert indices(renumber_window_lots(ents))[:4] == ["1", "2", "2", "3"]


def test_child_entities_move_with_their_window():
    ents = [ent("property", 1000, 1), ent("identifier", 1200, 1),
            ent("property", BUFFER + 100, 1), ent("identifier", BUFFER + 300, 1),
            ent("boundary", BUFFER + 400, 1), tail(45000)]
    assert indices(renumber_window_lots(ents))[:5] == ["1", "1", "2", "2", "2"]


def test_child_entity_is_placed_by_offset_not_list_order():
    # LangExtract does not emit entities sorted by offset; an early-window
    # child arriving after a late-window one must still stay in window 1.
    ents = [ent("property", 1000, 1), ent("property", BUFFER + 100, 1),
            ent("identifier", 38000, 1), ent("identifier", 1500, 1),
            tail(45000)]
    assert indices(renumber_window_lots(ents))[:4] == ["1", "2", "2", "1"]


def test_input_entities_are_never_mutated():
    ents = [ent("property", 1000, 1), ent("property", BUFFER + 100, 1),
            tail(45000)]
    renumber_window_lots(ents)
    assert indices(ents)[:2] == ["1", "1"]


def test_notice_level_entities_without_lot_index_pass_through():
    ents = [ent("secured_creditor", 50), ent("property", 1000, 1),
            ent("property", BUFFER + 100, 1), tail(45000)]
    out = renumber_window_lots(ents)
    assert (out[0].get("attrs") or {}).get("lot_index") is None
    assert indices(out)[1:3] == ["1", "2"]


def test_ungrounded_entity_stays_in_the_first_window():
    # No offset to place it by; it landed on lot 1 before the fix and must
    # keep landing there rather than moving to a different lot.
    ungrounded = {"id": "0", "cls": "identifier", "text": "x",
                  "attrs": {"lot_index": "1"}}
    ents = [ent("property", 1000, 1), ent("property", BUFFER + 100, 1),
            ungrounded, tail(45000)]
    assert indices(renumber_window_lots(ents))[:3] == ["1", "2", "1"]


def test_non_integer_lot_index_is_left_alone():
    ents = [ent("property", 1000, 1), ent("property", BUFFER + 100, 1),
            ent("identifier", BUFFER + 500, "A-2"), tail(45000)]
    assert indices(renumber_window_lots(ents))[2] == "A-2"


def test_window_offsets_reports_the_boundary_shift_and_local_lots():
    ents = [ent("property", 100, 1), ent("property", 200, 2),
            ent("property", BUFFER + 100, 1), tail(45000)]
    assert window_offsets(ents) == [
        (100, 0, frozenset({1, 2})),
        (BUFFER + 100, 2, frozenset({1})),
    ]


def test_child_naming_a_lot_its_window_does_not_have_is_left_alone():
    # Shifting it would create a lot with no property in it. The model does
    # emit these; inventing a lot is not a fix for fusing one.
    ents = [ent("property", 1000, 1), ent("property", BUFFER + 100, 1),
            ent("identifier", BUFFER + 500, 7), tail(45000)]
    assert indices(renumber_window_lots(ents))[:3] == ["1", "2", "7"]


def test_window_offsets_empty_when_numbering_holds():
    ents = [ent("property", 1, 1), ent("property", 31000, 2), tail(45000)]
    assert window_offsets(ents) == []


# ── the downstream invariant: distinct lots, distinct lot_key ────────────────

def test_build_lots_keeps_fused_properties_apart():
    ents = [
        ent("property", 1000, 1, text="First property"),
        ent("borrower", 1100, 1, text="Alice"),
        ent("property", BUFFER + 100, 1, text="Second property"),
        ent("borrower", BUFFER + 200, 1, text="Bob"),
        tail(45000),
    ]
    _, lots = P.build_lots(ents, "n.jpg")
    assert sorted(lot["lot_key"] for lot in lots) == ["n.jpg#1", "n.jpg#2"]
    parties = {lot["lot_key"]: [p["name"] for p in lot["parties"]] for lot in lots}
    assert parties["n.jpg#1"] == ["Alice"]
    assert parties["n.jpg#2"] == ["Bob"]


def test_group_lots_keeps_fused_properties_apart():
    ents = [
        ent("property", 1000, 1, text="First", property_type="flat"),
        ent("property", BUFFER + 100, 1, text="Second", property_type="land"),
        tail(45000),
    ]
    lots = AX.group_lots(ents)
    assert set(lots) == {"1", "2"}
    assert lots["1"]["fields"]["property_type_raw"] == "flat"
    assert lots["2"]["fields"]["property_type_raw"] == "land"
