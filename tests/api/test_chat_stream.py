"""
tests/api/test_chat_stream.py
-----------------------------
Coverage for the two cost/UX guards added to the chat path:

1. `/chat/stream` — SSE endpoint: status events for tool calls, text deltas
   for the final answer only, and a terminal `final` frame identical in shape
   to blocking /chat's JSON (so the client can treat both uniformly).
2. Per-run usage limits — both endpoints pass `usage_limits` to the agent so
   a pathological tool loop is capped well below pydantic-ai's default of 50
   LLM requests, and the limit trip maps to a friendly, non-retryable error.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient


def _parse_sse(text: str) -> list[tuple[str, dict[str, Any]]]:
    events: list[tuple[str, dict[str, Any]]] = []
    for frame in text.strip().split("\n\n"):
        name, data = None, ""
        for line in frame.split("\n"):
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data += line[len("data:"):].strip()
        if name and data:
            events.append((name, json.loads(data)))
    return events


def _streaming_agent(answer: str = "Found 2 auctions in Chennai."):
    """Real pydantic-ai Agent on TestModel: calls every registered tool once,
    then streams `answer` — exercising the genuine event sequence
    (PartStart/FunctionToolCall/FinalResult/PartDelta/AgentRunResult)."""
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    test_agent = Agent(TestModel(custom_output_text=answer))

    @test_agent.tool_plain
    def search_auctions(city: str = "Chennai") -> dict:
        """Stub search tool."""
        return {"total_count": 2, "results": [{"auction_id": "A1"}, {"auction_id": "A2"}]}

    return test_agent


def test_chat_stream_happy_path(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    answer = "Found 2 auctions in Chennai."
    monkeypatch.setattr(chat_router, "agent", _streaming_agent(answer))

    client = TestClient(app)
    resp = client.post("/chat/stream", json={"message": "auctions in chennai"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]

    # Tool call surfaced as a friendly status, before any answer text.
    assert ("status", {"label": "Searching auctions…"}) in events
    assert kinds.index("status") < kinds.index("delta")

    # Deltas reassemble exactly the final answer (no scratch text leaked).
    streamed = "".join(p["text"] for k, p in events if k == "delta")
    assert streamed == answer

    # Terminal frame carries the same payload shape as blocking /chat.
    assert kinds[-1] == "final"
    final = events[-1][1]
    assert final["answer"] == answer
    assert isinstance(final["message_history"], list) and final["message_history"]
    assert isinstance(final["artifacts"], list)
    tool_names = {a["tool"] for a in final["artifacts"]}
    assert "search_auctions" in tool_names


def test_chat_stream_gated_mode_requires_login() -> None:
    """Gating runs before the stream starts, so it's a real 401 — not an
    in-band error event."""
    from api.main import app

    client = TestClient(app)
    resp = client.post("/chat/stream", json={"message": "x", "mode": "deep-research"})
    assert resp.status_code == 401


def test_chat_stream_error_is_in_band(monkeypatch: pytest.MonkeyPatch) -> None:
    """Failures after the stream starts ride as `error` events; usage-limit
    trips keep their specific non-retry message."""
    from pydantic_ai.exceptions import UsageLimitExceeded

    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    class _ExplodingStream:
        def __init__(self, exc: Exception):
            self._exc = exc

        def __call__(self, *a: Any, **kw: Any):
            return self

        async def __aenter__(self):
            raise self._exc

        async def __aexit__(self, *exc_info: Any) -> None:
            return None

    class _Agent:
        run_stream_events = _ExplodingStream(UsageLimitExceeded("request_limit exceeded"))

    monkeypatch.setattr(chat_router, "agent", _Agent())
    client = TestClient(app)
    resp = client.post("/chat/stream", json={"message": "x"})
    assert resp.status_code == 200  # status already committed; error is in-band
    events = _parse_sse(resp.text)
    assert events == [("error", {"detail": chat_router._USAGE_LIMIT_DETAIL})]


def test_chat_passes_usage_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Blocking /chat forwards the per-run request ceiling to agent.run."""
    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    captured: dict[str, Any] = {}

    class _Res:
        output = "ok"

        def new_messages(self):
            return []

        def all_messages(self):
            return []

    class _Agent:
        async def run(self, *a: Any, **kw: Any) -> Any:
            captured.update(kw)
            return _Res()

    monkeypatch.setattr(chat_router, "agent", _Agent())
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 200
    limits = captured.get("usage_limits")
    assert limits is not None
    assert limits.request_limit == chat_router._CHAT_REQUEST_LIMIT_DEFAULT


def test_usage_limit_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    chat_router = importlib.import_module("api.chat.router")

    monkeypatch.setenv("CHAT_REQUEST_LIMIT", "7")
    assert chat_router._usage_limits().request_limit == 7
    monkeypatch.setenv("CHAT_REQUEST_LIMIT", "not-a-number")
    assert chat_router._usage_limits().request_limit == chat_router._CHAT_REQUEST_LIMIT_DEFAULT


def test_chat_usage_limit_maps_to_422(monkeypatch: pytest.MonkeyPatch) -> None:
    from pydantic_ai.exceptions import UsageLimitExceeded

    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    class _Agent:
        async def run(self, *a: Any, **kw: Any) -> Any:
            raise UsageLimitExceeded("request_limit of 15 exceeded")

    monkeypatch.setattr(chat_router, "agent", _Agent())
    client = TestClient(app)
    resp = client.post("/chat", json={"message": "hello"})
    assert resp.status_code == 422
    assert resp.json()["detail"] == chat_router._USAGE_LIMIT_DETAIL


def test_heartbeat_fills_idle_gaps() -> None:
    """`_with_heartbeat` inserts SSE comment frames while the source is
    silent (a slow LLM round-trip), and relays every real frame unchanged —
    so a proxy or client idle-timeout never sees a dead wire mid-turn."""
    import asyncio
    import importlib
    chat_router = importlib.import_module("api.chat.router")

    async def slow_source():
        yield "event: status\ndata: {}\n\n"
        await asyncio.sleep(0.3)
        yield "event: final\ndata: {}\n\n"

    async def collect() -> list[str]:
        return [f async for f in chat_router._with_heartbeat(slow_source(), interval=0.05)]

    frames = asyncio.run(collect())
    assert frames[0] == "event: status\ndata: {}\n\n"
    assert frames[-1] == "event: final\ndata: {}\n\n"
    # The 0.3s gap at 0.05s interval must have produced keepalive comments.
    keepalives = [f for f in frames if f == ": keepalive\n\n"]
    assert len(keepalives) >= 2
    # Nothing else was invented.
    assert set(frames) <= {"event: status\ndata: {}\n\n", "event: final\ndata: {}\n\n", ": keepalive\n\n"}


def test_heartbeat_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    import importlib
    chat_router = importlib.import_module("api.chat.router")

    monkeypatch.setenv("CHAT_STREAM_HEARTBEAT_SECONDS", "3.5")
    assert chat_router._heartbeat_seconds() == 3.5
    monkeypatch.setenv("CHAT_STREAM_HEARTBEAT_SECONDS", "not-a-number")
    assert chat_router._heartbeat_seconds() == chat_router._STREAM_HEARTBEAT_SECONDS_DEFAULT


def test_stream_endpoint_keepalives_are_ignored_by_sse_parsers(monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end: with an aggressive heartbeat the endpoint's raw body may
    carry comment frames, but the parsed event stream is identical to the
    happy path — comments are invisible to any spec-compliant SSE parser."""
    import importlib
    chat_router = importlib.import_module("api.chat.router")
    from api.main import app

    monkeypatch.setenv("CHAT_STREAM_HEARTBEAT_SECONDS", "0.01")
    answer = "Found 2 auctions in Chennai."
    monkeypatch.setattr(chat_router, "agent", _streaming_agent(answer))

    client = TestClient(app)
    resp = client.post("/chat/stream", json={"message": "auctions in chennai"})
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    kinds = [k for k, _ in events]
    assert kinds[-1] == "final"
    assert events[-1][1]["answer"] == answer
