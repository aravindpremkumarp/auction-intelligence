"""Tests for api/agent3/identifiers.py — shared survey/patta/door lookup.

Backs both find_properties(identifier=...) and the standalone
find_by_identifier tool, so its Lucene-escaping and dual-path (Lot/Parcel)
query shape are pinned once here rather than in either caller.
"""
from __future__ import annotations

from api.agent3 import identifiers as I


def _stub(monkeypatch, rows=None):
    calls: list[tuple[str, dict]] = []

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {})))
        return list(rows or [])

    monkeypatch.setattr(I, "run_read_query", fake)
    return calls


def test_value_is_phrase_quoted_against_lucene_syntax():
    """Survey numbers are full of Lucene operators — `123/4B`, `S.No 45-2` —
    that a bare query would parse as syntax, not text."""
    assert I.escape_lucene("123/4B") == '"123/4B"'
    assert I.escape_lucene('a "quoted" thing') == '"a \\"quoted\\" thing"'


def test_resolve_identifier_walks_both_lot_and_parcel_paths(monkeypatch):
    calls = _stub(monkeypatch, rows=[{"auction_id": "A1"}])
    I.resolve_identifier("331/1")
    cypher = calls[0][0]
    assert "MENTIONS_IDENTIFIER" in cypher
    assert "HAS_IDENTIFIER" in cypher
    assert calls[0][1]["q"] == '"331/1"'


def test_resolve_identifier_returns_plain_ids(monkeypatch):
    _stub(monkeypatch, rows=[{"auction_id": "A1"}, {"auction_id": "A2"}])
    assert I.resolve_identifier("331/1") == ["A1", "A2"]


def test_resolve_identifier_bad_kind_raises(monkeypatch):
    _stub(monkeypatch)
    import pytest
    from api.agent3.common import ToolInputError
    with pytest.raises(ToolInputError):
        I.resolve_identifier("331/1", kind="pincode")


def test_resolve_identifier_detail_score_does_not_collide_with_union(monkeypatch):
    """Regression: Neo4j 5.x rejects a UNION branch inside CALL{} returning a
    column literally named `score` when `score` is also an imported outer
    variable ('Variable `score` already declared in outer scope'). The fix
    aliases it to `match_score` inside the branches and back to `score` on
    the way out — this pins that the alias survives, not just that it parses
    (parsing is checked live against Neo4j, not here)."""
    cypher = I._DETAIL_CYPHER
    assert "score AS match_score" in cypher
    assert "match_score AS score" in cypher


def test_resolve_identifier_detail_shape(monkeypatch):
    _stub(monkeypatch, rows=[{"auction_id": "A1", "matched_kind": "survey_new",
                              "matched_value": "331/1", "score": 4.0,
                              "lot_key": "k#1", "lot_property_type": "Land",
                              "lot_count": 2, "city": "Coimbatore",
                              "bank": "SIB", "title": "t"}])
    out = I.resolve_identifier_detail("331/1")
    assert out[0]["auction_id"] == "A1"
    assert out[0]["lot_count"] == 2
