"""
tests/api/test_semantic_hybrid.py
---------------------------------
Hybrid (vector + Lucene fulltext) retrieval in semantic_search:

- the keyword branch + score normalization appear in the Cypher when the
  query yields searchable Lucene terms;
- Lucene specials are stripped before the text reaches the index;
- a missing fulltext index degrades to the vector-only query instead of
  failing the search;
- search_auctions' UI fetch is clamped to the hard cap even when the model
  asks for more.
"""
from __future__ import annotations

import pytest

import api.tools.cypher_tools as ct
from neo4j.exceptions import Neo4jError


@pytest.fixture(autouse=True)
def _fake_embedding(monkeypatch):
    monkeypatch.setattr(ct, "embed_query_gemini", lambda q: [0.0] * 4)


def test_lucene_query_strips_specials():
    assert ct._lucene_query('plot "no: 46" + (Adyar)/~flat') == "plot no 46 Adyar flat"
    assert ct._lucene_query("AND-OR && || !") == "AND OR"
    assert ct._lucene_query("  ") is None
    assert ct._lucene_query(":+!") is None


def test_semantic_search_includes_keyword_branch(monkeypatch):
    captured = {}

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        captured["cypher"] = cypher
        captured["params"] = params
        # Nonzero hits: a zero-hit primary now triggers a no-floor diagnostic
        # rerun which would overwrite `captured` — this test is about the
        # PRIMARY query's keyword branch.
        return [{"auction_id": "a1", "score": 0.9, "hit_sources": ["keyword"]}]

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    out = ct.semantic_search("flat in Balaraman Nagar")
    assert out["returned"] == 1
    assert ct.PROPERTY_FULLTEXT_INDEX in captured["cypher"]
    assert "'keyword' AS source" in captured["cypher"]
    # Normalization stage rides along with the keyword branch.
    assert "ft_max" in captured["cypher"]
    assert captured["params"]["ft_query"] == "flat in Balaraman Nagar"
    assert captured["params"]["keyword_weight"] == ct._KEYWORD_WEIGHT


def test_semantic_search_falls_back_when_fulltext_index_missing(monkeypatch):
    calls = []

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        calls.append(cypher)
        if ct.PROPERTY_FULLTEXT_INDEX in cypher:
            raise Neo4jError("no such fulltext index")
        return [{"auction_id": "a1", "score": 0.9, "hit_sources": ["desc"]}]

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    out = ct.semantic_search("villa in Adyar")
    assert len(calls) == 2
    assert ct.PROPERTY_FULLTEXT_INDEX not in calls[1]
    assert out["returned"] == 1
    assert out["results"][0]["auction_id"] == "a1"


def test_semantic_search_unsearchable_query_skips_keyword(monkeypatch):
    captured = {}

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        captured["cypher"] = cypher
        captured["params"] = params
        # Nonzero hits so the zero-hit diagnostic rerun doesn't overwrite
        # `captured` — the assertions target the PRIMARY query.
        return [{"auction_id": "a1", "score": 0.9, "hit_sources": ["desc"]}]

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    ct.semantic_search(":::")
    assert ct.PROPERTY_FULLTEXT_INDEX not in captured["cypher"]
    assert "ft_query" not in captured["params"]


def test_semantic_search_caps_llm_rows_and_offloads_overflow(monkeypatch):
    """The model-visible slice is capped at _SEMANTIC_ROWS_TO_MODEL; the full
    ranked set rides on _ui_results for the matches panel (same split as
    search_auctions). This is what keeps a large `limit` from dumping dozens
    of rows into the model's replayed history."""
    n = ct._SEMANTIC_ROWS_TO_MODEL + 15
    rows = [{"auction_id": f"a{i}", "score": 0.9, "hit_sources": ["desc"]}
            for i in range(n)]

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        return list(rows)

    monkeypatch.setattr(ct, "run_read_query", fake_read)
    out = ct.semantic_search("north facing plot", limit=n)

    # Model sees only the capped slice...
    assert out["returned"] == ct._SEMANTIC_ROWS_TO_MODEL
    assert len(out["results"]) == ct._SEMANTIC_ROWS_TO_MODEL
    # ...while the UI side-channel carries the full ranked set.
    assert len(out["_ui_results"]) == n
    assert out["results"] == out["_ui_results"][:ct._SEMANTIC_ROWS_TO_MODEL]


def test_semantic_search_reports_total_ranked_not_just_slice(monkeypatch):
    """`returned` counts the slice the model can read; `total_ranked` counts
    everything that matched. Without the second field, capping the slice
    would silently shrink the model's idea of how many hits exist — the
    "14 properties written from a 10-row sample" failure the search_auctions
    docstring warns about."""
    n = ct._SEMANTIC_ROWS_TO_MODEL + 7
    rows = [{"auction_id": f"a{i}", "score": 0.9, "hit_sources": ["desc"]}
            for i in range(n)]

    monkeypatch.setattr(ct, "run_read_query",
                        lambda c, p=None, timeout=10.0, max_rows=200: list(rows))
    out = ct.semantic_search("north facing plot", limit=n)

    assert out["returned"] == ct._SEMANTIC_ROWS_TO_MODEL
    assert out["total_ranked"] == n


def test_semantic_slice_is_not_wider_than_structured_search():
    """Semantic rows carry a 300-char description excerpt, so they must never
    put MORE rows in context than the structured search does — that inversion
    is what made semantic_search the largest tool payload in production."""
    assert ct._SEMANTIC_ROWS_TO_MODEL <= ct.SEARCH_ROWS_TO_MODEL
    assert ct._SEMANTIC_ROWS_TO_MODEL <= ct._LLM_ROWS_HARD_CAP


def test_semantic_search_no_overflow_key_when_within_cap(monkeypatch):
    """At or below the LLM cap there's no overflow, so no _ui_results key is
    attached (keeps the small-result payload clean)."""
    rows = [{"auction_id": f"a{i}", "score": 0.9, "hit_sources": ["desc"]}
            for i in range(5)]

    monkeypatch.setattr(ct, "run_read_query",
                        lambda c, p=None, timeout=10.0, max_rows=200: list(rows))
    out = ct.semantic_search("north facing plot", limit=20)
    assert out["returned"] == 5
    assert out["total_ranked"] == 5
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
