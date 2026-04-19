"""Admin router tests — guards and role/enabled toggles."""
from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.test_auth import _client, _register_and_verify, _reset_store


def _login(client: TestClient, email: str = "a@b.com", password: str = "Passw0rd") -> dict:
    r = client.post("/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def _promote_to_admin(email: str) -> None:
    """Directly flip role on the stub — mimics running scripts.create_admin."""
    from api import neo4j_client
    uid = neo4j_client._users_by_email[email.lower()]  # type: ignore[attr-defined]
    neo4j_client._users[uid]["role"] = "admin"         # type: ignore[attr-defined]


def test_admin_endpoints_require_admin() -> None:
    _reset_store()
    client = _client()
    _register_and_verify(client)
    tokens = _login(client)
    # regular user → 403
    r = client.get("/admin/users", headers={"Authorization": f"Bearer {tokens['access']}"})
    assert r.status_code == 403


def test_admin_can_list_and_patch_users() -> None:
    _reset_store()
    client = _client()
    _register_and_verify(client)
    _promote_to_admin("a@b.com")
    tokens = _login(client)
    headers = {"Authorization": f"Bearer {tokens['access']}"}

    r = client.get("/admin/users", headers=headers)
    assert r.status_code == 200
    users = r.json()
    assert len(users) == 1
    target_id = users[0]["id"]

    # Disable user
    r = client.patch(f"/admin/users/{target_id}", headers=headers,
                     json={"enabled": False})
    assert r.status_code == 200
    assert r.json()["enabled"] is False

    # Now /auth/me should 401 because the account is disabled
    r = client.get("/auth/me", headers=headers)
    assert r.status_code == 401


def test_admin_patch_unknown_user() -> None:
    _reset_store()
    client = _client()
    _register_and_verify(client)
    _promote_to_admin("a@b.com")
    tokens = _login(client)
    headers = {"Authorization": f"Bearer {tokens['access']}"}
    r = client.patch("/admin/users/does-not-exist", headers=headers,
                     json={"role": "user"})
    assert r.status_code == 404


def test_admin_requires_bearer() -> None:
    _reset_store()
    client = _client()
    r = client.get("/admin/users")
    assert r.status_code == 401
