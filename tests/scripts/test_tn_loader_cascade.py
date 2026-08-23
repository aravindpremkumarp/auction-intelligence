"""
tests/scripts/test_tn_loader_cascade.py
----------------------------------------
Regression cover for the silent-edge-loss bug in the TN graph pipeline.

The loader used to chain ``WITH a, r WHERE ...`` across all nine MERGE blocks.
In Cypher a filtered-out row skips every clause after it, so one empty field
cost a property every edge below it: an empty ``auction_type`` left 646 of
2,964 properties with no AuctionType *and* no Borrower, while all 2,964 kept
their Bank -- Bank is merged before the first filter.

These tests are structural. The logic they guard lives in Cypher, which cannot
run without a database, so they assert on the shape of the query instead: no
row-filtering guard may sit between the blocks, and no UNWIND may gate one.
That is exactly what would have to reappear for the bug to return.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="module")
def batch_query() -> str:
    from scripts.load_tn_to_neo4j import BATCH_QUERY
    return BATCH_QUERY


# Every optional block, and the field whose emptiness must skip only that block.
OPTIONAL_BLOCKS = [
    ("bank_name", "CONDUCTED_BY"),
    ("branch_name", "LISTED_BY_BRANCH"),
    ("state", "LOCATED_IN_STATE"),
    ("city", "LOCATED_IN_CITY"),
    ("area", "LOCATED_IN_AREA"),
    ("asset_category", "HAS_ASSET_CATEGORY"),
    ("auction_type", "IS_AUCTION_TYPE"),
    ("borrower_name", "HAS_BORROWER"),
]


def test_no_row_filtering_guard_between_blocks(batch_query: str):
    """`WITH ... WHERE` filters the ROW, killing every later block."""
    offenders = re.findall(r"WITH\s+[^\n]*\n\s*WHERE\s+[^\n]*", batch_query)
    assert offenders == [], (
        "row-filtering guard reintroduced -- an empty field here silently drops "
        "every MERGE below it:\n" + "\n".join(offenders)
    )


def test_property_types_does_not_gate_the_row(batch_query: str):
    """UNWIND of an empty list yields zero rows, which drops the row entirely.

    ``UNWIND $rows AS r`` at the top is the batch driver and must stay; the bug
    was a *second* UNWIND partway down, over a list that can be empty.
    """
    unwinds = re.findall(r"^\s*UNWIND\s+(\S+)", batch_query, re.MULTILINE)
    assert unwinds == ["$rows"], (
        "UNWIND over a per-row list reintroduced -- a property with no "
        f"property_types would take AuctionType and Borrower down with it: {unwinds}"
    )
    assert re.search(r"FOREACH\s*\(\s*pt_name\s+IN", batch_query), \
        "property_types should iterate with FOREACH so an empty list skips only its block"


@pytest.mark.parametrize("field,rel", OPTIONAL_BLOCKS)
def test_each_optional_block_is_independently_guarded(batch_query: str, field: str, rel: str):
    """Each relationship must sit inside a FOREACH keyed on its own field."""
    assert rel in batch_query, f"{rel} missing from the loader query"
    guard = re.compile(
        r"FOREACH\s*\(\s*\w+\s+IN\s+CASE\s+WHEN\s+coalesce\(\s*r\." + re.escape(field),
    )
    assert guard.search(batch_query), (
        f"{field} is not FOREACH-guarded; an empty value would not be contained "
        f"to the {rel} block"
    )


def test_auction_type_cannot_suppress_borrower(batch_query: str):
    """The exact 646-property failure: these two must not share a guard."""
    at = batch_query.index("IS_AUCTION_TYPE")
    bw = batch_query.index("HAS_BORROWER")
    between = batch_query[at:bw]
    # Borrower must open its own FOREACH rather than inherit AuctionType's scope.
    assert "FOREACH" in between and "borrower_name" in between, \
        "Borrower must be guarded independently of auction_type"
    assert "WHERE" not in between, \
        "a WHERE between AuctionType and Borrower re-creates the cascade"


def test_bank_merges_before_any_optional_guard(batch_query: str):
    """Bank survived the original bug; keep that ordering property true."""
    assert batch_query.index("CONDUCTED_BY") < batch_query.index("IS_AUCTION_TYPE")
