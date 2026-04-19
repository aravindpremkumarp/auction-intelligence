"""Rate-limit smoke tests. slowapi is disabled by RATELIMIT_DISABLED=1 in
conftest; for these tests we flip the existing limiter on in-place and reset
its counters between runs."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def rate_limited_client() -> TestClient:
    from api.auth.rate_limit import limiter
    from api.main import app

    prev = limiter.enabled
    limiter.enabled = True
    # slowapi keeps an internal limits-library storage; reset it so prior tests
    # don't leak counter state.
    try:
        limiter.reset()
    except Exception:
        pass
    try:
        yield TestClient(app)
    finally:
        limiter.enabled = prev
        try:
            limiter.reset()
        except Exception:
            pass


def _reset_store_for_rate() -> None:
    from api import neo4j_client
    for bucket in ("_users", "_users_by_email", "_refresh", "_verify", "_feedback"):
        getattr(neo4j_client, bucket, {}).clear()


def test_login_rate_limit_fires(rate_limited_client: TestClient) -> None:
    _reset_store_for_rate()
    client = rate_limited_client
    # Pre-register a user we can fail-login against.
    r = client.post("/auth/register",
                    json={"email": "r@r.com", "password": "Passw0rd", "name": "R"})
    assert r.status_code == 201

    # Six wrong-password POSTs in quick succession — sixth should 429.
    codes = []
    for _ in range(6):
        r = client.post("/auth/login", json={"email": "r@r.com", "password": "bad-Pass1"})
        codes.append(r.status_code)
    assert 429 in codes, f"expected 429 in {codes}"


def test_anon_chat_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual per-IP counter in api/main.py throttles anonymous /chat."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    from api import main as main_mod
    main_mod._anon_chat_hits.clear()

    # Stub the agent so /chat returns fast.
    class _Res:
        output = "ok"
        def new_messages(self): return []
        def all_messages(self): return []
    async def _fake_run(*_a, **_kw): return _Res()
    monkeypatch.setattr(main_mod.agent, "run", _fake_run, raising=False)

    client = TestClient(main_mod.app)
    codes = []
    for _ in range(main_mod._ANON_CHAT_MAX_PER_HOUR + 2):
        r = client.post("/chat", json={"message": "hi"})
        codes.append(r.status_code)
    assert 429 in codes, f"expected 429 in {codes}"
