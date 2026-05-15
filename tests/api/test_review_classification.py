"""Smoke + behavior tests for the classification review endpoints.

Uses the conftest in tests/api/conftest.py which stubs Neo4j and Supabase
JWT so the FastAPI app is importable without live credentials.
"""
from __future__ import annotations

import sys
import types
from datetime import datetime, timezone

import pytest


def _admin_header() -> dict[str, str]:
    """Same payload shape as the conftest auth_header, with admin role."""
    from tests.api.conftest import auth_header  # type: ignore
    return auth_header(sub="admin-sub", email="admin@example.com")


def _ensure_admin_user() -> None:
    """The Neo4j stub user store needs an entry whose role='admin' for the
    JWT sub to pass get_current_admin. Mirror what the auth router does on
    first login."""
    from api.neo4j_client import _users  # type: ignore[attr-defined]
    _users["admin-sub"] = {
        "supabase_id": "admin-sub",
        "email": "admin@example.com",
        "name": "Admin",
        "role": "admin",
        "enabled": True,
        "created_at": datetime.now(timezone.utc),
        "last_login_at": datetime.now(timezone.utc),
    }


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from api.main import app
    return TestClient(app)


def test_routes_registered() -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/classifications" in paths
    assert "/review/classifications/stats" in paths
    assert "/review/notice/{filename}/classify" in paths


def test_classifications_requires_auth(client) -> None:
    r = client.get("/review/classifications")
    # No bearer → 401 by FastAPI dependency
    assert r.status_code in (401, 403)


def test_classifications_returns_empty_when_no_docs(client, monkeypatch) -> None:
    _ensure_admin_user()
    # Stub returns empty list by default
    r = client.get("/review/classifications", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["page"] == 1
    assert body["size"] == 50
    assert body["total"] == 0
    assert body["rows"] == []


def test_classifications_stats_returns_zero_when_no_docs(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/classifications/stats", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body == {"total": 0, "pending": 0, "disagreement": 0, "verified": 0}


def test_classifications_row_shape(client, monkeypatch) -> None:
    """A row from the queue must be valid against the ClassificationRow model."""
    _ensure_admin_user()
    sample_row = {
        "filename": "abc.pdf",
        "file_path": "tn_properties/abc.pdf",
        "public_url": "https://r2/abc.pdf",
        "notice_type": "single",
        "property_count": 1,
        "classifier_pred": "multi",
        "classifier_confidence": 0.92,
        "classifier_reasoning": "found two distinct reserve prices",
        "classifier_model": "deepseek/deepseek-v4-flash",
        "classified_at": "2026-05-15T10:00:00+00:00",
        "overridden": False,
        "verified": False,
        "verified_at": None,
        "verified_by": None,
        "review_notes": None,
        "extraction_status": "applied",
        "disagreement": True,
        "sample_titles": ["Property in chennai"],
        "auction_id_count": 1,
    }

    def fake_read(cypher, params=None, timeout=10.0, max_rows=200):
        c = (cypher or "").strip()
        if "count(d) AS total" in c:
            return [{"total": 1}]
        return [sample_row]

    import api.neo4j_client as nm
    monkeypatch.setattr(nm, "run_read_query", fake_read)
    # api.review.queries imported run_read_query by name at import time, so
    # patch the local reference too.
    import api.review.queries as q
    monkeypatch.setattr(q, "run_read_query", fake_read)

    r = client.get("/review/classifications?status=disagreement",
                   headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["disagreement"] is True
    assert row["classifier_pred"] == "multi"
    assert row["classifier_confidence"] == pytest.approx(0.92)


def test_classify_rejects_invalid_notice_type(client) -> None:
    _ensure_admin_user()
    r = client.post("/review/notice/foo.pdf/classify",
                    json={"notice_type": "neither", "notes": None},
                    headers=_admin_header())
    # Pydantic rejects before reaching the query
    assert r.status_code == 422


def test_classify_404_when_filename_missing(client, monkeypatch) -> None:
    _ensure_admin_user()
    # Run_query stub returns [] by default for unknown patterns
    r = client.post("/review/notice/no-such.pdf/classify",
                    json={"notice_type": "single", "notes": "no notes"},
                    headers=_admin_header())
    assert r.status_code == 404


def test_classify_returns_result(client, monkeypatch) -> None:
    _ensure_admin_user()
    expected = {
        "filename": "abc.pdf",
        "notice_type": "multi",
        "verified_at": "2026-05-15T11:22:33+00:00",
        "verified_by": "admin@example.com",
        "review_notes": "split by hand",
        "extraction_status": "needs_reextract",
        "invalidated_count": 2,
    }

    def fake_query(cypher, params=None):
        c = (cypher or "").strip()
        if c.startswith("MATCH (d:Document {filename: $filename})"):
            assert params is not None
            assert params["filename"] == "abc.pdf"
            assert params["nt"] == "multi"
            return [expected]
        return []

    import api.neo4j_client as nm
    monkeypatch.setattr(nm, "run_query", fake_query)
    import api.review.queries as q
    monkeypatch.setattr(q, "run_query", fake_query)

    r = client.post("/review/notice/abc.pdf/classify",
                    json={"notice_type": "multi", "notes": "split by hand"},
                    headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["notice_type"] == "multi"
    assert body["extraction_status"] == "needs_reextract"
    assert body["invalidated_count"] == 2


# ── Pure-function tests for pipeline modules ────────────────────────────────


def test_match_schedule_exact() -> None:
    from pipeline.apply_descriptions import match_schedule
    schedules = [
        {"reserve_price_num": 1_000_000, "property_description_full": "lot A"},
        {"reserve_price_num": 2_000_000, "property_description_full": "lot B"},
    ]
    sched, reason = match_schedule(2_000_000, schedules)
    assert reason == "exact"
    assert sched["property_description_full"] == "lot B"


def test_match_schedule_tolerance() -> None:
    from pipeline.apply_descriptions import match_schedule
    schedules = [
        {"reserve_price_num": 1_005_000, "property_description_full": "near"},
    ]
    sched, reason = match_schedule(1_000_000, schedules)
    assert reason == "tolerance"
    assert sched is not None


def test_match_schedule_no_listing_price() -> None:
    from pipeline.apply_descriptions import match_schedule
    sched, reason = match_schedule(None, [{"reserve_price_num": 1, "property_description_full": "x"}])
    assert reason == "no_listing_price"
    assert sched is None


def test_match_schedule_no_match() -> None:
    from pipeline.apply_descriptions import match_schedule
    schedules = [{"reserve_price_num": 5_000_000, "property_description_full": "lot"}]
    sched, reason = match_schedule(1_000_000, schedules)
    assert reason == "none"
    assert sched is None


def test_classifier_normalize_verdict_valid() -> None:
    from pipeline.classify_notice import normalize_verdict
    v = normalize_verdict({"classification": "MULTI", "confidence": 0.83,
                            "reasoning": "two prices"})
    assert v is not None
    assert v["classification"] == "multi"
    assert v["confidence"] == pytest.approx(0.83)


def test_classifier_normalize_verdict_clamps_confidence() -> None:
    from pipeline.classify_notice import normalize_verdict
    v = normalize_verdict({"classification": "single", "confidence": 1.7,
                            "reasoning": "one"})
    assert v is not None
    assert v["confidence"] == 1.0


def test_classifier_normalize_verdict_rejects_bad_label() -> None:
    from pipeline.classify_notice import normalize_verdict
    assert normalize_verdict({"classification": "maybe", "confidence": 0.9}) is None


def test_classifier_normalize_verdict_rejects_missing_label() -> None:
    from pipeline.classify_notice import normalize_verdict
    assert normalize_verdict({"confidence": 0.9, "reasoning": "x"}) is None


def test_extractor_normalize_schedules_drops_blank() -> None:
    from pipeline.extract_descriptions import normalize_schedules
    out = normalize_schedules({"schedules": [
        {"reserve_price_num": 1, "property_description_full": "ok"},
        {"reserve_price_num": 2, "property_description_full": ""},
        {"reserve_price_num": 3},  # no description
    ]})
    assert out is not None
    assert len(out) == 1
    assert out[0]["reserve_price_num"] == 1
