"""
tests/api/test_chat_sse.py
--------------------------
The SSE helpers every streaming chat endpoint shares (`api/chat/sse.py`).

The keepalive exists because one LLM round-trip can sit 30-80 s with nothing
on the wire, and a client or proxy idle timeout cannot tell "slow model" from
"dead connection". These moved here from the retired pydantic-ai router's
tests, unchanged in substance.
"""
from __future__ import annotations

import asyncio
import json

import pytest


def test_frame_is_event_plus_json_data() -> None:
    from api.chat.sse import sse

    frame = sse("delta", {"text": "₹30 lakh"})
    assert frame.startswith("event: delta\ndata: ")
    assert frame.endswith("\n\n")
    # ensure_ascii=False: the rupee sign goes over the wire as itself.
    assert json.loads(frame.split("data: ", 1)[1]) == {"text": "₹30 lakh"}
    assert "₹" in frame


def test_heartbeat_fills_idle_gaps() -> None:
    """`with_heartbeat` inserts SSE comment frames while the source is
    silent (a slow LLM round-trip), and relays every real frame unchanged —
    so a proxy or client idle-timeout never sees a dead wire mid-turn."""
    from api.chat.sse import with_heartbeat

    async def slow_source():
        yield "event: status\ndata: {}\n\n"
        await asyncio.sleep(0.3)
        yield "event: final\ndata: {}\n\n"

    async def collect() -> list[str]:
        return [f async for f in with_heartbeat(slow_source(), interval=0.05)]

    frames = asyncio.run(collect())
    assert frames[0] == "event: status\ndata: {}\n\n"
    assert frames[-1] == "event: final\ndata: {}\n\n"
    # The 0.3s gap at 0.05s interval must have produced keepalive comments.
    keepalives = [f for f in frames if f == ": keepalive\n\n"]
    assert len(keepalives) >= 2
    # Nothing else was invented.
    assert set(frames) <= {"event: status\ndata: {}\n\n",
                           "event: final\ndata: {}\n\n", ": keepalive\n\n"}


def test_heartbeat_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from api.chat import sse as S

    monkeypatch.setenv("CHAT_STREAM_HEARTBEAT_SECONDS", "3.5")
    assert S.heartbeat_seconds() == 3.5
    monkeypatch.setenv("CHAT_STREAM_HEARTBEAT_SECONDS", "not-a-number")
    assert S.heartbeat_seconds() == S.STREAM_HEARTBEAT_SECONDS_DEFAULT


def test_the_streaming_endpoints_share_one_implementation() -> None:
    """All three streaming routers must frame through this module. A private
    copy in one of them is how the SSE vocabulary drifts between endpoints."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    for rel in ("api/agent3/router.py", "api/chat/v2/router.py",
                "api/chat/deep/router.py"):
        src = (root / rel).read_text(encoding="utf-8")
        assert "from api.chat.sse import sse, with_heartbeat" in src, rel
