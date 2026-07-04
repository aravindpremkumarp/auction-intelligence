"""End-to-end tests for /auth/me + /admin/users* using the in-memory Neo4j
stub and the fake Supabase JWT verifier from conftest."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _reset_store() -> None:
    from api import neo4j_client
    neo4j_client._users.clear()       # type: ignore[attr-defined]
    neo4j_client._feedback.clear()    # type: ignore[attr-defined]


def test_me_requires_auth() -> None:
    _reset_store()
    r = _client().get("/auth/me")
    assert r.status_code == 401


def test_me_upserts_profile_on_first_call() -> None:
    _reset_store()
    c = _client()
    from api import neo4j_client
    assert "sub-new" not in neo4j_client._users  # type: ignore[attr-defined]

    r = c.get("/auth/me", headers=auth_header(sub="sub-new", email="x@y.com", name="Xy"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == "sub-new"
    assert body["email"] == "x@y.com"
    assert body["name"] == "Xy"
    assert body["role"] == "user"
    assert body["enabled"] is True
    assert body["email_verified"] is True
    assert "sub-new" in neo4j_client._users  # type: ignore[attr-defined]


def test_me_is_idempotent() -> None:
    _reset_store()
    c = _client()
    h = auth_header(sub="sub-dup", email="d@d.com", name="D")
    r1 = c.get("/auth/me", headers=h)
    r2 = c.get("/auth/me", headers=h)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json() == r2.json()
    from api import neo4j_client
    assert len(neo4j_client._users) == 1  # type: ignore[attr-defined]


def test_me_disabled_user_returns_401() -> None:
    _reset_store()
    c = _client()
    h = auth_header(sub="sub-dis", email="d@d.com")
    assert c.get("/auth/me", headers=h).status_code == 200

    from api import neo4j_client
    neo4j_client._users["sub-dis"]["enabled"] = False  # type: ignore[attr-defined]
    assert c.get("/auth/me", headers=h).status_code == 401


def test_patch_me_updates_name() -> None:
    _reset_store()
    c = _client()
    h = auth_header(sub="sub-p", email="p@p.com", name="Old")
    c.get("/auth/me", headers=h)
    r = c.patch("/auth/me", headers=h, json={"name": "New"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "New"


def test_admin_endpoints_require_admin_role() -> None:
    _reset_store()
    c = _client()
    h = auth_header(sub="sub-u", email="u@u.com")
    # materialise the profile then try admin
    c.get("/auth/me", headers=h)
    assert c.get("/admin/users", headers=h).status_code == 403


def test_admin_can_list_and_patch_users() -> None:
    _reset_store()
    c = _client()
    # Seed two plain users via /auth/me.
    c.get("/auth/me", headers=auth_header(sub="sub-a", email="a@a.com", name="A"))
    c.get("/auth/me", headers=auth_header(sub="sub-b", email="b@b.com", name="B"))

    # Promote sub-a to admin directly in the stub store.
    from api import neo4j_client
    neo4j_client._users["sub-a"]["role"] = "admin"  # type: ignore[attr-defined]

    h_admin = auth_header(sub="sub-a", email="a@a.com")
    r = c.get("/admin/users", headers=h_admin)
    assert r.status_code == 200
    emails = {u["email"] for u in r.json()}
    assert {"a@a.com", "b@b.com"} <= emails

    r = c.patch("/admin/users/sub-b", headers=h_admin, json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["enabled"] is False

    # sub-b can no longer authenticate.
    assert c.get("/auth/me", headers=auth_header(sub="sub-b", email="b@b.com")).status_code == 401


def test_admin_bootstrap_email_auto_promotes(monkeypatch) -> None:
    _reset_store()
    monkeypatch.setenv("ADMIN_BOOTSTRAP_EMAIL", "boss@example.com")
    c = _client()
    r = c.get("/auth/me", headers=auth_header(sub="sub-boss", email="boss@example.com", name="Boss"))
    assert r.status_code == 200
    assert r.json()["role"] == "admin"


def test_invalid_token_returns_401() -> None:
    _reset_store()
    c = _client()
    r = c.get("/auth/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401


def test_expired_token_returns_401() -> None:
    import base64 as _b64
    import json as _json
    _reset_store()
    claims = {"sub": "s", "email": "e@e.com", "_expired": True}
    body = _b64.urlsafe_b64encode(_json.dumps(claims).encode()).rstrip(b"=").decode()
    r = _client().get("/auth/me", headers={"Authorization": f"Bearer test-{body}"})
    assert r.status_code == 401


def test_chat_gated_modes_require_login() -> None:
    """Deep Research requires a verified account. `report` was archived
    (modes/_archive/, 2026-07) — as an unknown mode it must NOT 401; it
    falls back to plain ask, so stale clients degrade gracefully instead
    of being locked out."""
    _reset_store()
    c = _client()
    # Anonymous → 401 for the gated mode
    r = c.post("/chat", json={"message": "hi", "mode": "deep-research"})
    assert r.status_code == 401
    # Archived/unknown mode → not gated (proceeds as plain ask; any
    # non-401 outcome is acceptable here — the agent itself is stubbed).
    r = c.post("/chat", json={"message": "hi", "mode": "report"})
    assert r.status_code != 401
