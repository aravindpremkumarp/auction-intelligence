"""
tests/api/test_semantic_hybrid.py
---------------------------------
Two-lens Lucene retrieval in semantic_search (the vector lenses were retired
— see docs/design/2026-08-22-retire-embeddings.md):

- both fulltext branches — lot schedule text and property blurb — appear in
  the Cypher, with per-source score normalization;
- Lucene specials are stripped, quoted phrases survive;
- a query with nothing searchable returns a hint instead of hitting Neo4j;
- search_auctions' UI fetch is clamped to the hard cap even when the model
  asks for more.
"""
from __future__ import annotations

import api.tools.cypher_tools as ct


def test_lucene_query_strips_specials():
    assert ct._lucene_query("plot no 46 (Adyar)/~flat") == "plot no 46 Adyar flat"
    assert ct._lucene_query("AND-OR && || !") == "AND OR"
    assert ct._lucene_query("  ") is None
    assert ct._lucene_query(":+!") is None


def test_lucene_query_preserves_quoted_phrases():
    """A quoted phrase passes through intact so the caller can demand exact
    word order; bare terms alongside it are still OR-joined."""
    assert ct._lucene_query('"corner plot" near highway') == '"corner plot" near highway'
    assert ct._lucene_query('"corner plot"') == '"corner plot"'


def test_lucene_query_neutralizes_backslash_in_phrase():
    """A trailing backslash inside a phrase would escape the closing quote we
    re-add, handing Lucene an unterminated phrase — a parse error, i.e. a 500
    rather than a bad result. Strip backslashes before re-quoting."""
    assert ct._lucene_query('"foo\\"') == '"foo"'
    assert ct._lucene_query('"a\\b c"') == '"a b c"'
    assert ct._lucene_query('"\\"') is None


def test_semantic_search_queries_both_fulltext_lenses(monkeypatch):
    captured = {}

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        captured["cypher"] = cypher
        captured["params"] = params
        # Nonzero hits: a zero-hit primary triggers a no-floor diagnostic
        # rerun which would overwrite `captured` — this test is about the
        # PRIMARY query.
        return [{"auction_id": "a1", "score": 0.9, "hit_sources": ["schedule"]}]

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    out = ct.semantic_search("flat in Balaraman Nagar")

    assert out["returned"] == 1
    assert ct.LOT_FULLTEXT_INDEX in captured["cypher"]
    assert ct.PROPERTY_FULLTEXT_INDEX in captured["cypher"]
    assert "'schedule' AS source" in captured["cypher"]
    assert "'description' AS source" in captured["cypher"]
    assert captured["params"]["ft_query"] == "flat in Balaraman Nagar"
    # No vector parameter survives the retirement.
    assert "qvec" not in captured["params"]


def test_semantic_search_normalizes_each_source_separately(monkeypatch):
    """BM25 scores from the two indexes are not comparable — the lot index
    scores over much longer documents — so each is divided by its own max
    before the weighted merge."""
    captured = {}

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        captured["cypher"] = cypher
        return [{"auction_id": "a1", "score": 0.9, "hit_sources": ["schedule"]}]

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    ct.semantic_search("borewell on agricultural land")

    assert "lot_max" in captured["cypher"]
    assert "prop_max" in captured["cypher"]
    assert str(ct._SOURCE_WEIGHTS["schedule"]) in captured["cypher"]
    assert str(ct._SOURCE_WEIGHTS["description"]) in captured["cypher"]


def test_semantic_search_unsearchable_query_returns_hint_without_querying(monkeypatch):
    """Nothing survives sanitizing and there is no second engine to fall back
    on, so the tool must say so rather than run an unmatchable query."""
    calls = []

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append(cypher)
        return []

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    out = ct.semantic_search(":::")

    assert calls == []
    assert out["returned"] == 0
    assert out["results"] == []
    assert "no searchable words" in out["hint"]


def test_semantic_search_caps_llm_rows_and_offloads_overflow(monkeypatch):
    """The model-visible slice is capped at _LLM_ROWS_HARD_CAP; the full
    ranked set rides on _ui_results for the matches panel (same split as
    search_auctions). This is what keeps a large `limit` from dumping dozens
    of rows into the model's replayed history."""
    n = ct._LLM_ROWS_HARD_CAP + 15
    rows = [{"auction_id": f"a{i}", "score": 0.9, "hit_sources": ["schedule"]}
            for i in range(n)]

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        return list(rows)

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    out = ct.semantic_search("north facing plot", limit=n)

    # Model sees only the capped slice...
    assert out["returned"] == ct._LLM_ROWS_HARD_CAP
    assert len(out["results"]) == ct._LLM_ROWS_HARD_CAP
    # ...while the UI side-channel carries the full ranked set.
    assert len(out["_ui_results"]) == n
    assert out["results"] == out["_ui_results"][:ct._LLM_ROWS_HARD_CAP]


def test_semantic_search_no_overflow_key_when_within_cap(monkeypatch):
    """At or below the LLM cap there's no overflow, so no _ui_results key is
    attached (keeps the small-result payload clean)."""
    rows = [{"auction_id": f"a{i}", "score": 0.9, "hit_sources": ["description"]}
            for i in range(5)]

    monkeypatch.setattr(ct, "run_read_query",
                        lambda c, p=None, timeout=10.0, max_rows=200: list(rows))
    out = ct.semantic_search("north facing plot", limit=20)
    assert out["returned"] == 5
    assert "_ui_results" not in out


def test_search_auctions_ui_fetch_clamped_to_hard_cap(monkeypatch):
    captured = {}

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        if "count(a) AS total_count" in cypher:
            # Nonzero: a zero total now skips the row fetch entirely.
            return [{"total_count": 1}]
        captured["params"] = params
        captured["max_rows"] = max_rows
        return []

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    ct.search_auctions(limit=10_000)
    assert captured["params"]["limit"] == ct._UI_ROWS_HARD_CAP
    assert captured["max_rows"] == ct._UI_ROWS_HARD_CAP
