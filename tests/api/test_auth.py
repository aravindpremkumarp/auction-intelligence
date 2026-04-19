"""End-to-end tests for /auth/* endpoints using the in-memory Neo4j stub."""
from __future__ import annotations

from fastapi.testclient import TestClient

from api.auth.security import (
    create_access_token,
    create_refresh_token,
    hash_password,
    verify_password,
)


def _client() -> TestClient:
    from api.main import app
    return TestClient(app)


def _reset_store() -> None:
    from api import neo4j_client
    neo4j_client._users.clear()          # type: ignore[attr-defined]
    neo4j_client._users_by_email.clear() # type: ignore[attr-defined]
    neo4j_client._refresh.clear()        # type: ignore[attr-defined]
    neo4j_client._verify.clear()         # type: ignore[attr-defined]
    neo4j_client._feedback.clear()       # type: ignore[attr-defined]


def _register(client: TestClient, email: str = "a@b.com",
              password: str = "Passw0rd", name: str = "A") -> dict:
    r = client.post("/auth/register", json={"email": email, "password": password, "name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _latest_verify_token(purpose: str = "verify_email") -> str:
    from api import neo4j_client
    tokens = [t for t, v in neo4j_client._verify.items()  # type: ignore[attr-defined]
              if v["purpose"] == purpose]
    assert tokens, f"no {purpose} token in stub store"
    return tokens[-1]


def _register_and_verify(client: TestClient) -> dict:
    body = _register(client)
    token = _latest_verify_token()
    r = client.post("/auth/verify-email", json={"token": token})
    assert r.status_code == 200, r.text
    return body


def test_password_hash_round_trip() -> None:
    h = hash_password("Passw0rd")
    assert h != "Passw0rd"
    assert verify_password("Passw0rd", h)
    assert not verify_password("wrong", h)


def test_jwt_access_round_trip() -> None:
    from api.auth.security import decode_token
    t = create_access_token("u-1", "user")
    payload = decode_token(t, "access")
    assert payload["sub"] == "u-1"
    assert payload["role"] == "user"


def test_jwt_refresh_has_jti() -> None:
    from api.auth.security import decode_token
    t, jti, _ = create_refresh_token("u-1")
    payload = decode_token(t, "refresh")
    assert payload["jti"] == jti


def test_register_and_duplicate() -> None:
    _reset_store()
    client = _client()
    body = _register(client)
    assert body["email"] == "a@b.com"
    assert body["email_verified"] is False
    # duplicate → 409
    r = client.post("/auth/register", json={"email": "a@b.com", "password": "Passw0rd", "name": "A"})
    assert r.status_code == 409


def test_login_requires_verified_email() -> None:
    _reset_store()
    client = _client()
    _register(client)
    r = client.post("/auth/login", json={"email": "a@b.com", "password": "Passw0rd"})
    assert r.status_code == 403  # email not verified


def test_full_flow_register_verify_login_me_refresh_logout() -> None:
    _reset_store()
    client = _client()
    _register_and_verify(client)

    r = client.post("/auth/login", json={"email": "a@b.com", "password": "Passw0rd"})
    assert r.status_code == 200, r.text
    pair = r.json()
    access = pair["access"]
    refresh = pair["refresh"]
    assert pair["user"]["email_verified"] is True

    # /auth/me
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {access}"})
    assert r.status_code == 200
    assert r.json()["email"] == "a@b.com"

    # /auth/me without token → 401
    r = client.get("/auth/me")
    assert r.status_code == 401

    # /auth/refresh rotates (old jti is revoked)
    r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 200
    new = r.json()
    assert new["access"] and new["refresh"]
    assert new["refresh"] != refresh

    # old refresh no longer works
    r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {refresh}"})
    assert r.status_code == 401

    # /auth/logout revokes the (new) refresh
    r = client.post("/auth/logout", headers={"Authorization": f"Bearer {new['refresh']}"})
    assert r.status_code == 204
    r = client.post("/auth/refresh", headers={"Authorization": f"Bearer {new['refresh']}"})
    assert r.status_code == 401


def test_login_wrong_password() -> None:
    _reset_store()
    client = _client()
    _register_and_verify(client)
    r = client.post("/auth/login", json={"email": "a@b.com", "password": "WrongPass1"})
    assert r.status_code == 401


def test_forgot_and_reset_password() -> None:
    _reset_store()
    client = _client()
    _register_and_verify(client)

    r = client.post("/auth/forgot-password", json={"email": "a@b.com"})
    assert r.status_code == 204

    # grab the reset token directly from the stub store
    from api import neo4j_client
    reset_tokens = [t for t, v in neo4j_client._verify.items()     # type: ignore[attr-defined]
                    if v["purpose"] == "reset_password"]
    assert len(reset_tokens) == 1
    reset_token = reset_tokens[0]

    r = client.post("/auth/reset-password",
                    json={"token": reset_token, "new_password": "NewPass123"})
    assert r.status_code == 204

    # old password no longer works; new one does
    r = client.post("/auth/login", json={"email": "a@b.com", "password": "Passw0rd"})
    assert r.status_code == 401
    r = client.post("/auth/login", json={"email": "a@b.com", "password": "NewPass123"})
    assert r.status_code == 200


def test_forgot_for_unknown_email_is_204() -> None:
    _reset_store()
    client = _client()
    r = client.post("/auth/forgot-password", json={"email": "nobody@nowhere.com"})
    assert r.status_code == 204  # don't leak existence


def test_verify_with_bad_token() -> None:
    _reset_store()
    client = _client()
    r = client.post("/auth/verify-email", json={"token": "not-a-real-token"})
    assert r.status_code == 400


def test_password_policy_rejects_weak() -> None:
    _reset_store()
    client = _client()
    r = client.post("/auth/register", json={"email": "w@w.com", "password": "short", "name": "W"})
    assert r.status_code == 422


def test_chat_gated_modes_require_login() -> None:
    """Deep Research and Report require a verified account."""
    _reset_store()
    client = _client()
    # Anonymous → 401 for gated mode
    r = client.post("/chat", json={"message": "hi", "mode": "deep-research"})
    assert r.status_code == 401
    r = client.post("/chat", json={"message": "hi", "mode": "report"})
    assert r.status_code == 401
