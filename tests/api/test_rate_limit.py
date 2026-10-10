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


def _stub_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make /chat/agent3 return fast without hitting a model."""
    from tests.api.conftest import stub_agent3_turn

    stub_agent3_turn(monkeypatch)


def test_anon_chat_daily_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anonymous chat hits the durable per-IP daily cap."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    monkeypatch.setenv("CHAT_ANON_DAILY_LIMIT", "3")
    monkeypatch.setenv("CHAT_ANON_MONTHLY_LIMIT", "100")  # high, so day is the binding cap
    from api.main import app
    import api.neo4j_client as neo
    neo._anon_quota.clear()
    _stub_agent(monkeypatch)

    client = TestClient(app)
    codes = [client.post("/chat/agent3", json={"message": "hi"}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:], f"unexpected {codes}"


def test_anon_chat_monthly_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anonymous chat hits the durable per-IP monthly cap even when the day cap
    is generous — the monthly window is the nudge to log in."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    monkeypatch.setenv("CHAT_ANON_DAILY_LIMIT", "100")  # high, so month is the binding cap
    monkeypatch.setenv("CHAT_ANON_MONTHLY_LIMIT", "2")
    from api.main import app
    import api.neo4j_client as neo
    neo._anon_quota.clear()
    _stub_agent(monkeypatch)

    client = TestClient(app)
    codes = [client.post("/chat/agent3", json={"message": "hi"}).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:], f"unexpected {codes}"


def test_user_chat_daily_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free-tier users hit the durable, day-bucketed per-user chat cap."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    monkeypatch.setenv("CHAT_FREE_DAILY_LIMIT", "3")
    from api.main import app
    from tests.api.conftest import auth_header

    _stub_agent(monkeypatch)

    client = TestClient(app)
    # Unique sub so the durable counter starts clean regardless of test order.
    h = auth_header(sub="sub-quota-free", email="limit@x.com")
    codes = [
        client.post("/chat/agent3", json={"message": "hi"}, headers=h).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:], f"unexpected {codes}"


def test_subscribe_per_ip_limit(monkeypatch: pytest.MonkeyPatch,
                                rate_limited_client: TestClient) -> None:
    """POST /alerts/subscribe caps signups per IP.

    Anonymous and write-shaped, it is the site's most obvious list-stuffing
    target; the honeypot only catches bots that fill every field, so the cap
    is what bounds a determined one. Guards the wiring as much as the number:
    slowapi silently no-ops on a route whose handler has no `request: Request`
    parameter, so a decorator alone proves nothing.
    """
    from api.alerts import repository as repo

    async def _noop(**_kw):
        return None
    monkeypatch.setattr(repo, "upsert_subscriber", _noop)

    codes = [
        rate_limited_client.post(
            "/alerts/subscribe", json={"email": f"x{i}@example.com"}
        ).status_code
        for i in range(7)
    ]
    assert codes[:5] == [200] * 5 and 429 in codes[5:], f"unexpected {codes}"
