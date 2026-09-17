"""Unit tests for the bulk-verify reset rule (scripts/reset_bulk_verifications.py).

Covers only `find_clusters` — the pure decision about which verified rows are a
sweep — with no DB, the way the other tests/scripts modules work.
"""
from __future__ import annotations

from scripts.reset_bulk_verifications import DEFAULT_MIN_CLUSTER, find_clusters


def _g(at, n, with_corrections=0, by="a@b.c"):
    return {"at": at, "by": by, "n": n, "with_corrections": with_corrections}


def test_a_large_identical_timestamp_group_is_a_sweep():
    # One Cypher statement stamps every row with the same millisecond; a human
    # clicking verify 1139 times cannot.
    sweeps, kept = find_clusters([_g("2026-08-20T17:44:04.639Z", 1139)],
                                 DEFAULT_MIN_CLUSTER)
    assert [s["n"] for s in sweeps] == [1139]
    assert kept == []


def test_individually_verified_rows_are_kept():
    groups = [_g("2026-08-19T05:35:37.096Z", 1),
              _g("2026-08-19T12:53:13.295Z", 1)]
    sweeps, kept = find_clusters(groups, DEFAULT_MIN_CLUSTER)
    assert sweeps == []
    assert len(kept) == 2


def test_a_group_with_corrections_is_never_reset():
    # Someone edited fields there, so a human demonstrably read it — that
    # outranks the timestamp signature, however large the group.
    sweeps, kept = find_clusters([_g("t", 900, with_corrections=3)],
                                 DEFAULT_MIN_CLUSTER)
    assert sweeps == []
    assert kept[0]["n"] == 900


def test_the_boundary_is_exclusive():
    at_limit = find_clusters([_g("t", DEFAULT_MIN_CLUSTER)], DEFAULT_MIN_CLUSTER)
    over_limit = find_clusters([_g("t", DEFAULT_MIN_CLUSTER + 1)], DEFAULT_MIN_CLUSTER)
    assert at_limit[0] == []                      # equal to the floor -> kept
    assert over_limit[0][0]["n"] == DEFAULT_MIN_CLUSTER + 1


def test_sweeps_and_hand_review_are_separated_in_one_pass():
    groups = [
        _g("2026-08-20T17:44:04.639Z", 1139),   # sweep
        _g("2026-09-05T12:16:47.209Z", 182),    # sweep
        _g("2026-08-19T05:35:37.096Z", 1),      # hand
        _g("2026-08-19T12:53:13.295Z", 1),      # hand
    ]
    sweeps, kept = find_clusters(groups, DEFAULT_MIN_CLUSTER)
    assert sorted(s["n"] for s in sweeps) == [182, 1139]
    assert sorted(k["n"] for k in kept) == [1, 1]
    # Nothing is invented or dropped: every group lands on exactly one side.
    assert len(sweeps) + len(kept) == len(groups)


def test_missing_counts_are_treated_as_zero_not_a_crash():
    sweeps, kept = find_clusters([{"at": "t", "by": None}], DEFAULT_MIN_CLUSTER)
    assert sweeps == []
    assert len(kept) == 1
