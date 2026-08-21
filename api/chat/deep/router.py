"""
api/chat/deep/router.py
-----------------------
`POST /chat/deep` and `POST /chat/deep/stream`.

The third chat surface, alongside `/chat` (pydantic-ai, v1) and `/chat/v2`
(the tiered loop). It exists to be measured against `/chat/v2`, so it reuses
that router's quota, model gating, artifact shape and SSE vocabulary verbatim
— the only intentional differences are the loop behind it and where the
conversation state lives.

**`thread_id` replaces `scope`.** v1 round-trips a transcript through the
client; v2 round-trips a summary. This endpoint round-trips neither: the
client sends the conversation id it already mints for
`api/conversations/`, and the transcript lives server-side in Neo4j under
that key. State the server depends on is state the server owns — which is the
class of bug that produced `apiChatScope` never being cleared between
threads.

**Admin only**, same as /chat/v2 and for the same reason: every request
spends real money on the prepaid OpenRouter key, and gating the /lab page
alone would be cosmetic when anyone who knew the URL could POST here.

**Lazy imports.** `deepagents` costs ~107 MB of RSS on top of LangChain's
~28 MB against a 512 MB instance, so every import that reaches it lives
inside a handler.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from api.auth import get_current_admin
from api.auth.schemas import UserOut
from api.chat.gating import enforce_chat_quota, resolve_turn_model
from api.chat.v2.schemas import (
    ChatV2Response,
    ExecutedCallOut,
    GateVerdictOut,
    PanelIn,
    ScopeIn,
)
from api.observability import SLOW_AGENT_MS, timed

logger = logging.getLogger(__name__)

router = APIRouter()

#: Modes this endpoint refuses. Empty, unlike /chat/v2's.
#:
#: `deep-research` — a full due-diligence pass on one property — is the one
#: mode the tiered loop rejects, because a plan-execute-synthesize shape has
#: nowhere to put an open-ended investigation. This loop does: it delegates to
#: the `property-dossier` subagent in `api/chat/deep/agent.py`, which is the
#: first thing the harness's `task` tool has actually been given to do.
V1_ONLY_MODES: set[str] = set()


class ChatDeepRequest(BaseModel):
    message: str
    #: The conversation id. The browser already mints one per thread for
    #: `api/conversations`; reusing it means the transcript and the saved
    #: conversation share a key and a lifecycle. Omitted on the very first
    #: turn of an unsaved chat, in which case the server mints one and
    #: returns it for the client to keep using.
    thread_id: str | None = None
    panel: PanelIn | None = None
    mode: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class ChatDeepResponse(ChatV2Response):
    """v2's response plus the thread key.

    Subclassed rather than redefined so a field added to the v2 contract
    cannot silently be missing here — the /lab inspector reads both.
    """

    thread_id: str = ""
    steps: int = 0


async def _prepare(request: Request, req: ChatDeepRequest,
                   user: UserOut | None) -> dict[str, Any]:
    if req.mode in V1_ONLY_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"mode {req.mode!r} is not available on /chat/deep — use /chat",
        )
    # One counter across all three endpoints: a caller must not be able to
    # spend the same allowance three times by rotating between them.
    await enforce_chat_quota(request, user)
    model_name, effort = resolve_turn_model(user, req.model, req.reasoning_effort)

    from api.chat.v2.scope import sanitize_ids

    return {
        "thread_id": _thread_id(req.thread_id),
        "model_name": model_name,
        "reasoning_effort": effort,
        "panel": sanitize_ids((req.panel.matches if req.panel else []) or []),
    }


def _thread_id(raw: str | None) -> str:
    """A safe thread key.

    Client-supplied and used as a Neo4j property in a MERGE, so it is bounded
    and stripped of anything that is not id-shaped. A malformed id mints a
    fresh thread rather than erroring: losing one turn's memory is a far
    better failure than refusing to answer.
    """
    text = (raw or "").strip()
    if text and len(text) <= 64 and all(c.isalnum() or c in "-_" for c in text):
        return text
    return f"deep-{uuid.uuid4()}"


def _saver():
    """The checkpointer, built per request.

    Holds no connection of its own — `api/neo4j_client` owns the pooled
    driver — so construction is free and there is no shared mutable state to
    reason about across requests.
    """
    from api.chat.deep.checkpointer import Neo4jSaver

    return Neo4jSaver()


async def _build_response(req: ChatDeepRequest, ctx: dict,
                          result) -> ChatDeepResponse:
    from api.chat.v2.artifacts import build_artifacts

    artifacts = await build_artifacts(result, panel_before=ctx["panel"])
    logger.info(
        "chat deep turn ok model=%s tool_calls=%d answer_chars=%d "
        "llm_calls=%d steps=%d in_tok=%d cached_tok=%d out_tok=%d "
        "seconds=%.1f gate=%s",
        ctx["model_name"], len(result.executed), len(result.answer or ""),
        result.model_calls, result.steps, result.input_tokens,
        result.cached_tokens, result.output_tokens, result.seconds,
        "ok" if (result.gate is None or result.gate.ok) else "flagged",
    )
    return ChatDeepResponse(
        answer=result.answer,
        recommendation=result.recommendation,
        thread_id=ctx["thread_id"],
        steps=result.steps,
        # Carried for the matches panel only — the loop's real memory is the
        # checkpointed transcript, not this.
        scope=ScopeIn(
            filters=result.filters,
            last_total_count=result.last_total_count,
            last_ids=result.last_ids,
            last_question=result.last_question,
            last_entities=result.last_entities,
            turn=0,
        ),
        plan=[ExecutedCallOut(tool=c.tool, args=c.args, ms=c.ms, tier=0,
                              error=c.error)
              for c in result.executed],
        artifacts=artifacts,
        gate=(
            GateVerdictOut(
                ok=result.gate.ok,
                unsupported_ids=result.gate.unsupported_ids,
                unsupported_amounts=result.gate.unsupported_amounts,
                reason=result.gate.reason,
            )
            if result.gate is not None
            else None
        ),
        usage={
            "llm_calls": result.model_calls,
            "input_tokens": result.input_tokens,
            "cached_tokens": result.cached_tokens,
            "output_tokens": result.output_tokens,
            "seconds": result.seconds,
            "tier": 0,
            "steps": result.steps,
        },
    )


@router.post("/chat/deep", response_model=ChatDeepResponse)
async def chat_deep(request: Request, req: ChatDeepRequest,
                    user: UserOut = Depends(get_current_admin)
                    ) -> ChatDeepResponse:
    ctx = await _prepare(request, req, user)
    from api.chat.deep.loop import run_turn

    with timed("chat_deep.turn", slow_ms=SLOW_AGENT_MS, model=ctx["model_name"]):
        result = await run_turn(
            req.message,
            thread_id=ctx["thread_id"],
            model_name=ctx["model_name"],
            reasoning_effort=ctx["reasoning_effort"],
            checkpointer=_saver(),
        )
    return await _build_response(req, ctx, result)


@router.post("/chat/deep/stream")
async def chat_deep_stream(request: Request, req: ChatDeepRequest,
                           user: UserOut = Depends(get_current_admin)):
    ctx = await _prepare(request, req, user)
    from api.chat.router import _sse, _with_heartbeat

    return StreamingResponse(
        _with_heartbeat(_stream_turn(req, ctx, _sse)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_turn(req: ChatDeepRequest, ctx: dict, sse) -> AsyncIterator[str]:
    """Relay the loop's phases as SSE frames — same drain pattern as v2."""
    import httpx

    from api.chat.deep.loop import run_turn

    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def on_event(event: str, payload: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, (event, payload))

    task = asyncio.create_task(run_turn(
        req.message,
        thread_id=ctx["thread_id"],
        model_name=ctx["model_name"],
        reasoning_effort=ctx["reasoning_effort"],
        checkpointer=_saver(),
        on_event=on_event,
    ))

    try:
        drain: asyncio.Task | None = None
        while True:
            drain = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({drain, task},
                                         return_when=asyncio.FIRST_COMPLETED)
            if drain in done:
                event, payload = drain.result()
                drain = None
                yield sse(event, payload)
                continue
            drain.cancel()
            drain = None
            break

        while not queue.empty():
            event, payload = queue.get_nowait()
            yield sse(event, payload)

        result = await task
    except (httpx.TimeoutException, TimeoutError):
        logger.exception("chat deep stream timed out")
        yield sse("error", {"detail": "the model took too long — please retry"})
        return
    except Exception:
        logger.exception("chat deep stream failed")
        yield sse("error", {"detail": "chat agent failed — please retry"})
        return
    finally:
        # A disconnecting client throws GeneratorExit here. Without this the
        # detached turn keeps calling the model and querying Neo4j for someone
        # who has gone.
        if drain is not None and not drain.done():
            drain.cancel()
        if not task.done():
            task.cancel()

    if result.answer:
        yield sse("delta", {"text": result.answer})

    response = await _build_response(req, ctx, result)
    yield sse("final", response.model_dump(mode="json"))


@router.delete("/chat/deep/{thread_id}")
async def forget_thread(thread_id: str,
                        user: UserOut = Depends(get_current_admin)) -> dict:
    """Drop a thread's checkpoints.

    The server-side twin of starting a new chat. The tiered loop needs the
    client to null its scope for this; here the client asks the server to
    forget, and a client that forgets to ask leaks memory into the next
    conversation exactly the way `apiChatScope` did — so `web/app.js` calls
    this from `newThread()` and from conversation delete.
    """
    key = _thread_id(thread_id)
    await _saver().adelete_thread(key)
    return {"thread_id": key, "forgotten": True}
