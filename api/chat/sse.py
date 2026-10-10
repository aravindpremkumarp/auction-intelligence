"""
api/chat/sse.py
---------------
Server-sent-event framing shared by every streaming chat endpoint
(`/chat/agent3/stream`, `/chat/v2/stream`, `/chat/deep/stream`).

These lived in the retired pydantic-ai router (`api/chat/router.py`), so every
streaming endpoint imported that router — and with it pydantic-ai — just to
write a frame. They have no dependency beyond the standard library.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from typing import Any


def sse(event: str, data: dict[str, Any]) -> str:
    """One SSE frame: `event: <name>` plus a JSON `data:` line."""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# Seconds of stream silence before a keepalive comment is emitted. A single
# LLM round-trip can sit 30-80s producing no event (DeepSeek first-party
# latency — see the 2026-08-19 investigation: tools answer in ms, the model
# calls eat the whole turn), during which the stream sent NOTHING. The client
# can't tell "slow model" from "dead connection", so its fetch timeout killed
# working turns ("BodyStreamBuffer was aborted"), and proxy idle timeouts
# could do the same. The keepalive is an SSE comment frame (": keepalive"),
# which every SSE parser must ignore — the web client just sees bytes arrive
# and resets its idle timer.
STREAM_HEARTBEAT_SECONDS_DEFAULT = 15.0


def heartbeat_seconds() -> float:
    try:
        return float(os.environ.get(
            "CHAT_STREAM_HEARTBEAT_SECONDS", str(STREAM_HEARTBEAT_SECONDS_DEFAULT)
        ))
    except ValueError:
        return STREAM_HEARTBEAT_SECONDS_DEFAULT


async def with_heartbeat(
    source: AsyncIterator[str], interval: float | None = None
) -> AsyncIterator[str]:
    """Relay `source` frames, inserting an SSE comment whenever `interval`
    seconds pass with no frame — so the wire is never silent longer than the
    interval while the agent is thinking."""
    if interval is None:
        interval = heartbeat_seconds()
    it = source.__aiter__()
    task: asyncio.Task | None = None
    try:
        while True:
            task = asyncio.ensure_future(it.__anext__())
            while True:
                done, _ = await asyncio.wait({task}, timeout=interval)
                if task in done:
                    break
                yield ": keepalive\n\n"
            try:
                frame = task.result()
            except StopAsyncIteration:
                return
            task = None
            yield frame
    finally:
        # Client disconnect (GeneratorExit) or cancellation: stop the pending
        # pull and let the source generator run its own cleanup.
        if task is not None and not task.done():
            task.cancel()
        aclose = getattr(it, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:  # noqa: BLE001 - teardown must never raise over the real exit
                pass
