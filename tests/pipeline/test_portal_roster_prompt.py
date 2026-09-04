"""The portal-roster prompt block.

The roster is reference context, not a source of values: LangExtract grounds
every extraction to a character interval in the notice, so a value copied out of
the roster has no honest span. These tests pin the two properties that keep that
true — the block always says "don't copy" and always says "this is not lot
order" — plus the framing that it renders nothing at all when there is nothing
to show, so a notice without portal listings gets a byte-identical prompt.
"""
from __future__ import annotations

import pytest

from pipeline.langextract_examples import (MAX_ROSTER_ROWS, PROMPT_DESCRIPTION,
                                           portal_roster_block,
                                           prompt_description_for)


def _row(**over):
    base = {"reserve": 9400000.0, "emd": 940000.0, "village": "Karai",
            "district": "Vellore", "area": "7000 sqft", "ptype": "industrial"}
    base.update(over)
    return base


def _identified(**over):
    """A row carrying everything the model needs to name it: an id to quote
    back, plus the two fields that separate lots money cannot."""
    base = {"aid": "796269", "borrower": "M/s Sri Vaaru Traders",
            "desc": "Second floor flat, Varadha Muthiappan Street, "
                    "George Town, Chennai"}
    base.update(over)
    return _row(**base)


# ── renders nothing when there is nothing ──────────────────────────────────

@pytest.mark.parametrize("roster", [None, [], [{}], [{"reserve": None}],
                                    [{"village": None, "emd": None}]])
def test_empty_roster_leaves_the_prompt_untouched(roster):
    assert portal_roster_block(roster) == ""
    assert prompt_description_for(None, roster) == PROMPT_DESCRIPTION


def test_a_row_with_only_a_village_still_renders():
    """Sparse rows are common; any one usable field is worth showing."""
    assert "Nowhere" in portal_roster_block([{"village": "Nowhere"}])


# ── the two guardrails ─────────────────────────────────────────────────────

def test_block_forbids_copying_values():
    block = portal_roster_block([_row()])
    assert "NEVER copy a value" in block
    assert "verbatim" in block
    assert "what the NOTICE says" in block


def test_block_disclaims_lot_ordering():
    """Portal row order is arbitrary — if the model read it as lot order it
    would assign lot_index from the wrong sequence."""
    block = portal_roster_block([_row(), _row(reserve=1)])
    assert "no particular order" in block
    assert "lot_index" in block


def test_block_marks_itself_as_not_the_notice():
    block = portal_roster_block([_row()])
    assert "reference only" in block.lower()
    assert "NOT" in block and "notice text" in block


# ── content ────────────────────────────────────────────────────────────────

def test_row_carries_the_matching_fields():
    """These are the fields apply_extractions already matches lots on."""
    block = portal_roster_block([_row()])
    assert "reserve 9400000" in block      # integer rupees, as the schema wants
    assert "emd 940000" in block
    assert "Karai Vellore" in block
    assert "7000 sqft" in block
    assert "industrial" in block


def test_row_count_is_stated():
    assert "lists 3 lot(s)" in portal_roster_block([_row(), _row(), _row()])


def test_long_roster_is_truncated_not_dropped():
    rows = [_row(reserve=i + 1) for i in range(MAX_ROSTER_ROWS + 7)]
    block = portal_roster_block(rows)
    # Count data lines only — the instruction bullets share the "  - " prefix.
    data = [ln for ln in block.splitlines() if ln.startswith("  - reserve ")]
    assert len(data) == MAX_ROSTER_ROWS
    assert "+7 further listings not shown" in block
    assert f"lists {MAX_ROSTER_ROWS + 7} lot(s)" in block, "full count still honest"


# ── composition with the lot-count hint ────────────────────────────────────

# ── portal_aid: the one thing the model may take from the roster ───────────

def test_row_is_labelled_with_the_auction_id():
    """The label is the whole mechanism — it is what the model quotes back as
    portal_aid, so the matcher can look the listing up."""
    assert "listing 796269:" in portal_roster_block([_identified()])


def test_row_without_an_auction_id_renders_unlabelled():
    """Such a row simply cannot be claimed; it still helps segmentation."""
    block = portal_roster_block([_row()])
    assert "listing" not in block.split("=== portal_aid")[-1].split("  - ")[-1]
    assert "  - reserve 9400000" in block


def test_row_carries_borrower_and_description():
    block = portal_roster_block([_identified()])
    assert "borrower M/s Sri Vaaru Traders" in block
    assert "Varadha Muthiappan Street" in block


def test_long_description_is_clipped_not_dropped():
    block = portal_roster_block([_identified(desc="x " * 400)])
    data = [ln for ln in block.splitlines() if ln.startswith("  - listing ")]
    assert len(data) == 1
    assert len(data[0]) < 400, "one row must not swamp the notice"
    assert "…" in data[0]


def test_block_asks_for_portal_aid_and_bounds_it():
    block = portal_roster_block([_identified()])
    assert "portal_aid" in block
    # the three rules that keep a claim honest
    assert "OMIT it when you are not sure" in block
    assert "Never put the same portal_aid on two lots" in block
    assert "checked against the portal's own figures" in block


def test_portal_aid_is_not_declared_in_the_static_guide():
    """It only exists when a roster does. A notice with no portal rows must
    not be told about an attribute it has no way to fill."""
    assert "portal_aid" not in PROMPT_DESCRIPTION
    assert "portal_aid" not in prompt_description_for(2, None)


def test_roster_composes_with_the_lot_count_hint():
    out = prompt_description_for(2, [_row(), _row(reserve=1)])
    assert "EXACTLY 2" in out           # the existing hint survives
    assert "PORTAL LISTINGS" in out     # and the roster is appended
    assert out.startswith(PROMPT_DESCRIPTION)


def test_lot_count_alone_is_unchanged_by_this_feature():
    assert prompt_description_for(3) == prompt_description_for(3, None)


def test_roster_without_a_lot_count_still_renders():
    out = prompt_description_for(None, [_row()])
    assert "PORTAL LISTINGS" in out
    assert "A human reviewer confirmed" not in out
