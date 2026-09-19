"""Who may read a chat thread, pinned.

agent3's memory is keyed by `thread_id` and nothing else. That was safe while
the endpoints were admin-only; opening them to everyone makes the thread id
the whole secret. These tests are the reason it isn't.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from api.agent3 import ownership
from api.auth.schemas import UserOut


def _user(uid: str = "u-1") -> UserOut:
    return UserOut(id=uid, email="qa@auctionscope.in", name="X", role="user",
                   enabled=True, email_verified=True, tier="free")


class _Req:
    """Just enough of a Starlette request for the key builders."""

    def __init__(self, ip: str = "203.0.113.7") -> None:
        self.client = type("C", (), {"host": ip})()


def test_an_account_and_an_ip_can_never_collide() -> None:
    account = ownership.owner_key(_Req(), _user("abc"))
    anon = ownership.owner_key(_Req(), None)
    assert account.startswith("user:")
    assert anon.startswith("anon:")
    assert account != anon


def test_the_anon_key_is_the_quota_key_not_a_second_hash() -> None:
    """Two salted hashes of the same IP in two modules is the kind of second
    definition that drifts the day QUOTA_IP_SALT rotates — and the drift
    would silently orphan every anonymous conversation."""
    from api.chat.gating import anon_quota_key

    req = _Req("198.51.100.4")
    assert ownership.owner_key(req, None) == f"anon:{anon_quota_key(req)}"


def test_the_raw_ip_never_appears_in_the_key() -> None:
    assert "203.0.113.7" not in ownership.owner_key(_Req("203.0.113.7"), None)


# ── claim / owns, against a faked graph ──────────────────────────────────

def _fake_graph(monkeypatch, rows, record=None):
    async def run(cypher, params=None):
        if record is not None:
            record.append((cypher, params))
        if isinstance(rows, Exception):
            raise rows
        return rows

    monkeypatch.setattr(ownership, "run_query_async", run)


def test_a_thread_whose_owner_matches_is_claimable(monkeypatch) -> None:
    _fake_graph(monkeypatch, [{"owner": "user:u-1"}])
    assert asyncio.run(ownership.claim("t-1", "user:u-1")) is True


def test_someone_elses_thread_is_refused(monkeypatch) -> None:
    _fake_graph(monkeypatch, [{"owner": "user:someone-else"}])
    assert asyncio.run(ownership.claim("t-1", "user:u-1")) is False
    assert asyncio.run(ownership.owns("t-1", "user:u-1")) is False


def test_a_thread_nobody_claimed_is_nobody_to_read(monkeypatch) -> None:
    """An older thread predates `owner_key`. Handing it to whoever asks first
    is exactly the theft this module exists to stop."""
    _fake_graph(monkeypatch, [{"owner": None}])
    assert asyncio.run(ownership.claim("t-old", "user:u-1")) is False
    assert asyncio.run(ownership.owns("t-old", "user:u-1")) is False


def test_a_missing_thread_is_not_owned(monkeypatch) -> None:
    _fake_graph(monkeypatch, [])
    assert asyncio.run(ownership.owns("nope", "user:u-1")) is False


def test_a_database_error_fails_closed(monkeypatch) -> None:
    """An unreachable graph must not answer "sure, it's yours". The caller
    reads False as "not mine", mints a fresh thread and still answers — so an
    outage costs memory, never privacy."""
    _fake_graph(monkeypatch, RuntimeError("neo4j down"))
    assert asyncio.run(ownership.claim("t-1", "user:u-1")) is False
    assert asyncio.run(ownership.owns("t-1", "user:u-1")) is False


def test_claiming_an_unowned_thread_requires_it_to_be_empty(monkeypatch) -> None:
    """The Cypher, not the Python, is what enforces this — so assert on the
    statement rather than on a mock that could agree with a broken query."""
    seen: list = []
    _fake_graph(monkeypatch, [{"owner": "user:u-1"}], record=seen)
    asyncio.run(ownership.claim("t-1", "user:u-1"))
    cypher = seen[0][0]
    assert "ON CREATE SET c.owner_key" in cypher
    assert "NOT (c)-[:HAS_CHECKPOINT]->()" in cypher


# ── the endpoints ────────────────────────────────────────────────────────

@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch):
    from api.agent3 import router as R
    from api.main import app

    async def no_quota(*a, **k):
        return None

    monkeypatch.setattr(R, "enforce_chat_quota", no_quota)
    monkeypatch.setattr(R, "_saver", lambda: object())
    return TestClient(app)


def test_reading_someone_elses_thread_is_a_404_not_a_403(client, monkeypatch) -> None:
    """404 rather than 403 on purpose: a 403 confirms the thread exists."""
    from api.agent3 import router as R

    async def owns(thread_id, key):
        return False

    monkeypatch.setattr(R.ownership, "owns", owns)
    for path in ("/chat/agent3/t-1/history", "/chat/agent3/t-1/manifests"):
        r = client.get(path)
        assert r.status_code == 404, path
    assert client.delete("/chat/agent3/t-1").status_code == 404


def test_a_turn_on_someone_elses_thread_gets_a_fresh_one(monkeypatch) -> None:
    """Never an error on a POST: losing a turn's memory beats refusing to
    answer, the same rule `_thread_id` applies to a malformed id."""
    from api.agent3 import router as R

    claimed: list[str] = []

    async def claim(thread_id, key):
        claimed.append(thread_id)
        # The id the caller asked for is not theirs; anything minted after is.
        return thread_id != "not-mine"

    async def no_quota(*a, **k):
        return None

    monkeypatch.setattr(R.ownership, "claim", claim)
    monkeypatch.setattr(R, "enforce_chat_quota", no_quota)
    req = R.ChatAgent3Request(message="hello", thread_id="not-mine")
    ctx = asyncio.run(R._prepare(_Req(), req, None))

    assert claimed[0] == "not-mine"
    assert ctx["thread_id"] != "not-mine"
    assert ctx["thread_id"].startswith("agent3-")
