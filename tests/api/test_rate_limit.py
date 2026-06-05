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


def test_anon_chat_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The manual per-IP counter in api/chat/router.py throttles anonymous /chat."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    from api.main import app
    from api.chat.router import _ANON_CHAT_MAX_PER_HOUR, _anon_chat_hits, agent
    _anon_chat_hits.clear()

    # Stub the agent so /chat returns fast.
    class _Res:
        output = "ok"
        def new_messages(self): return []
        def all_messages(self): return []
    async def _fake_run(*_a, **_kw): return _Res()
    monkeypatch.setattr(agent, "run", _fake_run, raising=False)

    client = TestClient(app)
    codes = []
    for _ in range(_ANON_CHAT_MAX_PER_HOUR + 2):
        r = client.post("/chat", json={"message": "hi"})
        codes.append(r.status_code)
    assert 429 in codes, f"expected 429 in {codes}"
