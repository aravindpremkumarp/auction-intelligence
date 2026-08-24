"""api/agent3/common.py: the pieces every tool shares.

`scope_of`/`scope_note` are the whole scope-honesty mechanism (see
common.py's module docstring) — these pin the `resolved` path added for the
lot resolver alongside the existing single-lot-notice path.
"""
from __future__ import annotations

from api.agent3.common import scope_note, scope_of


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
