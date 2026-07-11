"""
tests/api/test_neo4j_pool_config.py
-----------------------------------
Regression guard for the Neo4j Aura "defunct connection" outage.

Aura (and its load balancer) silently close Bolt connections that have been
idle for a few minutes. The driver was previously built with no pool config,
so `liveness_check_timeout` defaulted to None: idle pooled connections were
never probed before reuse, and the first request after an idle gap reused a
dead connection and raised
`SessionExpired("Failed to read from defunct connection")`. That surfaced as a
500 on /auth/me and as "chat agent failed — please retry" when the chat
agent's Neo4j tool calls hit it (the agent's generic `except Exception` turns
any tool error into that SSE error frame).

The fix wires `liveness_check_timeout` + `max_connection_lifetime` (and a
bounded acquisition timeout) onto BOTH the sync and async drivers so the pool
self-heals. These tests assert that wiring survives refactors, and that the
kwargs are still accepted by the pinned driver version.

conftest replaces `api.neo4j_client` in sys.modules with an in-memory stub, so
this file loads the real module under an alias via importlib (the same trick
tests/api/test_deferred_capabilities.py uses for api.agent). The driver
factory is monkeypatched, so no socket is ever opened.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _real_neo4j_client():
    """The real api/neo4j_client.py, loaded past the conftest stub."""
    if "api_neo4j_client_real" in sys.modules:
        return sys.modules["api_neo4j_client_real"]
    spec = importlib.util.spec_from_file_location(
        "api_neo4j_client_real", _REPO_ROOT / "api" / "neo4j_client.py"
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["api_neo4j_client_real"] = mod
    spec.loader.exec_module(mod)
    return mod


class _Recorder:
    """Stand-in for neo4j.GraphDatabase / AsyncGraphDatabase whose `.driver`
    records the kwargs it was called with instead of opening a connection."""

    def __init__(self) -> None:
        self.kwargs: dict | None = None

    def driver(self, uri, **kwargs):  # noqa: D401 - factory shape mirrors neo4j
        self.kwargs = kwargs
        return object()  # sentinel; never used to talk to a server


@pytest.fixture
def client(monkeypatch):
    mod = _real_neo4j_client()
    # Force a rebuild on this test's get_driver()/get_async_driver() calls.
    monkeypatch.setattr(mod, "_driver", None, raising=False)
    monkeypatch.setattr(mod, "_async_driver", None, raising=False)
    return mod


def test_sync_driver_gets_pool_config(client, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(client, "GraphDatabase", rec)
    client.get_driver()
    assert rec.kwargs is not None, "GraphDatabase.driver was never called"
    # The two knobs that defend against Aura's idle-connection drops.
    assert rec.kwargs.get("liveness_check_timeout") is not None
    assert rec.kwargs["liveness_check_timeout"] > 0
    assert rec.kwargs.get("max_connection_lifetime") is not None
    assert rec.kwargs["max_connection_lifetime"] > 0


def test_async_driver_gets_pool_config(client, monkeypatch):
    rec = _Recorder()
    monkeypatch.setattr(client, "AsyncGraphDatabase", rec)
    client.get_async_driver()
    assert rec.kwargs is not None, "AsyncGraphDatabase.driver was never called"
    assert rec.kwargs.get("liveness_check_timeout") is not None
    assert rec.kwargs["liveness_check_timeout"] > 0
    assert rec.kwargs.get("max_connection_lifetime") is not None
    assert rec.kwargs["max_connection_lifetime"] > 0


def test_sync_and_async_share_the_same_pool_config(client, monkeypatch):
    sync_rec, async_rec = _Recorder(), _Recorder()
    monkeypatch.setattr(client, "GraphDatabase", sync_rec)
    monkeypatch.setattr(client, "AsyncGraphDatabase", async_rec)
    client.get_driver()
    client.get_async_driver()
    # Both request paths must be protected identically — the outage hit both a
    # sync path (/auth/me, threadpool handlers) and async paths.
    assert sync_rec.kwargs == async_rec.kwargs


def test_pinned_driver_accepts_the_pool_kwargs(client):
    """The real neo4j driver must actually accept these kwargs — a driver bump
    that renamed/removed one would otherwise 500 every request at startup. The
    driver factory is lazy (no socket until first use), so this is offline."""
    from neo4j import GraphDatabase

    drv = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("u", "p"), **client._POOL_KWARGS
    )
    drv.close()


# ── transient-error retry ────────────────────────────────────────────────────
# The pool config (above) prevents most Aura idle-drops; the retry catches the
# residual — a connection that dies between the liveness probe and the query,
# or a routing-table refresh against a stale connection. Both surfaced in
# production as SessionExpired / ServiceUnavailable that took down a whole
# request. These tests assert the helper retries the transient classes on a
# fresh session and gives up cleanly, and that the real query helpers are wired
# to it.
from contextlib import contextmanager  # noqa: E402

import pytest  # noqa: E402  (already imported above; explicit for this block)
from neo4j.exceptions import ServiceUnavailable, SessionExpired  # noqa: E402


@pytest.fixture
def fast_retry(client, monkeypatch):
    """Zero out the backoff so retry tests don't sleep."""
    monkeypatch.setattr(client, "NEO4J_RETRY_BASE_DELAY_S", 0.0, raising=False)
    monkeypatch.setattr(client, "USE_HTTP_API", False, raising=False)
    return client


def test_retry_recovers_after_transient(fast_retry):
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        if calls["n"] < 3:
            raise SessionExpired("Failed to read from defunct connection")
        return ["ok"]

    assert fast_retry._run_with_retry(work, op="t") == ["ok"]
    assert calls["n"] == 3  # 2 transient failures, then success


def test_retry_gives_up_after_max(fast_retry, monkeypatch):
    monkeypatch.setattr(fast_retry, "NEO4J_MAX_QUERY_RETRIES", 2, raising=False)
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        raise ServiceUnavailable("Unable to retrieve routing information")

    with pytest.raises(ServiceUnavailable):
        fast_retry._run_with_retry(work, op="t")
    assert calls["n"] == 3  # attempts = retries + 1


def test_retry_does_not_swallow_non_transient(fast_retry):
    calls = {"n": 0}

    def work():
        calls["n"] += 1
        raise ValueError("real bug — must not be retried")

    with pytest.raises(ValueError):
        fast_retry._run_with_retry(work, op="t")
    assert calls["n"] == 1  # non-transient errors fail fast


def test_run_read_query_retries_transient_on_fresh_session(fast_retry, monkeypatch):
    """End-to-end through the real run_read_query: the first session's run
    raises SessionExpired, the retry opens a fresh session and succeeds. Proves
    the helper is wired to the retry, not just the retry helper in isolation."""
    state = {"n": 0}

    class _Sess:
        def run(self, *_a, **_k):
            state["n"] += 1
            if state["n"] == 1:
                raise SessionExpired("Failed to read from defunct connection")
            return [{"auction_id": "A1"}, {"auction_id": "A2"}]

    @contextmanager
    def fake_read_session():
        yield _Sess()

    monkeypatch.setattr(fast_retry, "read_session", fake_read_session)
    out = fast_retry.run_read_query("MATCH (n) RETURN n", max_rows=10)
    assert out == [{"auction_id": "A1"}, {"auction_id": "A2"}]
    assert state["n"] == 2  # one failure, one success


def test_run_read_query_respects_max_rows_cap(fast_retry, monkeypatch):
    """The retry rewrite must preserve the row cap (fetch stops at max_rows)."""
    class _Sess:
        def run(self, *_a, **_k):
            return [{"auction_id": f"A{i}"} for i in range(100)]

    @contextmanager
    def fake_read_session():
        yield _Sess()

    monkeypatch.setattr(fast_retry, "read_session", fake_read_session)
    out = fast_retry.run_read_query("MATCH (n) RETURN n", max_rows=5)
    assert len(out) == 5
