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
    """Make /chat return fast without hitting a model."""
    from api.chat.router import agent

    class _Res:
        output = "ok"
        def new_messages(self): return []
        def all_messages(self): return []
    async def _fake_run(*_a, **_kw): return _Res()
    monkeypatch.setattr(agent, "run", _fake_run, raising=False)


def test_anon_chat_daily_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anonymous /chat hits the durable per-IP daily cap."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    monkeypatch.setenv("CHAT_ANON_DAILY_LIMIT", "3")
    monkeypatch.setenv("CHAT_ANON_MONTHLY_LIMIT", "100")  # high, so day is the binding cap
    from api.main import app
    import api.neo4j_client as neo
    neo._anon_quota.clear()
    _stub_agent(monkeypatch)

    client = TestClient(app)
    codes = [client.post("/chat", json={"message": "hi"}).status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:], f"unexpected {codes}"


def test_anon_chat_monthly_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anonymous /chat hits the durable per-IP monthly cap even when the day cap
    is generous — the monthly window is the nudge to log in."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    monkeypatch.setenv("CHAT_ANON_DAILY_LIMIT", "100")  # high, so month is the binding cap
    monkeypatch.setenv("CHAT_ANON_MONTHLY_LIMIT", "2")
    from api.main import app
    import api.neo4j_client as neo
    neo._anon_quota.clear()
    _stub_agent(monkeypatch)

    client = TestClient(app)
    codes = [client.post("/chat", json={"message": "hi"}).status_code for _ in range(4)]
    assert codes[:2] == [200, 200] and 429 in codes[2:], f"unexpected {codes}"


def test_user_chat_daily_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Free-tier users hit the durable, day-bucketed per-user chat cap."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    monkeypatch.setenv("CHAT_FREE_DAILY_LIMIT", "3")
    from api.main import app
    from api.chat.router import agent
    from tests.api.conftest import auth_header

    class _Res:
        output = "ok"
        def new_messages(self): return []
        def all_messages(self): return []
    async def _fake_run(*_a, **_kw): return _Res()
    monkeypatch.setattr(agent, "run", _fake_run, raising=False)

    client = TestClient(app)
    # Unique sub so the durable counter starts clean regardless of test order.
    h = auth_header(sub="sub-quota-free", email="limit@x.com")
    codes = [
        client.post("/chat", json={"message": "hi"}, headers=h).status_code
        for _ in range(5)
    ]
    assert codes[:3] == [200, 200, 200] and 429 in codes[3:], f"unexpected {codes}"
