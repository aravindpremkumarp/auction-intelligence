"""Smoke + behavior tests for the classification review endpoints.

Uses the conftest in tests/api/conftest.py which stubs Neo4j and Supabase
JWT so the FastAPI app is importable without live credentials.
"""
from __future__ import annotations

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
    assert "/review/notice/{filename}/unverify" in paths


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
    assert body == {"total": 0, "pending": 0, "verified": 0, "edited": 0}


def test_classifications_row_shape(client, monkeypatch) -> None:
    """A row from the queue must be valid against the ClassificationRow model."""
    _ensure_admin_user()
    sample_row = {
        "filename": "abc.pdf",
        "file_path": "tn_properties/abc.pdf",
        "public_url": "https://r2/abc.pdf",
        "notice_type": "single",
        "property_count": 1,
        "expected_lot_count": 1,
        "overridden": False,
        "verified": False,
        "verified_at": None,
        "verified_by": None,
        "review_notes": None,
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

    r = client.get("/review/classifications?status=all",
                   headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert len(body["rows"]) == 1
    row = body["rows"][0]
    assert row["notice_type"] == "single"
    assert row["property_count"] == 1
    assert row["expected_lot_count"] == 1


def test_classify_rejects_inconsistent_lot_count(client) -> None:
    """'single' means exactly 1 lot; 'multi' means at least 2."""
    _ensure_admin_user()
    r = client.post("/review/notice/foo.pdf/classify",
                    json={"notice_type": "single", "expected_lot_count": 3},
                    headers=_admin_header())
    assert r.status_code == 422
    r = client.post("/review/notice/foo.pdf/classify",
                    json={"notice_type": "multi", "expected_lot_count": 1},
                    headers=_admin_header())
    assert r.status_code == 422


def test_classify_rejects_out_of_range_lot_count(client) -> None:
    _ensure_admin_user()
    for bad in (0, -1, 501):
        r = client.post("/review/notice/foo.pdf/classify",
                        json={"notice_type": "multi", "expected_lot_count": bad},
                        headers=_admin_header())
        assert r.status_code == 422, f"expected_lot_count={bad} accepted"


def test_classify_single_defaults_lot_count_to_one(client, monkeypatch) -> None:
    """Confirming 'single' without a count must still stamp expected_lot_count=1."""
    _ensure_admin_user()
    captured: dict = {}

    def fake_query(cypher, params=None):
        if (cypher or "").strip().startswith("MATCH (d:Document {filename: $filename})"):
            captured.update(params or {})
            return [{"filename": "abc.pdf", "notice_type": "single",
                     "expected_lot_count": 1,
                     "verified_at": None, "verified_by": None,
                     "review_notes": None,
                     "invalidated_count": 0}]
        return []

    import api.neo4j_client as nm
    monkeypatch.setattr(nm, "run_query", fake_query)
    import api.review.queries as q
    monkeypatch.setattr(q, "run_query", fake_query)

    r = client.post("/review/notice/abc.pdf/classify",
                    json={"notice_type": "single"},
                    headers=_admin_header())
    assert r.status_code == 200
    assert captured["elc"] == 1
    assert r.json()["expected_lot_count"] == 1


def test_classify_multi_passes_lot_count_through(client, monkeypatch) -> None:
    _ensure_admin_user()
    captured: dict = {}

    def fake_query(cypher, params=None):
        if (cypher or "").strip().startswith("MATCH (d:Document {filename: $filename})"):
            captured.update(params or {})
            return [{"filename": "abc.pdf", "notice_type": "multi",
                     "expected_lot_count": 4,
                     "verified_at": None, "verified_by": None,
                     "review_notes": None,
                     "invalidated_count": 0}]
        return []

    import api.neo4j_client as nm
    monkeypatch.setattr(nm, "run_query", fake_query)
    import api.review.queries as q
    monkeypatch.setattr(q, "run_query", fake_query)

    r = client.post("/review/notice/abc.pdf/classify",
                    json={"notice_type": "multi", "expected_lot_count": 4},
                    headers=_admin_header())
    assert r.status_code == 200
    assert captured["elc"] == 4
    assert r.json()["expected_lot_count"] == 4


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
    assert body["invalidated_count"] == 2


# ── Undo a classification sign-off ──────────────────────────────────────────


def test_classify_unverify_requires_auth(client) -> None:
    r = client.post("/review/notice/abc.pdf/unverify")
    assert r.status_code in (401, 403)


def test_classify_unverify_404_when_filename_missing(client) -> None:
    _ensure_admin_user()
    r = client.post("/review/notice/no-such.pdf/unverify",
                    headers=_admin_header())
    assert r.status_code == 404


def test_classify_unverify_clears_the_signoff_only(client, monkeypatch) -> None:
    """The stamps go; notice_type, lot count and notes stay — that is what
    makes the pending card re-openable on the last decision."""
    _ensure_admin_user()
    captured: dict = {"cypher": ""}

    def fake_query(cypher, params=None):
        c = (cypher or "").strip()
        if c.startswith("MATCH (d:Document {filename: $filename})") and "REMOVE" in c:
            captured["cypher"] = c
            captured.update(params or {})
            return [{"filename": "abc.pdf", "notice_type": "multi",
                     "expected_lot_count": 6,
                     "verified_at": None, "verified_by": None,
                     "review_notes": "counted by hand",
                     "invalidated_count": 0}]
        return []

    import api.neo4j_client as nm
    monkeypatch.setattr(nm, "run_query", fake_query)
    import api.review.queries as q
    monkeypatch.setattr(q, "run_query", fake_query)

    r = client.post("/review/notice/abc.pdf/unverify", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["verified_at"] is None
    assert body["verified_by"] is None
    # Survivors: the reviewer's earlier decision is still on the card.
    assert body["notice_type"] == "multi"
    assert body["expected_lot_count"] == 6
    assert body["review_notes"] == "counted by hand"
    assert captured["filename"] == "abc.pdf"

    cypher = captured["cypher"]
    # The override flag must go too: "edited" is a verified state, so leaving
    # it set would park the row in the edited tab instead of pending.
    assert "d.notice_type_overridden" in cypher
    assert "d.notice_type_verified_at" in cypher
    assert "d.notice_type_verified_by" in cypher
    # Nothing about the document changed, so no re-extract is queued.
    assert "extraction_stale_at" not in cypher
    # The decision itself is untouched.
    assert "REMOVE d.notice_type," not in cypher
    assert "d.expected_lot_count =" not in cypher


# ── Pure-function tests for pipeline modules ────────────────────────────────


def test_classifications_accepts_uniform_status_values(client) -> None:
    """status=pending/verified/edited/all must be accepted (canonical 4-value set)."""
    _ensure_admin_user()
    for s in ("pending", "verified", "edited", "all"):
        r = client.get(f"/review/classifications?status={s}", headers=_admin_header())
        assert r.status_code == 200, f"status={s} rejected: {r.text}"


def test_classifications_accepts_notice_type(client) -> None:
    _ensure_admin_user()
    for nt in ("all", "single", "multi", "unclassified"):
        r = client.get(f"/review/classifications?notice_type={nt}", headers=_admin_header())
        assert r.status_code == 200, f"notice_type={nt} rejected: {r.text}"


def test_classifications_stats_includes_edited(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/classifications/stats", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert "edited" in body
    assert "verified" in body
    assert "pending" in body
    assert "total" in body


def test_classifications_by_property_routes_registered(client) -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/classifications/by-property" in paths


def test_classifications_by_property_returns_empty(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/classifications/by-property", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["rows"] == []


def test_classifications_rejects_legacy_status(client) -> None:
    _ensure_admin_user()
    for s in ("disagreement", "auto-confirm"):
        r = client.get(f"/review/classifications?status={s}", headers=_admin_header())
        assert r.status_code == 422, f"status={s} should be rejected"
