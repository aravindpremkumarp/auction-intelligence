"""
api/agent3/router.py
--------------------
`POST /chat/agent3`, `POST /chat/agent3/stream`, `DELETE /chat/agent3/{thread}`.

The fourth chat surface, alongside `/chat` (pydantic-ai v1), `/chat/v2` (the
tiered loop) and `/chat/deep`. None of those three are touched — this is the
front door for `api/agent3/`, which until now nothing outside the eval scripts
could call.

**It follows `/chat/deep`'s contract, not `/chat/v2`'s.** Same reason: the
transcript lives server-side under a `thread_id`, so there is no `scope`
object to round-trip through the client. v1 round-trips a transcript, v2
round-trips a summary, both of these round-trip neither.

**What it deliberately does NOT return.** `/chat/deep` subclasses
`ChatV2Response` and fills `scope` and `plan` — `scope` with `turn=0` and
filters carried "for the matches panel only". This endpoint returns neither:

- `scope` would be a lie. The memory is the checkpointed transcript; a scope
  object next to it is the second source of truth whose four un-cleared reset
  sites are what put memory on the server in the first place.
- `plan` would be a half-lie. agent3 counts tool calls but does not record
  per-call args and timings, so the field could only be filled with
  placeholders. The frontend reads both defensively (`resp.scope ||
  apiChatScope`, and `scope` only under the v2 flag), so omitting them costs
  nothing and claiming them would cost the next reader an hour.

**Admin only**, same as /chat/v2 and /chat/deep, for the same reason: every
request spends real money on the prepaid OpenRouter key.

**Lazy imports.** `langchain_openai` is ~28 MB of RSS against a 512 MB
instance, so every import that reaches the loop lives inside a handler and an
idle deploy never pays for it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from api.auth import get_current_admin
from api.auth.schemas import UserOut
from api.chat.gating import enforce_chat_quota, resolve_turn_model
from api.chat.v2.schemas import PanelIn
from api.observability import record

logger = logging.getLogger(__name__)

router = APIRouter()

#: Hard cap on one message. The loop bounds its own cost per turn, but an
#: unbounded prompt is prepended to a cached prefix and re-sent on every later
#: turn of the thread, so a single huge paste is a permanent tax on the
#: conversation, not a one-off.
MAX_MESSAGE_CHARS = 4000


class ChatAgent3Request(BaseModel):
    message: str
    #: The conversation id. The browser already mints one per thread for
    #: `api/conversations`; reusing it means the transcript and the saved
    #: conversation share a key and a lifecycle. Omitted on the first turn of
    #: an unsaved chat — the server mints one and returns it.
    thread_id: str | None = None
    #: What the matches panel is currently showing. Used only to avoid
    #: re-sending an identical set, which would make the panel flicker.
    panel: PanelIn | None = None
    model: str | None = None
    reasoning_effort: str | None = None


class GateOut(BaseModel):
    """What the answer gate did this turn.

    A diagnostic, not product surface. `repairs` above zero means the model's
    first draft broke a rule checked in code and was made to rewrite —
    `repaired` says which rule, because the offending draft is deleted and
    would otherwise leave no trace.
    """

    repairs: int = 0
    repaired: list[str] = Field(default_factory=list)
    #: Numeric findings that are recorded and never acted on. Present so the
    #: false-positive rate stays visible; see api/agent3/gates.py.
    advisory: list[str] = Field(default_factory=list)


class ChatAgent3Response(BaseModel):
    answer: str
    thread_id: str = ""
    #: Same shape v1 returns, so the whole matches-panel path in web/app.js
    #: works unchanged.
    artifacts: list[dict[str, Any]] = Field(default_factory=list)
    #: Skills the turn loaded. Cheap to return and the fastest way to see why
    #: an answer went the way it did.
    skills: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    gate: GateOut | None = None
    #: What the UI should show for THIS turn — cards, the agent's own sentence
    #: per card, the query echo, the counts. Carries its own `turn_index` so
    #: the frontend can tag the message it just rendered and join by the same
    #: ordinal on reload. See api/agent3/manifest.py and
    #: docs/designs/turn-owned-property-cards.md. None if the build failed:
    #: the answer still stands, it just renders without cards.
    manifest: dict[str, Any] | None = None


def _thread_id(raw: str | None) -> str:
    """A safe thread key.

    Client-supplied and used as a Neo4j property in a MERGE, so it is bounded
    and stripped of anything not id-shaped. A malformed id mints a fresh
    thread rather than erroring: losing one turn's memory is a far better
    failure than refusing to answer.
    """
    text = (raw or "").strip()
    if text and len(text) <= 64 and all(c.isalnum() or c in "-_" for c in text):
        return text
    return f"agent3-{uuid.uuid4()}"


def _saver():
    """The checkpointer, built per request.

    Holds no connection of its own — `api/neo4j_client` owns the pooled driver
    — so construction is free and there is no shared mutable state across
    requests.
    """
    from api.checkpointer import Neo4jSaver

    return Neo4jSaver()


async def _prepare(request: Request, req: ChatAgent3Request,
                   user: UserOut | None) -> dict[str, Any]:
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message is required")
    if len(message) > MAX_MESSAGE_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"message is too long ({len(message)} chars, "
                   f"max {MAX_MESSAGE_CHARS})")

    # One counter across all four endpoints: a caller must not be able to
    # spend the same allowance four times by rotating between them.
    await enforce_chat_quota(request, user)
    model_name, effort = resolve_turn_model(user, req.model, req.reasoning_effort)

    from api.chat.v2.scope import sanitize_ids

    return {
        "message": message,
        "thread_id": _thread_id(req.thread_id),
        "model_name": model_name,
        "reasoning_effort": effort,
        "panel": sanitize_ids((req.panel.matches if req.panel else []) or []),
    }


async def _persist_manifest(result) -> dict[str, Any] | None:
    """Store this turn's manifest and return it for the response.

    Best-effort by design, the same rule `artifacts.py` runs under: the
    manifest says how to *draw* a turn, and losing it must never cost the user
    an answer they already paid for. A turn whose manifest failed to store
    renders card-less on reload — visibly degraded, never wrong.

    Two writers on one thread compute the same `turn_index`, so one manifest
    silently overwrites the other while both answers persist. That is a real
    misalignment rather than a benign race, so it is logged here; recovery is
    the card-less rule above, because shifting ordinals to absorb it would
    make every later join lie.
    """
    manifest = getattr(result, "manifest", None)
    if manifest is None:
        return None
    from api.agent3 import manifest_store

    try:
        await manifest_store.save(manifest)
    except Exception:  # noqa: BLE001 - display metadata, never fatal
        logger.exception("manifest store failed — turn stands without cards")
    return manifest.to_dict()


async def _build_response(ctx: dict, result) -> ChatAgent3Response:
    from api.agent3.artifacts import build_artifacts

    artifacts = await build_artifacts(result, panel_before=ctx["panel"])
    manifest = await _persist_manifest(result)
    findings = result.gate_findings or {}
    # On `auction.obs`, not this module's logger. The line this replaced went
    # to `api.agent3.router`, which has no handler on it in any environment —
    # root logging is unconfigured and `api/telemetry.py` attached Logfire to
    # `auction.obs` alone — so five real conversations went through this
    # endpoint on 22 Aug and their token counts reached neither Render's logs
    # nor Logfire. The usage was computed, returned to the browser, and
    # dropped.
    usage = result.usage or {}
    record(
        "chat_agent3.turn",
        model=ctx["model_name"], thread=ctx["thread_id"],
        llm_calls=result.model_calls, tool_calls=result.tool_calls,
        answer_chars=len(result.answer or ""),
        skills=",".join(result.skills_loaded) or "-",
        in_tok=usage.get("input_tokens", 0),
        cached_tok=usage.get("cached_input_tokens", 0),
        out_tok=usage.get("output_tokens", 0),
        total_tok=usage.get("total_tokens", 0),
        seconds=result.seconds,
        gate_repairs=result.gate_repairs,
        artifacts=len(artifacts),
    )
    return ChatAgent3Response(
        answer=result.answer,
        thread_id=ctx["thread_id"],
        artifacts=artifacts,
        skills=list(result.skills_loaded),
        usage={
            "llm_calls": result.model_calls,
            "tool_calls": result.tool_calls,
            "input_tokens": (result.usage or {}).get("input_tokens", 0),
            "cached_tokens": (result.usage or {}).get("cached_input_tokens", 0),
            "output_tokens": (result.usage or {}).get("output_tokens", 0),
            "seconds": result.seconds,
        },
        gate=GateOut(
            repairs=result.gate_repairs,
            repaired=list(result.gate_repaired),
            advisory=list(findings.get("advisory") or []),
        ),
        manifest=manifest,
    )


@router.post("/chat/agent3", response_model=ChatAgent3Response)
async def chat_agent3(request: Request, req: ChatAgent3Request,
                      user: UserOut = Depends(get_current_admin)
                      ) -> ChatAgent3Response:
    ctx = await _prepare(request, req, user)
    from api.agent3.loop import run_turn

    # No `timed` wrapper here any more: `run_turn` times the turn itself, so
    # both this endpoint and the streaming one are covered by one line
    # instead of only this one being covered by two.
    result = await run_turn(
        ctx["message"],
        thread_id=ctx["thread_id"],
        model_name=ctx["model_name"],
        reasoning_effort=ctx["reasoning_effort"],
        checkpointer=_saver(),
    )
    return await _build_response(ctx, result)


@router.post("/chat/agent3/stream")
async def chat_agent3_stream(request: Request, req: ChatAgent3Request,
                             user: UserOut = Depends(get_current_admin)):
    ctx = await _prepare(request, req, user)
    from api.chat.router import _sse, _with_heartbeat

    return StreamingResponse(
        _with_heartbeat(_stream_turn(ctx, _sse)),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


async def _stream_turn(ctx: dict, sse) -> AsyncIterator[str]:
    """Run the turn and emit the answer, then the final payload.

    **Not token streaming, and the docstring says so rather than letting the
    endpoint imply it.** `api/agent3/loop.py::run_turn` awaits the whole graph
    and returns a finished `TurnResult`; there is no per-token channel to
    relay. This exists so the frontend's SSE path works against agent3
    unchanged, and so a long turn holds the connection open with heartbeats
    instead of looking hung. Real deltas need `astream_events` on the compiled
    graph, which is a change to the loop, not to this handler.
    """
    import httpx

    from api.agent3.loop import run_turn

    task = asyncio.create_task(run_turn(
        ctx["message"],
        thread_id=ctx["thread_id"],
        model_name=ctx["model_name"],
        reasoning_effort=ctx["reasoning_effort"],
        checkpointer=_saver(),
    ))
    try:
        result = await task
    except (httpx.TimeoutException, TimeoutError):
        logger.exception("chat agent3 stream timed out")
        yield sse("error", {"detail": "the model took too long — please retry"})
        return
    except Exception:
        logger.exception("chat agent3 stream failed")
        yield sse("error", {"detail": "chat agent failed — please retry"})
        return
    finally:
        # A disconnecting client throws GeneratorExit here. Without this the
        # detached turn keeps calling the model and querying Neo4j for someone
        # who has gone.
        if not task.done():
            task.cancel()

    if result.answer:
        yield sse("delta", {"text": result.answer})
    response = await _build_response(ctx, result)
    # A dedicated event immediately before `final`, carrying the same body the
    # non-stream response puts in its `manifest` field. Separate so a client
    # can draw the cards the moment they are known without having to wait for
    # (or re-parse) the full payload.
    if response.manifest is not None:
        yield sse("manifest", response.manifest)
    yield sse("final", response.model_dump(mode="json"))


async def _thread_messages(thread_id: str) -> list:
    """The thread's checkpointed messages, or [] if it has none."""
    tup = await _saver().aget_tuple({"configurable": {"thread_id": thread_id}})
    if tup is None:
        return []
    values = (tup.checkpoint or {}).get("channel_values") or {}
    return list(values.get("messages") or [])


@router.get("/chat/agent3/{thread_id}/history")
async def thread_history(thread_id: str,
                         user: UserOut = Depends(get_current_admin)) -> dict:
    """The thread's user questions and final answers, in order.

    The client keeps its own copy of the conversation for display, and #404
    bans joining on it: a copy the browser owns can be edited, truncated or
    stale, and an ordinal computed from it would silently attach turn 4's
    cards to turn 3. This is the server's count, and it is the only one the
    manifest join may use.

    Which messages count and which are dropped lives in
    `manifest.history_from_messages`, so the ordinal rule is one function with
    tests rather than logic buried in an endpoint.
    """
    from api.agent3.manifest import history_from_messages

    key = _thread_id(thread_id)
    return {"thread_id": key,
            "messages": history_from_messages(await _thread_messages(key))}


@router.get("/chat/agent3/{thread_id}/manifests")
async def thread_manifests(thread_id: str,
                           user: UserOut = Depends(get_current_admin)) -> dict:
    """Every turn's manifest for a thread, in turn order.

    The other half of reload: the frontend fetches this alongside `/history`
    and joins on `turn_index`, so a reopened thread draws exactly the cards
    each answer produced. A turn with no matching manifest renders card-less
    — ordinals never shift to close a gap, because a shifted ordinal makes
    every later turn show another turn's properties.
    """
    from api.agent3 import manifest_store

    key = _thread_id(thread_id)
    return {"thread_id": key, "manifests": await manifest_store.load_thread(key)}


@router.delete("/chat/agent3/{thread_id}")
async def forget_thread(thread_id: str,
                        user: UserOut = Depends(get_current_admin)) -> dict:
    """Drop a thread's checkpoints — the server-side twin of a new chat.

    The tiered loop needs the client to null its scope for this. Here the
    client asks the server to forget, and a client that forgets to ask leaks
    memory into the next conversation exactly the way `apiChatScope` did.
    """
    from api.agent3 import manifest_store

    key = _thread_id(thread_id)
    await _saver().adelete_thread(key)
    # Manifests are part of the thread, so they go with it — retention is the
    # thread's lifetime and there is no separate story to tell. Kept here
    # rather than inside `adelete_thread` because `api/checkpointer.py` is
    # deliberately generic: it must stay importable with nothing but the Neo4j
    # driver, and reaching into `api/agent3` from there would end that.
    await manifest_store.delete_thread(key)
    return {"thread_id": key, "forgotten": True}
