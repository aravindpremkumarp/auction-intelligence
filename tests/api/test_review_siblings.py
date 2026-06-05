"""Tests for the detail-view property switcher endpoint.

`GET /review/property/{auction_id}/siblings` returns the lots that share a
sales notice, so the reviewer can step between them without leaving the
description/notice review view.

Neo4j is stubbed (tests/api/conftest.py), so the read query is monkeypatched
to return canned rows; we assert the endpoint shapes them into the
ReviewSiblingsOut model correctly.
"""
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


def test_route_registered() -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/property/{auction_id}/siblings" in paths


def test_siblings_requires_auth(client) -> None:
    r = client.get("/review/property/abc/siblings")
    assert r.status_code in (401, 403)


def test_siblings_empty_when_no_notice(client, monkeypatch) -> None:
    """No linked Document → empty payload (not a 404) so the UI hides the
    switcher cleanly."""
    _ensure_admin_user()
    import api.review.queries as q
    monkeypatch.setattr(q, "run_read_query", lambda *a, **k: [])

    r = client.get("/review/property/lonely/siblings", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] is None
    assert body["properties"] == []


def test_siblings_returns_notice_lots(client, monkeypatch) -> None:
    """Each sibling lot is shaped into the ReviewNoticeProperty model and the
    notice metadata rides along."""
    _ensure_admin_user()
    sample = {
        "filename": "canara_multi.jpg",
        "file_path": "tn/canara_multi.jpg",
        "public_url": "https://r2/canara_multi.jpg",
        "notice_type": "multi",
        "markdown": "lot one\nlot two\n",
        "properties": [
            {
                "auction_id": "A1",
                "title": "Lot 1",
                "borrowers": ["Vanitha Settu"],
                "reserve_price": 2400000.0,
                "completeness": 0.5,
                "source": "human",
                "verified": True,
                "verified_at": "2026-05-01T10:00:00+00:00",
                "verified_by": "admin@example.com",
            },
            {
                "auction_id": "A2",
                "title": "Lot 2",
                "borrowers": ["M/s New Metro Bazaar"],
                "reserve_price": 7600000.0,
                "completeness": None,
                "source": "notice",
                "verified": False,
                "verified_at": None,
                "verified_by": None,
            },
        ],
    }

    import api.review.queries as q
    monkeypatch.setattr(q, "run_read_query", lambda *a, **k: [sample])

    r = client.get("/review/property/A2/siblings", headers=_admin_header())
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "canara_multi.jpg"
    assert body["notice_type"] == "multi"
    # The notice markdown must not leak into the response payload.
    assert "markdown" not in body
    aids = [p["auction_id"] for p in body["properties"]]
    assert aids == ["A1", "A2"]
    a1 = body["properties"][0]
    assert a1["borrowers"] == ["Vanitha Settu"]
    assert a1["source"] == "human"
    assert a1["verified"] is True


def test_list_notice_siblings_sorts_by_markdown_and_drops_markdown(monkeypatch) -> None:
    """The query helper orders lots by their position in the notice markdown
    (so the switcher matches page order) and strips markdown from the row."""
    import api.review.queries as q

    row = {
        "filename": "n.jpg",
        "public_url": "https://r2/n.jpg",
        "notice_type": "multi",
        # "two" appears before "one" in the markdown → A2 should sort first.
        "markdown": "reserve price rs 76,00,000 ... reserve price rs 24,00,000",
        "properties": [
            {"auction_id": "A1", "reserve_price": 2400000.0, "verified": False,
             "verified_at": None},
            {"auction_id": "A2", "reserve_price": 7600000.0, "verified": False,
             "verified_at": None},
        ],
    }
    monkeypatch.setattr(q, "run_read_query", lambda *a, **k: [row])

    out = q.list_notice_siblings("A1")
    assert out is not None
    assert "markdown" not in out
    assert [p["auction_id"] for p in out["properties"]] == ["A2", "A1"]


def test_list_notice_siblings_none_when_no_doc(monkeypatch) -> None:
    import api.review.queries as q
    monkeypatch.setattr(q, "run_read_query", lambda *a, **k: [])
    assert q.list_notice_siblings("nope") is None
