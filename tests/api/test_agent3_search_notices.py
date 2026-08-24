"""Tests for api/agent3/search_notices.py.

Two things worth pinning: the AND-not-OR query builder (verified live —
plain terms match 2,824/3,335 lots, AND-joined match 2), and that a lot hit
and a listing hit on the same auction_id merge into one result instead of
showing the property twice.
"""
from __future__ import annotations

from api.agent3 import search_notices as SN


def _stub(monkeypatch, lot_rows=None, listing_rows=None):
    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "lot_description_ft" in cypher:
            return list(lot_rows or [])
        return list(listing_rows or [])
    monkeypatch.setattr(SN, "run_read_query", fake)


def _lot_row(auction_id="A1", score=3.0, lot_count=1, resolved_lot_key=None):
    return {"auction_id": auction_id, "source": "lot", "score": score,
            "snippet": "borewell mentioned here", "lot_count": lot_count,
            "resolved_lot_key": resolved_lot_key}


def _listing_row(auction_id="A1", score=3.0, lot_count=1, resolved_lot_key=None):
    return {"auction_id": auction_id, "source": "listing", "score": score,
            "snippet": "a listing blurb", "lot_count": lot_count,
            "resolved_lot_key": resolved_lot_key}


# ── the query builder ───────────────────────────────────────────────────

def test_bare_terms_are_and_joined_not_or():
    """OR is Lucene's default for space-separated terms and is nearly
    useless on this corpus: verified live, 'north facing corner plot' as
    bare terms matches 2,824 of 3,335 lots; AND-joined, 2."""
    assert SN._build_lucene_query("north facing corner plot") == \
        "north AND facing AND corner AND plot"


def test_quoted_phrase_passes_through_and_joins_with_bare_terms():
    assert SN._build_lucene_query('"corner plot" borewell') == \
        '"corner plot" AND borewell'


def test_lucene_special_characters_are_stripped_from_bare_terms():
    q = SN._build_lucene_query("borewell+water?")
    assert "+" not in q and "?" not in q


def test_empty_query_yields_none():
    assert SN._build_lucene_query("   ") is None


# ── the tool ─────────────────────────────────────────────────────────────

def test_too_short_query_is_an_error(monkeypatch):
    _stub(monkeypatch)
    out = SN.search_notices("a")
    assert "error" in out


def test_zero_results_carries_a_synonym_hint(monkeypatch):
    _stub(monkeypatch)
    out = SN.search_notices("borewell")
    assert out["results"] == []
    assert "not semantic" in out["hint"]


def test_single_lot_hit_is_scoped_as_lot(monkeypatch):
    _stub(monkeypatch, lot_rows=[_lot_row(lot_count=1)])
    out = SN.search_notices("borewell")
    assert out["results"][0]["scope"] == "lot"
    assert "scope_note" not in out["results"][0]


def test_multi_lot_hit_is_scoped_as_notice(monkeypatch):
    """The real shape verified live: 791566-791568 are three listings
    sharing one 3-lot notice matched by the same snippet. That snippet is
    the notice's, not any one listing's alone."""
    _stub(monkeypatch, lot_rows=[_lot_row(auction_id="791566", lot_count=3)])
    out = SN.search_notices("borewell")
    row = out["results"][0]
    assert row["scope"] == "notice"
    assert "does not say which one" in row["scope_note"]


def test_a_resolved_multi_lot_hit_reads_as_lot_scoped(monkeypatch):
    _stub(monkeypatch, lot_rows=[_lot_row(auction_id="796269", lot_count=6,
                                         resolved_lot_key="notice.jpg#3")])
    out = SN.search_notices("borewell")
    row = out["results"][0]
    assert row["scope"] == "lot"
    assert "scope_note" not in row


def test_same_auction_id_from_both_indexes_merges_into_one_result(monkeypatch):
    """A lot match and a listing match on the same property must not show
    the property twice — the higher-scoring hit wins and records both."""
    _stub(monkeypatch,
          lot_rows=[_lot_row(auction_id="A1", score=5.0)],
          listing_rows=[_listing_row(auction_id="A1", score=2.0)])
    out = SN.search_notices("borewell")
    assert out["result_count"] == 1
    assert out["results"][0]["matched_in"] == "lot"
    assert out["results"][0]["also_matched_in"] == "listing"


def test_results_are_sorted_by_score_desc(monkeypatch):
    _stub(monkeypatch, lot_rows=[_lot_row("A1", score=1.0), _lot_row("A2", score=9.0)])
    out = SN.search_notices("borewell")
    assert [r["auction_id"] for r in out["results"]] == ["A2", "A1"]


def test_limit_is_applied_after_merge(monkeypatch):
    rows = [_lot_row(f"A{i}", score=float(i)) for i in range(20)]
    _stub(monkeypatch, lot_rows=rows)
    out = SN.search_notices("borewell", limit=3)
    assert len(out["results"]) == 3
