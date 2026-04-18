"""Tests for scoring.auction_scorer.score_auction — the 10-dimension
composite used by the `score_auction` agent tool and the evaluate /
deep-research / report modes.

The underlying Neo4j calls are stubbed so these tests run offline.
"""
from __future__ import annotations


def _patch_scorer_queries(monkeypatch, overrides=None):
    """Patch run_query INSIDE the scoring module so both the fetch and the
    per-dimension stats lookups return canned data."""
    overrides = overrides or {}

    def fake_run_query(cypher: str, params: dict | None = None):
        params = params or {}
        c = " ".join(cypher.split())
        if "MATCH (a:AuctionProperty {auction_id: $id})" in c and "OPTIONAL MATCH" in c:
            if overrides.get("no_auction"):
                return []
            return [{
                "a": {
                    "auction_id": params.get("id"),
                    "reserve_price_num": 5_000_000.0,
                    "emd_num": 500_000.0,
                    "description": "Sample description " * 20,
                    "description_completeness": 0.8,
                    "possession_type": "Physical",
                    "application_deadline_dt": "2099-01-01T00:00:00",
                },
                "city": "Chennai",
                "area": "Ambattur",
                "bank": "Canara Bank",
                "property_type": "Flat",
            }]
        if "avg(o.reserve_price_num) AS avg_price" in c:
            return [{"avg_price": 4_000_000.0}]
        if "count(o) AS density" in c:
            return [{"density": 50}]
        if "count(o) AS total" in c and ":Bank" in c:
            return [{"total": 200}]
        if "count(o) AS concurrent" in c:
            return [{"concurrent": 10}]
        return []

    import scoring.auction_scorer as sa
    monkeypatch.setattr(sa, "run_query", fake_run_query)


def test_score_auction_returns_composite(monkeypatch):
    _patch_scorer_queries(monkeypatch)
    from scoring.auction_scorer import score_auction

    result = score_auction("AUC-TEST-1")
    assert result is not None
    assert result.auction_id == "AUC-TEST-1"
    assert 0.0 <= result.composite_score <= 100.0
    assert result.grade in {"A+", "A", "B", "C", "D", "F"}
    assert len(result.dimensions) == 10

    # Each dimension carries its name, a score, a weight, and a rationale.
    names = {d.name for d in result.dimensions}
    expected = {
        "price_attractiveness", "location_quality", "legal_clarity",
        "bank_reliability", "property_condition", "timeline_urgency",
        "due_diligence_ease", "area_price_trend", "competition_risk",
        "yield_potential",
    }
    assert names == expected
    for d in result.dimensions:
        assert 0.0 <= d.score <= 100.0
        assert 0.0 < d.weight < 1.0
        assert d.rationale


def test_score_auction_missing_returns_none(monkeypatch):
    _patch_scorer_queries(monkeypatch, overrides={"no_auction": True})
    from scoring.auction_scorer import score_auction

    assert score_auction("AUC-DOES-NOT-EXIST") is None


def test_score_auction_to_dict_roundtrip(monkeypatch):
    """The agent tool calls .to_dict() on the result; verify that shape
    matches what the mode markdown prompts expect to see."""
    _patch_scorer_queries(monkeypatch)
    from scoring.auction_scorer import score_auction

    result = score_auction("AUC-TEST-2")
    d = result.to_dict()
    assert set(d.keys()) == {"auction_id", "composite_score", "grade", "dimensions"}
    first_dim = d["dimensions"][0]
    assert set(first_dim.keys()) == {"name", "score", "weight", "rationale"}
