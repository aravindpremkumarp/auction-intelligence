"""Unit tests for field-level provenance on :AuctionProperty.

`notice_fields_lot` vs `notice_fields_consensus` is the difference between
"this listing's village, read off the one lot it was confirmed to be" and "a
village every lot on the notice shares, so true whichever lot it is". The node
used to state both as flat fact.
"""
from __future__ import annotations

import pipeline.apply_extractions as AX
from pipeline.apply_extractions import (
    SCOPE_CONSENSUS, SCOPE_LOT, consensus_and_contested, field_provenance,
)


def row(aid, props, scope, filename="n.jpg"):
    return {"aid": aid, "filename": filename, "props": props, "scope": scope}


# ── field_provenance: the split ──────────────────────────────────────────────

def test_lot_and_consensus_are_separated():
    out = field_provenance([
        row("1", {"village": "Perungudi", "taluk": "Sholinganallur"}, SCOPE_LOT),
        row("2", {"taluk": "Sholinganallur"}, SCOPE_CONSENSUS),
    ])
    assert out["1"] == {SCOPE_LOT: ["taluk", "village"], SCOPE_CONSENSUS: []}
    assert out["2"] == {SCOPE_LOT: [], SCOPE_CONSENSUS: ["taluk"]}


def test_the_two_lists_are_disjoint_and_sorted():
    out = field_provenance([
        row("1", {"z": 1, "a": 2}, SCOPE_LOT),
        row("1", {"m": 3}, SCOPE_CONSENSUS),
    ])
    lot, cons = out["1"][SCOPE_LOT], out["1"][SCOPE_CONSENSUS]
    assert lot == sorted(lot) and cons == sorted(cons)
    assert not set(lot) & set(cons)


def test_a_listing_with_no_rows_is_absent_not_empty():
    assert field_provenance([]) == {}
    assert "9" not in field_provenance([row("1", {"a": 1}, SCOPE_LOT)])


def test_a_row_with_no_props_contributes_nothing():
    assert field_provenance([row("1", {}, SCOPE_LOT)]) == {}


# ── provenance follows the value, not the other way round ────────────────────

def test_a_later_row_wins_the_scope_because_it_wins_the_value():
    """12 listings link to two scans of one notice. `SET +=` applies both in
    order, so the later row's value survives — its scope must too."""
    out = field_provenance([
        row("1", {"village": "A"}, SCOPE_CONSENSUS, filename="scan1.jpg"),
        row("1", {"village": "B"}, SCOPE_LOT, filename="scan2.jpg"),
    ])
    assert out["1"] == {SCOPE_LOT: ["village"], SCOPE_CONSENSUS: []}


def test_the_same_holds_in_the_other_direction():
    out = field_provenance([
        row("1", {"village": "A"}, SCOPE_LOT, filename="scan1.jpg"),
        row("1", {"village": "B"}, SCOPE_CONSENSUS, filename="scan2.jpg"),
    ])
    assert out["1"] == {SCOPE_LOT: [], SCOPE_CONSENSUS: ["village"]}


def test_a_field_only_the_first_row_carries_keeps_its_own_scope():
    out = field_provenance([
        row("1", {"village": "A", "taluk": "T"}, SCOPE_LOT),
        row("1", {"village": "B"}, SCOPE_CONSENSUS),
    ])
    assert out["1"] == {SCOPE_LOT: ["taluk"], SCOPE_CONSENSUS: ["village"]}


def test_a_row_with_no_scope_is_treated_as_consensus():
    """The cautious default: consensus claims less than lot does."""
    out = field_provenance([{"aid": "1", "props": {"village": "A"}}])
    assert out["1"] == {SCOPE_LOT: [], SCOPE_CONSENSUS: ["village"]}


# ── agreement with consensus_and_contested ───────────────────────────────────

def test_a_single_lot_notice_has_no_contested_fields():
    lots = {"1": {"fields": {"village": "Perungudi", "extent_sqft": 1200}}}
    consensus, contested = consensus_and_contested(lots)
    assert contested == set()
    assert consensus == {"village": "Perungudi", "extent_sqft": 1200}


def test_a_value_every_lot_shares_is_consensus_and_the_rest_is_contested():
    lots = {
        "1": {"fields": {"village": "Perungudi", "extent_sqft": 1200}},
        "2": {"fields": {"village": "Perungudi", "extent_sqft": 900}},
    }
    consensus, contested = consensus_and_contested(lots)
    assert consensus == {"village": "Perungudi"}
    assert contested == {"extent_sqft"}


def test_a_field_missing_from_one_lot_is_contested_not_consensus():
    """Present-on-some is not agreement: it names the lots that carry it."""
    lots = {"1": {"fields": {"village": "Perungudi", "landmark": "temple"}},
            "2": {"fields": {"village": "Perungudi"}}}
    consensus, contested = consensus_and_contested(lots)
    assert consensus == {"village": "Perungudi"}
    assert contested == {"landmark"}


def test_contested_keys_are_exactly_what_a_rival_listing_must_not_claim():
    lots = {"1": {"fields": {"village": "A", "taluk": "T"}},
            "2": {"fields": {"village": "B", "taluk": "T"}}}
    consensus, contested = consensus_and_contested(lots)
    # A rival listing gets `consensus` as its props, so its provenance can
    # never name a contested key.
    out = field_provenance([row("rival", consensus, SCOPE_CONSENSUS)])
    assert not set(out["rival"][SCOPE_CONSENSUS]) & contested
    assert out["rival"][SCOPE_CONSENSUS] == ["taluk"]


# ── the write path carries it ────────────────────────────────────────────────

def test_write_fields_sends_both_lists(monkeypatch):
    seen: list[dict] = []

    def fake_run_query(cypher, params=None):
        if params and "rows" in params and "a += row.props" in cypher:
            seen.extend(params["rows"])
            return [{"aid": r["aid"]} for r in params["rows"]]
        return []

    monkeypatch.setattr(AX, "run_query", fake_run_query)
    AX.write_fields([
        row("1", {"village": "Perungudi", "taluk": "T"}, SCOPE_LOT),
        row("2", {"taluk": "T"}, SCOPE_CONSENSUS),
    ])

    by_aid = {r["aid"]: r for r in seen}
    assert by_aid["1"]["prov_lot"] == ["taluk", "village"]
    assert by_aid["1"]["prov_consensus"] == []
    assert by_aid["2"]["prov_lot"] == []
    assert by_aid["2"]["prov_consensus"] == ["taluk"]


def test_write_fields_gives_both_rows_of_one_listing_the_merged_answer(
        monkeypatch):
    """Provenance is merged across a listing's rows before writing, so a row
    landing in a later batch does not overwrite the answer with its own half."""
    monkeypatch.setattr(AX, "WRITE_CHUNK", 1)
    seen: list[dict] = []

    def fake_run_query(cypher, params=None):
        if params and "rows" in params and "a += row.props" in cypher:
            seen.extend(params["rows"])
            return [{"aid": r["aid"]} for r in params["rows"]]
        return []

    monkeypatch.setattr(AX, "run_query", fake_run_query)
    AX.write_fields([
        row("1", {"village": "A"}, SCOPE_LOT, filename="scan1.jpg"),
        row("1", {"taluk": "T"}, SCOPE_CONSENSUS, filename="scan2.jpg"),
    ])

    assert len(seen) == 2, "expected one row per batch"
    for r in seen:
        assert r["prov_lot"] == ["village"]
        assert r["prov_consensus"] == ["taluk"]


def test_write_fields_on_nothing_writes_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(AX, "run_query",
                        lambda *a, **k: called.append(1) or [])
    assert AX.write_fields([]) == 0
    assert not called


# ── the clear path strips the names it removes ───────────────────────────────

def test_clear_unsafe_fields_also_filters_both_provenance_lists(monkeypatch):
    """A name left behind for a property that was just REMOVEd claims a field
    the node no longer has."""
    statements: list[str] = []

    def fake_run_query(cypher, params=None):
        statements.append(cypher)
        return []

    monkeypatch.setattr(AX, "run_query", fake_run_query)
    AX.clear_unsafe_fields([
        {"aid": "1", "filename": "n.jpg", "keys": ["extent_sqft", "village"]},
    ])

    assert statements, "expected a write"
    cypher = statements[0]
    assert "REMOVE a.`extent_sqft`, a.`village`" in cypher
    for prop in ("notice_fields_lot", "notice_fields_consensus"):
        assert f"a.{prop} =" in cypher
        assert f"[k IN coalesce(a.{prop}, [])" in cypher
    assert "WHERE NOT k IN row.keys" in cypher
