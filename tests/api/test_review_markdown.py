"""Smoke + behavior tests for the markdown review endpoints."""
from __future__ import annotations
from datetime import datetime, timezone

import pytest


def _admin_header() -> dict[str, str]:
    from tests.api.conftest import auth_header  # type: ignore
    return auth_header(sub="admin-sub", email="admin@example.com")


def _ensure_admin_user() -> None:
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


def test_markdown_accepts_uniform_status_values(client) -> None:
    _ensure_admin_user()
    for s in ("pending", "verified", "edited", "all"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 200, f"status={s} rejected: {r.text}"


def test_markdown_accepts_score_max(client) -> None:
    _ensure_admin_user()
    r = client.get(
        "/review/markdown?score_min=50&score_max=80",
        headers=_admin_header(),
    )
    assert r.status_code == 200


def test_markdown_accepts_notice_type(client) -> None:
    _ensure_admin_user()
    for nt in ("all", "single", "multi", "unclassified"):
        r = client.get(f"/review/markdown?notice_type={nt}", headers=_admin_header())
        assert r.status_code == 200, f"notice_type={nt} rejected: {r.text}"


def test_markdown_stats_includes_edited(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/markdown/stats", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert "edited" in body
    assert "verified" in body
    assert "pending" in body
    assert "total" in body


def test_markdown_by_property_routes_registered(client) -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/markdown/by-property" in paths


def test_markdown_by_property_returns_empty(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/markdown/by-property", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["rows"] == []


def test_markdown_rejects_legacy_status(client) -> None:
    _ensure_admin_user()
    for s in ("good", "bad", "unscored"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 422, f"status={s} should be rejected"


def test_markdown_row_model_exposes_highlights() -> None:
    # Regression: the highlight spans must survive the response_model. FastAPI
    # silently drops fields not declared on MarkdownRow, which is what hid the
    # highlights from the UI.
    from api.review.router import MarkdownRow
    assert "highlights" in MarkdownRow.model_fields
    row = MarkdownRow(filename="n.jpg", highlights=[{"start": 3, "end": 9}])
    dumped = row.model_dump()
    assert dumped["highlights"] == [{"start": 3, "end": 9}]
    # default is an empty list, never missing
    assert MarkdownRow(filename="n.jpg").model_dump()["highlights"] == []
