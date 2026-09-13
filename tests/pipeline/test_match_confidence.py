"""Unit tests for pipeline/match_confidence.py — the IS_LOT.confidence grade.

The point of this file is the guard at the bottom: a matching tier must never
become high-confidence because somebody added it to the matcher and forgot the
grade. `confidence_for` degrades an unknown name to UNKNOWN at runtime (it runs
inside a batch write and must not abort one); these tests are the loud half.
"""
from __future__ import annotations

import pytest

import pipeline.apply_extractions as AX
from pipeline.match_confidence import (
    CONFIRMED, INFERRED, MATCH_CONFIDENCE, PROBABLE, UNKNOWN,
    UNMATCHED_REASONS, confidence_for,
)


# ── every method currently on an edge ────────────────────────────────────────

@pytest.mark.parametrize("method, expected", [
    # deterministic, or a person decided
    ("single", CONFIRMED),
    ("exact", CONFIRMED),
    ("decision", CONFIRMED),
    # a real signal that can still land on the wrong lot
    ("tolerance", PROBABLE),
    ("emd", PROBABLE),
    ("emd_tolerance", PROBABLE),
    ("identifier", PROBABLE),
    # inference
    ("borrower", INFERRED),
    ("portal_aid", INFERRED),
    ("description", INFERRED),
    ("remainder", INFERRED),
])
def test_grade_of_each_live_method(method, expected):
    assert confidence_for(method) == expected


# ── the guard: an unknown tier is never CONFIRMED ────────────────────────────

@pytest.mark.parametrize("method", [
    "a_tier_nobody_has_written_yet",
    "EXACT",          # case matters — the writer stores lowercase
    "exact ",         # and so does whitespace
    "",
    None,
    123,              # a non-string can reach here from a malformed row
    ["exact"],
])
def test_unknown_method_is_unknown_never_confirmed(method):
    assert confidence_for(method) == UNKNOWN
    assert confidence_for(method) != CONFIRMED


def test_unknown_is_not_silently_one_of_the_real_grades():
    assert UNKNOWN not in (CONFIRMED, PROBABLE, INFERRED)


# ── the guard: the vocabulary and the mapping cannot drift apart ─────────────

def test_every_matcher_reason_is_either_graded_or_declared_unmatched():
    """A new reason in `_EXPLAIN_TEXT` must be a deliberate choice.

    `_EXPLAIN_TEXT` is the full reason vocabulary — the ones that link a
    listing to a lot and the ones that explain why it was left unlinked. Each
    must be exactly one of: graded in MATCH_CONFIDENCE, or named in
    UNMATCHED_REASONS as unable to reach an edge. Adding a matching tier
    without a grade fails here rather than shipping as UNKNOWN.
    """
    vocabulary = set(AX._EXPLAIN_TEXT)
    graded = set(MATCH_CONFIDENCE)
    accounted = graded | set(UNMATCHED_REASONS)

    assert not (vocabulary - accounted), (
        "reason(s) in _EXPLAIN_TEXT with no confidence grade and not declared "
        f"unmatched: {sorted(vocabulary - accounted)}")
    assert not (graded & UNMATCHED_REASONS), (
        "reason(s) both graded and declared unreachable: "
        f"{sorted(graded & UNMATCHED_REASONS)}")


def test_graded_reasons_beyond_the_matcher_are_intentional():
    """`decision` is graded but is not a matcher reason — it is written by
    scripts/resolve_lots.py when a human's stored verdict is applied, so it
    never appears in `_EXPLAIN_TEXT`. It is the only such name; anything else
    means a grade was written for a reason that cannot occur."""
    assert set(MATCH_CONFIDENCE) - set(AX._EXPLAIN_TEXT) == {"decision"}


def test_unmatched_reasons_never_reach_an_edge():
    for reason in UNMATCHED_REASONS:
        assert reason not in MATCH_CONFIDENCE
        assert confidence_for(reason) == UNKNOWN


def test_every_grade_is_one_of_the_three():
    assert set(MATCH_CONFIDENCE.values()) == {CONFIRMED, PROBABLE, INFERRED}


# ── SAME_LISTING_AS: the cross-portal bridge has its own table ───────────────

def test_same_listing_methods_match_the_matcher_and_are_graded():
    """`sources.match.METHODS` is the vocabulary that can reach a
    SAME_LISTING_AS edge; each must be graded, and nothing else may be."""
    from pipeline.match_confidence import SAME_LISTING_CONFIDENCE, listing_confidence_for
    from sources.match import METHODS

    assert set(SAME_LISTING_CONFIDENCE) == set(METHODS)
    assert set(SAME_LISTING_CONFIDENCE.values()) == {CONFIRMED, PROBABLE, INFERRED}
    assert listing_confidence_for("notice_bytes") == CONFIRMED
    assert listing_confidence_for("bucket_only") == INFERRED
    for bad in ("exact", "", None, 7, "NOTICE_BYTES"):
        assert listing_confidence_for(bad) == UNKNOWN


def test_same_listing_table_does_not_leak_into_is_lot_grades():
    """`borrower` is PROBABLE on the bridge and INFERRED on IS_LOT; the two
    tables must not be one."""
    from pipeline.match_confidence import SAME_LISTING_CONFIDENCE, listing_confidence_for

    assert listing_confidence_for("borrower") == PROBABLE
    assert confidence_for("borrower") == INFERRED
    assert confidence_for("notice_bytes") == UNKNOWN
    assert "notice_bytes" not in MATCH_CONFIDENCE
    assert set(SAME_LISTING_CONFIDENCE) - set(MATCH_CONFIDENCE) == {"notice_bytes", "boundaries", "bucket_only"}


# ── the write path stamps it ─────────────────────────────────────────────────

def test_write_lot_matches_grades_every_row(monkeypatch):
    """`write_lot_matches` must put `confidence` on each row it sends, or the
    Cypher writes null."""
    seen: list[dict] = []

    def fake_run_query(cypher, params=None):
        if params and "rows" in params and "IS_LOT" in cypher and "MERGE" in cypher:
            seen.extend(params["rows"])
            return [{"aid": r["aid"]} for r in params["rows"]]
        return []

    monkeypatch.setattr(AX, "run_query", fake_run_query)

    AX.write_lot_matches([
        {"aid": "1", "lot_key": "f.jpg#1", "reason": "exact", "filename": "f.jpg"},
        {"aid": "2", "lot_key": "f.jpg#2", "reason": "borrower", "filename": "f.jpg"},
        {"aid": "3", "lot_key": "f.jpg#3", "reason": "not_a_tier", "filename": "f.jpg"},
    ])

    graded = {r["aid"]: r["confidence"] for r in seen}
    assert graded == {"1": CONFIRMED, "2": INFERRED, "3": UNKNOWN}
