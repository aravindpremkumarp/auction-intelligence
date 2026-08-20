"""
api/chat/v2/router.py
---------------------
`POST /chat/v2` and `POST /chat/v2/stream`.

/chat and /chat/stream are frozen on pydantic-ai and are not touched by any
of this. v2 owns its own contract, whose one substantive difference is that
the **scope object replaces `message_history`**: the client echoes back a
small dict instead of a transcript that grows every turn and is re-billed on
each one.

Two things are deliberately kept identical to v1:

* the **SSE event vocabulary** (`status` / `delta` / `final` / `error` plus
  keepalive comments), reusing `_sse` and `_with_heartbeat` from the v1
  router, so the browser needs no new event handling;
* the **artifact shape**, so `extractResultsFromArtifacts`, `setPanelSource`
  and the whole matches-panel path in `web/app.js` work unchanged.

**Lazy imports.** Everything under `api.chat.v2` pulls in LangChain, which
costs ~28 MB of RSS on a 512 MB Render starter instance. The imports live
inside the handlers so an idle deploy and all v1 traffic stay at today's
footprint and the cost is paid on the first v2 request.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from api.auth import get_optional_user
from api.auth.schemas import UserOut
from api.chat.gating import enforce_chat_quota, resolve_turn_model
from api.chat.v2.schemas import ChatV2Request, ChatV2Response, ExecutedCallOut, ScopeIn
from api.observability import SLOW_AGENT_MS, timed

logger = logging.getLogger(__name__)

router = APIRouter()

# Modes that stay on v1. `deep-research` runs a long multi-step investigation
# the tiered loop is not shaped for; rather than half-support it, v2 rejects
# it so the client keeps that mode on /chat.
V1_ONLY_MODES = {"deep-research"}


async def _prepare(request: Request, req: ChatV2Request,
                   user: UserOut | None) -> dict[str, Any]:
    """Shared gating for both endpoints. Raises HTTPException on refusal."""
    if req.mode in V1_ONLY_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode {req.mode!r} is not available on /chat/v2 — use /chat",
        )
    # One counter across both endpoints: a caller must not be able to spend
    # the same allowance twice by alternating between them.
    await enforce_chat_quota(request, user)
    model_name, effort = resolve_turn_model(user, req.model, req.reasoning_effort)

    from api.chat.v2.scope import sanitize_ids, sanitize_scope

    scope_in = req.scope or ScopeIn()
    # The client echoes the scope back, so re-validate every field: unlike v1's
    # inert message_history, these values are merged into search_auctions
    # kwargs by code.
    filters = sanitize_scope({**(req.active_filters or {}), **scope_in.filters})
    return {
        "filters": filters,
        "last_ids": sanitize_ids(scope_in.last_ids),
        "last_total_count": scope_in.last_total_count,
        "turn": scope_in.turn,
        "model_name": model_name,
        "reasoning_effort": effort,
        "panel": sanitize_ids((req.panel.matches if req.panel else []) or []),
    }


async def _build_response(req: ChatV2Request, ctx: dict, result) -> ChatV2Response:
    from api.chat.v2.artifacts import build_artifacts

    artifacts = await build_artifacts(result, panel_before=ctx["panel"])
    logger.info(
        "chat v2 turn ok tier=%s model=%s tool_calls=%d answer_chars=%d "
        "llm_calls=%d in_tok=%d cached_tok=%d out_tok=%d seconds=%.1f "
        "gate=%s",
        result.tier, ctx["model_name"], len(result.executed),
        len(result.answer or ""), result.model_calls, result.input_tokens,
        result.cached_tokens, result.output_tokens, result.seconds,
        "ok" if (result.gate is None or result.gate.ok) else "flagged",
    )
    return ChatV2Response(
        answer=result.answer,
        recommendation=result.recommendation,
        scope=ScopeIn(
            filters=result.filters,
            last_total_count=result.last_total_count,
            last_ids=result.last_ids,
            turn=ctx["turn"] + 1,
        ),
        plan=[ExecutedCallOut(tool=c.tool, args=c.args, ms=c.ms, tier=c.tier,
                              error=c.error)
              for c in result.executed],
        artifacts=artifacts,
        usage={
            "llm_calls": result.model_calls,
            "input_tokens": result.input_tokens,
            "cached_tokens": result.cached_tokens,
            "output_tokens": result.output_tokens,
            "seconds": result.seconds,
            "tier": result.tier,
        },
    )


@router.post("/chat/v2", response_model=ChatV2Response)
async def chat_v2(request: Request, req: ChatV2Request,
                  user: UserOut | None = Depends(get_optional_user)) -> ChatV2Response:
    ctx = await _prepare(request, req, user)
    from api.chat.v2.loop import run_turn

    with timed("chat_v2.turn", slow_ms=SLOW_AGENT_MS, model=ctx["model_name"]):
        result = await run_turn(
            req.message,
            scope=ctx["filters"],
            last_ids=ctx["last_ids"],
            last_total_count=ctx["last_total_count"],
            model_name=ctx["model_name"],
            reasoning_effort=ctx["reasoning_effort"],
        )
    return await _build_response(req, ctx, result)


@router.post("/chat/v2/stream")
async def chat_v2_stream(request: Request, req: ChatV2Request,
                         user: UserOut | None = Depends(get_optional_user)):
    ctx = await _prepare(request, req, user)
    from api.chat.router import _sse, _with_heartbeat

    return StreamingResponse(
        _with_heartbeat(_stream_turn(req, ctx, _sse)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_turn(req: ChatV2Request, ctx: dict, sse) -> AsyncIterator[str]:
    """Relay the loop's phases as SSE frames.

    The loop emits its events synchronously from inside `run_turn`, so they go
    onto a queue and this coroutine drains it — that is what lets a status
    line appear the moment each parallel query lands, instead of the whole
    execute phase going silent.
    """
    import httpx

    from api.chat.v2.loop import run_turn

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_event(event: str, payload: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, payload))

    task = asyncio.create_task(run_turn(
        req.message,
        scope=ctx["filters"],
        last_ids=ctx["last_ids"],
        last_total_count=ctx["last_total_count"],
        model_name=ctx["model_name"],
        reasoning_effort=ctx["reasoning_effort"],
        on_event=on_event,
    ))

    try:
        while True:
            drain = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({drain, task},
                                         return_when=asyncio.FIRST_COMPLETED)
            if drain in done:
                event, payload = drain.result()
                yield sse(event, payload)
                continue
            drain.cancel()
            break

        # Anything queued between the last drain and the task finishing.
        while not queue.empty():
            event, payload = queue.get_nowait()
            yield sse(event, payload)

        result = await task
    except (httpx.TimeoutException, TimeoutError):
        logger.exception("chat v2 stream timed out")
        yield sse("error", {"detail": "the model took too long — please retry"})
        return
    except Exception:
        logger.exception("chat v2 stream failed")
        yield sse("error", {"detail": "chat agent failed — please retry"})
        return

    # The answer arrives whole rather than token-by-token in Phase 1: the
    # synthesis call returns one structured object. Streaming the characters
    # inside its `answer` field (which is why that field is declared first in
    # the schema) is the next step; until then the answer ships as one delta
    # so the client's rendering path is already exercised.
    if result.answer:
        yield sse("delta", {"text": result.answer})
    if result.recommendation is not None:
        yield sse("recommendation",
                  json.loads(result.recommendation.model_dump_json()))

    response = await _build_response(req, ctx, result)
    yield sse("final", response.model_dump(mode="json"))
