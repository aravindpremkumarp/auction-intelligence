"""api/agent3/common.py: the pieces every tool shares.

`scope_of`/`scope_note` are the whole scope-honesty mechanism (see
common.py's module docstring) — these pin the `resolved` path added for the
lot resolver alongside the existing single-lot-notice path.
"""
from __future__ import annotations

from api.agent3.common import (
    LISTING_OF_LOT, LOT_OF_LISTING, owns_lot, scope_note, scope_of,
)


def test_a_single_lot_notice_is_lot_scoped_without_resolution():
    assert scope_of(1) == "lot"
    assert scope_note("x", 1) is None


def test_an_unresolved_multi_lot_notice_stays_notice_scoped():
    assert scope_of(6) == "notice"
    note = scope_note("this snippet", 6)
    assert note is not None and "6 lots" in note


def test_a_resolved_multi_lot_notice_reads_as_lot_scoped():
    """The whole point of the resolver: `resolved=True` makes a 6-lot notice
    behave exactly like a single-lot one for scope purposes."""
    assert scope_of(6, resolved=True) == "lot"
    assert scope_note("this snippet", 6, resolved=True) is None


def test_resolved_never_downgrades_an_already_single_lot_notice():
    """`resolved` is an OR, not a replacement — a single-lot notice was
    already lot-scoped and stays that way regardless of what `resolved`
    carries (a resolver has nothing to resolve when there is only one lot)."""
    assert scope_of(1, resolved=False) == "lot"


def test_zero_lot_count_note_still_explains_the_gap_when_unresolved():
    note = scope_note("x", 0)
    assert note is not None and "No sale-notice lot" in note


def test_zero_lot_count_with_resolved_true_is_lot_scoped():
    """A defensive combination that should never occur in practice (the
    resolver never runs without candidate lots) but must not crash or lie
    the other way if it ever did."""
    assert scope_of(0, resolved=True) == "lot"
    assert scope_note("x", 0, resolved=True) is None


# ── owns_lot: which lot a listing's values may come from ─────────────────

def test_owns_lot_prefers_the_resolved_lot():
    assert "(a)-[:IS_LOT]->(l)" in owns_lot()


def test_owns_lot_still_admits_an_unresolved_listing():
    """Without the fallback, the 3 listings carrying no `IS_LOT` edge would
    match no lot at all and vanish from every lot-layer filter."""
    assert "NOT (a)-[:IS_LOT]->(:Lot)" in owns_lot()
    assert " OR " in owns_lot()


def test_owns_lot_binds_the_names_it_is_given():
    """cypher_tools calls the listing `p`, not `a`."""
    pred = owns_lot("p", "hit")
    assert "(p)-[:IS_LOT]->(hit)" in pred
    assert "(a)-[:IS_LOT]" not in pred


def test_the_two_paths_walk_the_same_edge_in_both_directions():
    for path in (LOT_OF_LISTING, LISTING_OF_LOT):
        assert "HAS_DOCUMENT" in path and "HAS_LOT" in path
        assert owns_lot() in path
    assert LOT_OF_LISTING.startswith("(a)-[:HAS_DOCUMENT]->")
    assert LISTING_OF_LOT.startswith("(l)<-[:HAS_LOT]-")
