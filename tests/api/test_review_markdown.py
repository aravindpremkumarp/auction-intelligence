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
    for s in ("pending", "verified", "edited", "all", "good", "bad", "unscored"):
        r = client.get(f"/review/markdown?status={s}", headers=_admin_header())
        assert r.status_code == 200, f"status={s} rejected: {r.text}"
