"""Resolution review: queues, decisions, and the funnel's review stage.

The queue logic is exercised against monkeypatched query functions the way the
extraction tests do — no DB, real payload shapes.
"""
from __future__ import annotations

import json

import pytest

import api.review.queries as q


def test_resolution_stages_are_in_the_funnel():
    keys = [k for k, _l, _p in q.PIPELINE_STAGES]
    assert "resolved" in keys
    assert "resolve_ok" in keys
    # Review comes after resolution, and both sit before the planned stages.
    assert keys.index("resolve_ok") == keys.index("resolved") + 1


def test_resolved_predicate_excuses_documents_with_no_properties():
    """A notice with no linked AuctionProperty has no places to resolve; it
    must clear on the lender alone rather than being stuck forever."""
    pred = dict((k, p) for k, _l, p in q.PIPELINE_STAGES)["resolved"]
    assert "entity_resolved_at" in pred
    assert "place_resolved_at" in pred
    assert "NOT EXISTS" in pred


def test_decision_endpoint_derives_the_key_and_refuses_bad_input(monkeypatch):
    written = {}

    def fake_run_query(cypher, params=None):
        written.update(params or {})
        return [{"n": 1}]

    monkeypatch.setattr(q, "run_query", fake_run_query)
    out = q.record_resolution_decision(
        "bank-merge", {"a": "Piramal Finance", "b": "Pirama Finance"},
        "approved", by_email="admin@example.com")
    # The key is server-derived, order-independent, and stored with the payload.
    assert out["key"] == written["key"]
    assert out["key"].startswith("bank-merge:")
    assert written["by"] == "admin@example.com"
    assert json.loads(written["payload"])["a"] == "Piramal Finance"

    with pytest.raises(ValueError):
        q.record_resolution_decision("bank-merge", {"a": "only one side"},
                                     "approved", by_email="x")
    with pytest.raises(ValueError):
        q.record_resolution_decision("bank-merge", {"a": "x", "b": "y"},
                                     "maybe", by_email="x")


def test_village_alias_target_must_exist_in_the_gazetteer(monkeypatch):
    """A typo in an alias target must fail at decide time, loudly — not
    invent a place downstream."""
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 0})
    monkeypatch.setattr(q, "run_query", lambda *a, **k: [])
    with pytest.raises(ValueError, match="not a revenue village"):
        q.record_resolution_decision(
            "village-alias",
            {"raw": "Selaiyur", "taluk": "Tambaram", "target": "Nope"},
            "approved", by_email="x")


def test_queues_filter_decided_rows_at_read_time(monkeypatch):
    """A verdict empties its queue row immediately — before the next resolver
    run rebuilds the stored state."""
    from pipeline.resolution_review import decision_key

    proposals = [
        {"score": 97.1, "a": "Piramal Finance", "b": "Pirama Finance",
         "a_count": 9, "b_count": 1},
        {"score": 92.9, "a": "ARC (India) Ltd", "b": "India SME ARC Ltd",
         "a_count": 12, "b_count": 4},
    ]
    conflicts = [
        {"auction_id": "1", "raw_district": "Kanchipuram",
         "taluk": "Pallavaram", "resolved_district": "Chengalpattu",
         "kind": "notice"},
        {"auction_id": "2", "raw_district": "Vellore", "taluk": "Walajah",
         "resolved_district": "Ranipet", "kind": "notice"},
    ]
    decisions = [
        {"key": decision_key("bank-merge",
                             {"a": "Piramal Finance", "b": "Pirama Finance"}),
         "kind": "bank-merge", "verdict": "approved",
         "payload_json": json.dumps(
             {"a": "Piramal Finance", "b": "Pirama Finance"})},
        {"key": decision_key("district-conflict",
                             {"raw": "Vellore", "taluk": "Walajah"}),
         "kind": "district-conflict", "verdict": "approved",
         "payload_json": json.dumps({"raw": "Vellore", "taluk": "Walajah"})},
    ]

    def fake_read(cypher, params=None, **kw):
        if "ResolutionDecision" in cypher:
            return decisions
        if "bank_canonical IN" in cypher:
            return [{"name": n, "files": ["f.jpg"]}
                    for n in (params or {}).get("names", [])]
        if "place_village_status" in cypher:
            return []
        if "RevenueVillage" in cypher:
            return []
        raise AssertionError(f"unexpected read: {cypher[:60]}")

    def fake_count(cypher, params=None):
        if "entity_resolution" in cypher:
            return {"pj": json.dumps(proposals)}
        if "place_resolution" in cypher:
            return {"cj": json.dumps(conflicts)}
        raise AssertionError(f"unexpected count: {cypher[:60]}")

    monkeypatch.setattr(q, "run_read_query", fake_read)
    monkeypatch.setattr(q, "_count_query", fake_count)

    out = q.resolution_review()
    # The decided pair and the decided pattern are gone; the others remain.
    assert [p["a"] for p in out["bank_pairs"]] == ["ARC (India) Ltd"]
    assert [c["raw_district"] for c in out["district_conflicts"]] == \
        ["Kanchipuram"]
    assert out["decided"] == 2
    assert out["open"] == 2


def test_undo_recomputes_the_same_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(q, "run_query",
                        lambda c, p=None: seen.update(p or {}) or [{"n": 1}])
    out = q.undo_resolution_decision(
        "district-conflict", {"raw": "Vellore", "taluk": "Walajah"})
    assert out["deleted"] is True
    assert seen["key"] == out["key"]
