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
    branch_props = [
        {"score": 94.1, "a": "Portonovo", "b": "Portnovo",
         "a_count": 3, "b_count": 1, "bank": "Indian Bank"},
        {"score": 98.0, "a": "Asset Recovery Management Branch",
         "b": "Assets Recovery Management Branch",
         "a_count": 5, "b_count": 2, "bank": "Indian Overseas Bank"},
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
        {"key": decision_key("branch-merge",
                             {"bank": "Indian Bank", "a": "Portonovo",
                              "b": "Portnovo"}),
         "kind": "branch-merge", "verdict": "rejected",
         "payload_json": json.dumps(
             {"bank": "Indian Bank", "a": "Portonovo", "b": "Portnovo"})},
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
        if "HAS_LOT" in cypher:
            return []
        raise AssertionError(f"unexpected read: {cypher[:60]}")

    def fake_count(cypher, params=None):
        if "entity_resolution" in cypher:
            return {"pj": json.dumps(proposals)}
        if "branch_resolution" in cypher:
            return {"pj": json.dumps(branch_props)}
        if "place_resolution" in cypher:
            return {"cj": json.dumps(conflicts)}
        raise AssertionError(f"unexpected count: {cypher[:60]}")

    monkeypatch.setattr(q, "run_read_query", fake_read)
    monkeypatch.setattr(q, "_count_query", fake_count)

    out = q.resolution_review()
    # Every decided row is gone — approved, rejected alike; the rest remain.
    assert [p["a"] for p in out["bank_pairs"]] == ["ARC (India) Ltd"]
    assert [p["bank"] for p in out["branch_pairs"]] == ["Indian Overseas Bank"]
    assert [c["raw_district"] for c in out["district_conflicts"]] == \
        ["Kanchipuram"]
    assert out["decided"] == 3
    assert out["open"] == 3


def test_lot_match_candidates_carries_the_resolver_s_own_evidence(monkeypatch):
    """Each row shows what the rule saw and why it couldn't decide — same
    reserve price / borrower comparison `resolve_lot` itself makes — plus
    the notice image, this listing's portal link, and every DB property
    sharing the notice (so "9 lots, 1 property in our DB" is visible)."""
    listing_rows = [
        {"auction_id": "796269", "title": "Sriperumbudur plot",
         "listing_url": "https://www.eauctionsindia.com/properties/796269",
         "file_path": "notice.jpg", "public_url": "https://cdn/notice.jpg",
         "reserve": 999, "lot_count": 6, "borrower": None},
    ]
    lot_rows = [
        {"file_path": "notice.jpg", "lot_key": "notice.jpg#1",
         "reserve": 4160000, "sqft": 1200.456, "address": "Plot 1",
         "borrowers": ["Mr. X"]},
        {"file_path": "notice.jpg", "lot_key": "notice.jpg#2",
         "reserve": 8355000, "sqft": None, "address": "Plot 2",
         "borrowers": ["Mr. Y"]},
    ]
    sib_rows = [
        {"file_path": "notice.jpg", "auction_id": "796269",
         "title": "Sriperumbudur plot",
         "url": "https://www.eauctionsindia.com/properties/796269",
         "reserve": 999},
    ]

    def fake_read(cypher, params=None, **kw):
        if "count(l) AS lot_count" in cypher:
            return listing_rows
        if "sib:AuctionProperty" in cypher:
            return sib_rows
        if "HAS_LOT" in cypher:
            return lot_rows
        raise AssertionError(f"unexpected read: {cypher[:60]}")

    monkeypatch.setattr(q, "run_read_query", fake_read)
    out = q._lot_match_candidates([])
    assert len(out) == 1
    row = out[0]
    assert row["auction_id"] == "796269"
    assert row["lot_count"] == 6
    assert row["public_url"] == "https://cdn/notice.jpg"
    assert row["listing_url"] == "https://www.eauctionsindia.com/properties/796269"
    # The notice has 6 lots but only 1 AuctionProperty in our DB — the two
    # counts must stay visibly distinct, not collapsed into one number.
    assert len(row["db_properties"]) == 1
    assert row["db_properties"][0]["auction_id"] == "796269"
    assert [c["lot_key"] for c in row["candidates"]] == \
        ["notice.jpg#1", "notice.jpg#2"]
    # sqft is rounded for display; a missing one stays None, not 0.
    assert row["candidates"][0]["sqft"] == 1200.5
    assert row["candidates"][1]["sqft"] is None
    assert row["reason"]


def test_lot_match_candidates_short_circuits_on_no_open_listings(monkeypatch):
    calls = []

    def fake_read(cypher, params=None, **kw):
        calls.append(cypher)
        return []

    monkeypatch.setattr(q, "run_read_query", fake_read)
    assert q._lot_match_candidates([]) == []
    # The lot-detail and sibling-count queries never fire when there's
    # nothing to join.
    assert len(calls) == 1


def test_lot_match_candidates_excludes_a_reviewed_none_of_these(monkeypatch):
    """A human's "none of these" verdict must not keep coming back — the
    listing is filtered before the query even runs, not after."""
    from pipeline.resolution_review import NONE_LOT_KEY, lot_match_key

    seen_params = {}

    def fake_read(cypher, params=None, **kw):
        if "count(l) AS lot_count" in cypher:
            seen_params.update(params or {})
            return []
        raise AssertionError(f"unexpected read: {cypher[:60]}")

    monkeypatch.setattr(q, "run_read_query", fake_read)
    decisions = [{
        "key": lot_match_key("796269", NONE_LOT_KEY), "kind": "lot-match",
        "verdict": "rejected",
        "payload": {"auction_id": "796269", "lot_key": NONE_LOT_KEY},
    }]
    assert q._lot_match_candidates(decisions) == []
    assert seen_params["skipped"] == ["796269"]


def test_lot_match_candidates_excludes_an_approved_pick_before_it_applies(
        monkeypatch):
    """A candidate a human already picked must vanish from the queue right
    away — not linger until the next "Apply my decisions" run writes
    resolved_lot_key."""
    from pipeline.resolution_review import lot_match_key

    seen_params = {}

    def fake_read(cypher, params=None, **kw):
        if "count(l) AS lot_count" in cypher:
            seen_params.update(params or {})
            return []
        raise AssertionError(f"unexpected read: {cypher[:60]}")

    monkeypatch.setattr(q, "run_read_query", fake_read)
    decisions = [{
        "key": lot_match_key("796269", "notice.jpg#3"), "kind": "lot-match",
        "verdict": "approved",
        "payload": {"auction_id": "796269", "lot_key": "notice.jpg#3"},
    }]
    assert q._lot_match_candidates(decisions) == []
    assert seen_params["skipped"] == ["796269"]


def test_lot_match_decision_rejects_a_lot_key_off_the_listing_s_notice(
        monkeypatch):
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 0})
    with pytest.raises(ValueError, match="not a lot on"):
        q.record_resolution_decision(
            "lot-match", {"auction_id": "796269", "lot_key": "wrong.jpg#9"},
            "approved", by_email="x")


def test_lot_match_decision_accepts_a_lot_key_on_the_listing_s_notice(
        monkeypatch):
    written = {}
    monkeypatch.setattr(q, "_count_query", lambda *a, **k: {"n": 1})
    monkeypatch.setattr(
        q, "run_query",
        lambda cypher, params=None: written.update(params or {}) or [{"n": 1}])
    out = q.record_resolution_decision(
        "lot-match", {"auction_id": "796269", "lot_key": "notice.jpg#3"},
        "approved", by_email="reviewer@example.com")
    assert out["key"].startswith("lot-match:")
    assert written["by"] == "reviewer@example.com"


def test_lot_match_rejection_skips_the_notice_check(monkeypatch):
    """A rejection ("none of these") doesn't need the lot to exist — there's
    nothing to point `resolved_lot_key` at."""
    monkeypatch.setattr(
        q, "_count_query",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not run")))
    monkeypatch.setattr(q, "run_query", lambda c, p=None: [{"n": 1}])
    out = q.record_resolution_decision(
        "lot-match", {"auction_id": "796269", "lot_key": "notice.jpg#3"},
        "rejected", by_email="x")
    assert out["key"].startswith("lot-match:")


def test_undo_recomputes_the_same_key(monkeypatch):
    seen = {}
    monkeypatch.setattr(q, "run_query",
                        lambda c, p=None: seen.update(p or {}) or [{"n": 1}])
    out = q.undo_resolution_decision(
        "district-conflict", {"raw": "Vellore", "taluk": "Walajah"})
    assert out["deleted"] is True
    assert seen["key"] == out["key"]
