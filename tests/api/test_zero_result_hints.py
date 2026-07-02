"""
tests/api/test_zero_result_hints.py
-----------------------------------
Zero-result searches must explain themselves. Logfire traces showed the
future-only default silently hiding matches, which sent the agent into
retry loops (paraphrased `semantic_search` ×4, per-area splits of an
already-tried list filter, 26 tool calls on one turn). When a search comes
back empty *because of the defaulted future-only floor*, the tools now run
one cheap follow-up without the floor and attach `past_matches` + `hint` so
the model makes a single informed decision instead of flailing.

No diagnostic runs when the caller set the window explicitly (starts_after
or include_past) — that zero is intentional.
"""
from __future__ import annotations

from datetime import datetime, timezone


# ── search_auctions ────────────────────────────────────────────────────────

def _patch_search_queries(monkeypatch, *, past_count: int):
    """First call (count) returns 0; a follow-up count returns past_count.
    Captures every (cypher, params) so tests can assert the follow-up shape."""
    calls: list[tuple[str, dict]] = []

    def fake(cypher: str, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {})))
        if "count(a) AS total_count" in cypher and "starts_after" not in (params or {}):
            return [{"total_count": past_count}]
        return [{"total_count": 0}]

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake)
    return calls


def test_zero_with_defaulted_floor_reports_past_matches(monkeypatch) -> None:
    calls = _patch_search_queries(monkeypatch, past_count=42)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(asset_category="Industrials", limit=10)

    assert out["total_count"] == 0
    assert out["past_matches"] == 42
    assert "include_past" in out["hint"]
    # The follow-up count must drop the date floor and its param.
    followup_cypher, followup_params = calls[-1]
    assert "starts_after" not in followup_params
    assert "$starts_after" not in followup_cypher


def test_zero_everywhere_gets_do_not_retry_hint(monkeypatch) -> None:
    _patch_search_queries(monkeypatch, past_count=0)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(city="Nowhere", limit=10)

    assert out["total_count"] == 0
    assert "past_matches" not in out
    assert "hint" in out
    assert "do not retry the same shape" in out["hint"]


def test_no_diagnostic_when_caller_set_the_window(monkeypatch) -> None:
    calls = _patch_search_queries(monkeypatch, past_count=99)
    from api.tools.cypher_tools import search_auctions

    out = search_auctions(
        city="Chennai", limit=10,
        starts_after=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert out["total_count"] == 0
    assert "hint" not in out and "past_matches" not in out
    assert len(calls) == 1  # count only — no follow-up, no row fetch

    # include_past=True: the count itself has no date floor, so use a fake
    # that returns 0 for it too — still no diagnostic, no extra query.
    calls = _patch_search_queries(monkeypatch, past_count=0)
    out = search_auctions(city="Chennai", limit=10, include_past=True)
    assert "hint" not in out
    assert len(calls) == 1


def test_zero_total_skips_row_fetch(monkeypatch) -> None:
    """total_count == 0 means the row query can't return anything — don't
    spend a Neo4j round-trip fetching an empty page."""
    calls = _patch_search_queries(monkeypatch, past_count=0)
    from api.tools.cypher_tools import search_auctions

    search_auctions(city="Nowhere", limit=10)
    row_queries = [c for c, _ in calls if "ORDER BY" in c]
    assert not row_queries


# ── semantic_search ────────────────────────────────────────────────────────

def _patch_semantic(monkeypatch, *, floor_hits: int, no_floor_hits: int):
    """Return `floor_hits` rows for queries WITH the date floor and
    `no_floor_hits` rows for the no-floor rerun."""
    calls: list[tuple[str, dict]] = []

    def fake(cypher: str, params=None, timeout=10.0, max_rows=200):
        calls.append((cypher, dict(params or {})))
        n = floor_hits if "starts_after" in (params or {}) else no_floor_hits
        return [{"auction_id": str(i), "score": 0.9} for i in range(n)]

    import api.tools.cypher_tools as ct
    monkeypatch.setattr(ct, "run_read_query", fake)
    monkeypatch.setattr(ct, "embed_query_gemini", lambda q: [0.0] * 3072)
    return calls


def test_semantic_zero_reports_past_matches_without_reembedding(monkeypatch) -> None:
    calls = _patch_semantic(monkeypatch, floor_hits=0, no_floor_hits=7)
    from api.tools.cypher_tools import semantic_search

    out = semantic_search("near industrial area", limit=20)

    assert out["returned"] == 0
    assert out["past_matches"] == 7
    assert "include_past" in out["hint"]
    # Exactly two Neo4j queries (floor + no-floor); the embedding is reused.
    assert len(calls) == 2
    assert "starts_after" not in calls[-1][1]


def test_semantic_zero_everywhere_says_do_not_rephrase(monkeypatch) -> None:
    _patch_semantic(monkeypatch, floor_hits=0, no_floor_hits=0)
    from api.tools.cypher_tools import semantic_search

    out = semantic_search("query with no matches", limit=20)

    assert out["returned"] == 0
    assert "past_matches" not in out
    assert "do not retry with rephrased wording" in out["hint"].lower()


def test_semantic_hits_get_no_hint(monkeypatch) -> None:
    calls = _patch_semantic(monkeypatch, floor_hits=3, no_floor_hits=9)
    from api.tools.cypher_tools import semantic_search

    out = semantic_search("3-BR flat in Adyar", limit=20)

    assert out["returned"] == 3
    assert "hint" not in out
    assert len(calls) == 1  # no diagnostic rerun


def test_semantic_no_diagnostic_when_include_past(monkeypatch) -> None:
    calls = _patch_semantic(monkeypatch, floor_hits=0, no_floor_hits=0)
    from api.tools.cypher_tools import semantic_search

    out = semantic_search("anything", limit=20, include_past=True)

    assert out["returned"] == 0
    assert "hint" not in out
    assert len(calls) == 1


# ── diagnostic failures must never fail (or overclaim on) a valid zero ─────

def test_search_diagnostic_failure_returns_plain_zero(monkeypatch) -> None:
    """The unfloored count is heavier than the indexed primary; if it times
    out, the user still gets the valid 0-match answer — just without a hint."""
    import api.tools.cypher_tools as ct

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "starts_after" not in (params or {}):
            raise TimeoutError("simulated slow unfloored count")
        return [{"total_count": 0}]

    monkeypatch.setattr(ct, "run_read_query", fake)
    out = ct.search_auctions(city="Chennai", limit=10)

    assert out["total_count"] == 0
    assert "hint" not in out and "past_matches" not in out


def test_semantic_diagnostic_failure_gets_no_confident_hint(monkeypatch) -> None:
    """A failed no-floor rerun must NOT produce the definitive 'no matches in
    any time window' claim — that zero was never verified."""
    import api.tools.cypher_tools as ct

    def fake(cypher, params=None, timeout=10.0, max_rows=200):
        if "starts_after" not in (params or {}):
            raise TimeoutError("simulated slow rerun")
        return []

    monkeypatch.setattr(ct, "run_read_query", fake)
    monkeypatch.setattr(ct, "embed_query_gemini", lambda q: [0.0] * 3072)
    out = ct.semantic_search("near industrial area", limit=20)

    assert out["returned"] == 0
    assert "hint" not in out and "past_matches" not in out
