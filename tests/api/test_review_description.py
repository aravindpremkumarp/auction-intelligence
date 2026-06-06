"""Smoke + behavior tests for the description review endpoints — specifically
the score (completeness-judge confidence) filter on the queue/notice listings
and the bulk-confirm action that mirrors the markdown/classification flows.
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


def test_bulk_confirm_route_registered() -> None:
    from api.main import app
    paths = {r.path for r in app.routes}
    assert "/review/bulk-confirm" in paths


def test_queue_accepts_judge_band(client) -> None:
    _ensure_admin_user()
    r = client.get(
        "/review/queue?judge_min=0.5&judge_max=0.9",
        headers=_admin_header(),
    )
    assert r.status_code == 200, r.text


def test_notices_accepts_judge_band(client) -> None:
    _ensure_admin_user()
    r = client.get(
        "/review/notices?judge_min=0.3&judge_max=0.8",
        headers=_admin_header(),
    )
    assert r.status_code == 200, r.text


def test_queue_rejects_out_of_range_judge(client) -> None:
    _ensure_admin_user()
    r = client.get("/review/queue?judge_min=2", headers=_admin_header())
    assert r.status_code == 422


def test_bulk_confirm_requires_auth(client) -> None:
    r = client.post("/review/bulk-confirm", json={"judge_min": 0.0})
    assert r.status_code in (401, 403)


def test_bulk_confirm_returns_count(client, monkeypatch) -> None:
    _ensure_admin_user()

    seen: dict = {}

    def fake_auto_confirm(**kwargs):
        seen.update(kwargs)
        return {"count": 5, "dry_run": kwargs.get("dry_run", False)}

    import api.review.router as router_mod
    monkeypatch.setattr(router_mod.q, "auto_confirm_descriptions", fake_auto_confirm)

    r = client.post(
        "/review/bulk-confirm",
        headers=_admin_header(),
        json={
            "judge_min": 0.5,
            "judge_max": 0.9,
            "notice_type": "single",
            "q": "kumar",
            "dry_run": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body == {"count": 5, "dry_run": False}
    # The router must forward the band + filters and the reviewer's email.
    assert seen["judge_min"] == 0.5
    assert seen["judge_max"] == 0.9
    assert seen["notice_type"] == "single"
    assert seen["q"] == "kumar"
    assert seen["by_email"] == "admin@example.com"


def test_bulk_confirm_rejects_out_of_range_judge(client) -> None:
    _ensure_admin_user()
    r = client.post(
        "/review/bulk-confirm",
        headers=_admin_header(),
        json={"judge_min": -0.1},
    )
    assert r.status_code == 422
