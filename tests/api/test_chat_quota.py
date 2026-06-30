"""
tests/api/test_chat_quota.py
----------------------------
Durable, tier-aware chat quota + entitlement (PR 1 of the pricing work).

Covers the free/paid cap split, the date-bucket window reset on the atomic
counter, and the derived tier surfacing on /auth/me. The concurrency guarantee
(N parallel requests, remaining=1 -> 1 success / N-1 rejects) lives in the paid
flow's live-E2E lane against a real Neo4j — the in-memory stub here can't prove
DB-level atomicity, so it isn't asserted with the fake store.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from tests.api.conftest import auth_header


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


@pytest.fixture
def chat_client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """A /chat client with the quota live and a fast stubbed agent."""
    monkeypatch.setenv("RATELIMIT_DISABLED", "")
    from api.main import app
    from api.chat.router import agent

    class _Res:
        output = "ok"
        def new_messages(self): return []
        def all_messages(self): return []

    async def _fake_run(*_a, **_kw): return _Res()
    monkeypatch.setattr(agent, "run", _fake_run, raising=False)
    return TestClient(app)


def test_paid_tier_bypasses_free_cap(
    chat_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A user with a future plan expiry is "paid" and clears the tiny free cap."""
    monkeypatch.setenv("CHAT_FREE_DAILY_LIMIT", "2")
    monkeypatch.setenv("CHAT_PAID_DAILY_LIMIT", "100")
    import api.neo4j_client as neo  # the in-memory stub

    sub = "sub-paid"
    # Materialise the user, then grant a 30-day plan.
    h = auth_header(sub=sub, email="paid@x.com")
    chat_client.get("/auth/me", headers=h)
    neo._users[sub]["plan_expires_at"] = _iso(
        datetime.now(timezone.utc) + timedelta(days=30)
    )

    codes = [
        chat_client.post("/chat", json={"message": "hi"}, headers=h).status_code
        for _ in range(5)
    ]
    assert codes == [200] * 5, f"paid user should not hit the free cap: {codes}"


def test_expired_plan_falls_back_to_free(
    chat_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A past plan expiry lazily degrades to the free tier (no cron)."""
    monkeypatch.setenv("CHAT_FREE_DAILY_LIMIT", "2")
    import api.neo4j_client as neo

    sub = "sub-expired"
    h = auth_header(sub=sub, email="expired@x.com")
    chat_client.get("/auth/me", headers=h)
    neo._users[sub]["plan_expires_at"] = _iso(
        datetime.now(timezone.utc) - timedelta(days=1)
    )

    me = chat_client.get("/auth/me", headers=h).json()
    assert me["tier"] == "free"
    codes = [
        chat_client.post("/chat", json={"message": "hi"}, headers=h).status_code
        for _ in range(4)
    ]
    assert codes[:2] == [200, 200] and 429 in codes[2:], f"unexpected {codes}"


def test_bucket_rollover_resets_count() -> None:
    """The atomic counter restarts at 1 when the date bucket changes."""
    import asyncio

    from api.auth import repository as repo
    import api.neo4j_client as neo

    sub = "sub-bucket"
    neo._users[sub] = {
        "supabase_id": sub, "email": "b@x.com", "name": "B",
        "role": "user", "enabled": True,
    }

    async def _run() -> list[int]:
        a = await repo.bump_chat_quota(sub, "20260614", "202606")
        b = await repo.bump_chat_quota(sub, "20260614", "202606")
        c = await repo.bump_chat_quota(sub, "20260615", "202606")  # new day -> reset
        # Day counter resets on the new day; the month counter keeps climbing.
        return [a["day"], b["day"], c["day"], c["month"]]

    assert asyncio.run(_run()) == [1, 2, 1, 3]


def test_me_reports_free_tier_by_default(chat_client: TestClient) -> None:
    """Users with no plan surface as free with no expiry."""
    h = auth_header(sub="sub-default-free", email="free@x.com")
    me = chat_client.get("/auth/me", headers=h).json()
    assert me["tier"] == "free"
    assert me["plan_expires_at"] is None
