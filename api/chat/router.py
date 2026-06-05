"""
api/chat/router.py
------------------
`/chat` (run the agent) + `/modes` (mode registry). Extracted from
api/main.py. Owns the rolling-scope extraction, UI/LLM result split, and
stale-turn history trimming that keep multi-turn conversations cheap, plus
the anonymous-chat throttle and per-turn latency observability.
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)

from api.agent import ChatDeps, agent
from api.auth import get_optional_user
from api.auth.schemas import UserOut
from api.observability import SLOW_AGENT_MS, timed

logger = logging.getLogger(__name__)

router = APIRouter()

_SEARCH_TOOLS = {"search_auctions", "semantic_property_search"}

# Args that describe scope we want to carry across turns. Excludes output
# controls (limit, order_by) and aggregate knobs — those don't narrow the
# user's target set, they just shape the current call.
_CARRY_FORWARD_FILTER_KEYS = {
    "min_price", "max_price",
    "city", "area",
    "property_type", "asset_category",
    "bank",
    "starts_after", "starts_before",
}

_GATED_MODES = {"deep-research", "report"}

# Simple in-memory hourly counter for anonymous /chat. slowapi's decorator can't
# see Depends-provided state, so we enforce this manually only when user=None.
_ANON_CHAT_MAX_PER_HOUR = 10
_anon_chat_hits: dict[str, list[float]] = {}


def _enforce_anon_chat_limit(request: Request) -> None:
    if os.environ.get("RATELIMIT_DISABLED", "").lower() in {"1", "true", "yes"}:
        return
    now = time.time()
    window = 3600.0
    ip = request.client.host if request.client else "unknown"
    hits = [t for t in _anon_chat_hits.get(ip, []) if now - t < window]
    if len(hits) >= _ANON_CHAT_MAX_PER_HOUR:
        raise HTTPException(status_code=429, detail="rate limit exceeded")
    hits.append(now)
    _anon_chat_hits[ip] = hits


class ChatRequest(BaseModel):
    message: str
    message_history: list[dict[str, Any]] | None = None
    mode: str | None = None
    # Filters the client wants the agent to scope to on the next turn — e.g.
    # the browse panel's current selection. History-extracted filters layer
    # on top so the rolling-scope behavior across turns still works.
    active_filters: dict[str, Any] | None = None


# Whitelist of modes the agent will overlay. Each maps to a modes/<id>.md file.
# Keeping this explicit prevents arbitrary file reads from the modes/ dir.
_AVAILABLE_MODES: list[dict[str, Any]] = [
    {
        "id": "ask",
        "label": "Ask",
        "description": "Free-form Q&A over the Neo4j graph (default).",
        "examples": [
            "Residential auctions in Chennai under 30 lakhs",
            "What is the price range in Kanchipuram?",
            "Show auctions with deadline in the next 7 days",
            "How many banks have auctions in Chennai?",
            "Which borrowers have more than 3 properties?",
            "List all cities in the database",
        ],
    },
    {
        "id": "deep-research",
        "label": "Deep research",
        "description": "7-step due-diligence workflow on one auction_id.",
        "examples": [
            "Deep research on auction AUC-12345",
        ],
    },
    {
        "id": "compare",
        "label": "Compare",
        "description": "Side-by-side comparison of 2–5 auctions.",
        "examples": [
            "Compare AUC-12345 and AUC-67890",
        ],
    },
    {
        "id": "report",
        "label": "Personalized report",
        "description": "Investment report tuned to an investor profile.",
        "examples": [
            "Report on AUC-12345 for a conservative investor under 50 lakhs",
        ],
    },
]


class ToolArtifact(BaseModel):
    tool: str
    args: dict[str, Any] | str | None = None
    result: Any = None
    # UI-only row overflow from search tools. Populated when a search's
    # `total_count` exceeds the model-visible `limit` — lets the right-side
    # artifacts panel render every match without inflating the LLM's
    # context. Never included in `result` (which is what the model sees on
    # follow-up turns).
    ui_rows: list[dict[str, Any]] | None = None


class ChatResponse(BaseModel):
    answer: str
    artifacts: list[ToolArtifact] = []
    message_history: list[dict[str, Any]] = []


def _extract_active_filters(messages) -> tuple[dict, int | None]:
    """Walk the message history and merge every prior search tool's scope
    filters into a single rolling dict — the "active scope" of the
    conversation so far.

    Rules:
      * Later calls overwrite earlier ones for the same key, so the latest
        user narrowing wins.
      * Keys not in `_CARRY_FORWARD_FILTER_KEYS` (e.g. `limit`, `order_by`,
        `aggregations`) are ignored — they're per-call, not scope.
      * A call that passes an explicit `None` for a key clears that key —
        the model is effectively telling us it dropped that scope.
      * Non-search tool calls don't affect the scope.

    Returns (filters, last_total_count). `last_total_count` is the
    total_count of the most recent search for context.
    """
    calls: dict[str, ToolCallPart] = {}
    filters: dict = {}
    last_total: int | None = None
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart) and part.tool_name in _SEARCH_TOOLS:
                call = calls.get(part.tool_call_id)
                if call is None:
                    continue
                try:
                    args = call.args_as_dict()
                except Exception:
                    args = {}
                for key in _CARRY_FORWARD_FILTER_KEYS:
                    if key not in args:
                        continue
                    val = args[key]
                    if val is None:
                        filters.pop(key, None)
                    else:
                        filters[key] = val
                result = part.content
                if isinstance(result, dict) and "total_count" in result:
                    last_total = result["total_count"]
                elif isinstance(result, list):
                    last_total = len(result)
    return filters, last_total


def _split_ui_rows(result: Any) -> tuple[Any, list[dict[str, Any]] | None]:
    """Pop the UI-only overflow from a search-tool result.

    `search_auctions` returns `_ui_results` when total_count exceeds the
    model-visible `limit`. That list belongs in the HTTP response so the
    right-side panel can render every match — but it must NOT flow back
    into the LLM's message history on the next turn, or we defeat the
    whole point of the split. We copy the dict (so we don't mutate the
    in-memory ToolReturnPart content), pop `_ui_results`, and return the
    trimmed copy for the LLM + the raw list for the UI.
    """
    if not isinstance(result, dict) or "_ui_results" not in result:
        return result, None
    trimmed = {k: v for k, v in result.items() if k != "_ui_results"}
    ui_rows = result.get("_ui_results")
    if not isinstance(ui_rows, list):
        ui_rows = None
    return trimmed, ui_rows


def _extract_artifacts(messages) -> list[ToolArtifact]:
    """Pair ToolCallPart with its matching ToolReturnPart by tool_call_id.

    When a search tool returns `_ui_results`, move those rows onto the
    artifact's `ui_rows` field and strip them from `result`. This keeps
    the UI payload rich while the LLM-facing `result` stays lean.
    """
    calls: dict[str, ToolCallPart] = {}
    artifacts: list[ToolArtifact] = []
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolCallPart):
                calls[part.tool_call_id] = part
            elif isinstance(part, ToolReturnPart):
                call = calls.get(part.tool_call_id)
                if call is not None:
                    try:
                        args = call.args_as_dict()
                    except Exception:
                        args = call.args if isinstance(call.args, (dict, str)) else str(call.args)
                    result, ui_rows = _split_ui_rows(part.content)
                    artifacts.append(ToolArtifact(
                        tool=part.tool_name,
                        args=args,
                        result=result,
                        ui_rows=ui_rows,
                    ))
    return artifacts


def _strip_ui_rows_from_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove `_ui_results` from any ToolReturnPart content in a dumped
    message history. Prevents the UI overflow from ballooning the LLM's
    context on the next turn when the client echoes history back.
    """
    for msg in history:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if isinstance(content, dict) and "_ui_results" in content:
                content.pop("_ui_results", None)
    return history


# ── old tool-result trimming ───────────────────────────────────────────────
# Once the model has read a heavy tool result and written its prose answer,
# the raw rows rarely need to re-enter context on later turns — the agent can
# always re-query the graph. We keep the most recent turns fully detailed and
# squeeze older, heavy tool returns down to a breadcrumb stub, shrinking the
# history the client echoes back into the LLM on the next /chat turn. This is
# the same philosophy as `_strip_ui_rows_from_history`, applied to the rows the
# model *did* see (but only on stale turns).
_HISTORY_KEEP_FULL_TURNS = max(1, int(os.getenv("CHAT_HISTORY_KEEP_FULL_TURNS", "2")))
# Only trim tool returns whose JSON is at least this many chars — leaves small
# aggregate/stat results (list_distinct) untouched so the
# model keeps cheap-but-useful context, and avoids stubs larger than the
# original payload.
_HISTORY_TRIM_MIN_CHARS = int(os.getenv("CHAT_HISTORY_TRIM_MIN_CHARS", "600"))
# How many auction_ids to keep as a breadcrumb so the model can still reference
# (or re-query) specific properties from a trimmed turn.
_HISTORY_TRIM_ID_SAMPLE = int(os.getenv("CHAT_HISTORY_TRIM_ID_SAMPLE", "20"))
_HISTORY_TRIM_NOTE = "older result trimmed to save context — re-call the tool for full rows"


def _summarize_tool_return_content(content: Any) -> Any:
    """Collapse a heavy tool-return `content` to a compact breadcrumb stub.

    Keeps the cheap facts the model reasons over (row count, total_count, a
    sample of auction_ids) and drops the bulky per-row payload. Content that
    is already a stub or isn't row-shaped is returned unchanged.
    """
    if isinstance(content, dict):
        if content.get("_trimmed"):
            return content  # already trimmed — idempotent
        stub: dict[str, Any] = {"_trimmed": True}
        if "total_count" in content:
            stub["total_count"] = content["total_count"]
        rows = content.get("results")
        if isinstance(rows, list):
            stub["returned"] = len(rows)
            ids = [r["auction_id"] for r in rows
                   if isinstance(r, dict) and r.get("auction_id")]
            if ids:
                stub["auction_ids"] = ids[:_HISTORY_TRIM_ID_SAMPLE]
        elif content.get("auction_id"):
            # Single-record result (e.g. get_auction_detail).
            stub["auction_id"] = content["auction_id"]
        stub["_note"] = _HISTORY_TRIM_NOTE
        return stub
    if isinstance(content, list):
        ids = [r["auction_id"] for r in content
               if isinstance(r, dict) and r.get("auction_id")]
        stub = {"_trimmed": True, "returned": len(content), "_note": _HISTORY_TRIM_NOTE}
        if ids:
            stub["auction_ids"] = ids[:_HISTORY_TRIM_ID_SAMPLE]
        return stub
    return content


def _trim_old_tool_results(
    history: list[dict[str, Any]],
    keep_full_turns: int = _HISTORY_KEEP_FULL_TURNS,
) -> list[dict[str, Any]]:
    """Squeeze heavy tool-return payloads on all but the most recent turns.

    A "turn" starts at each message carrying a `user-prompt` part. The last
    `keep_full_turns` turns are left fully intact (the agent may still be
    working with their results); older turns get their large tool returns
    replaced with breadcrumb stubs. Mutates and returns `history`.
    """
    if not history or keep_full_turns < 1:
        return history
    turn_starts = [
        i for i, msg in enumerate(history)
        if any(p.get("part_kind") == "user-prompt" for p in msg.get("parts", []))
    ]
    if len(turn_starts) <= keep_full_turns:
        return history  # nothing older than the keep window
    cutoff = turn_starts[-keep_full_turns]
    trimmed_count = 0
    saved_chars = 0
    for msg in history[:cutoff]:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if isinstance(content, dict) and content.get("_trimmed"):
                continue
            content_json = json.dumps(content, default=str)
            if len(content_json) < _HISTORY_TRIM_MIN_CHARS:
                continue
            part["content"] = _summarize_tool_return_content(content)
            saved_chars += max(0, len(content_json) - len(json.dumps(part["content"], default=str)))
            trimmed_count += 1
    if trimmed_count:
        logger.info(
            "chat history: trimmed %d old tool result(s) (~%d chars, ≈%d tokens) "
            "beyond the last %d turn(s)",
            trimmed_count, saved_chars, saved_chars // 4, keep_full_turns,
        )
    return history


def _strip_dynamic_system_prompts_from_history(
    history: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop any SystemPromptPart with a `dynamic_ref` from incoming history.

    The agent used to register `inject_prior_search` and `inject_mode_overlay`
    as `@agent.system_prompt(dynamic=True)`, which persists their output as
    SystemPromptParts carrying a `dynamic_ref` qualname. After we migrated
    those functions to `@agent.instructions` (cleaner Gemini cache prefix —
    instructions are skipped when empty, and not persisted in history),
    the refs in older stored histories no longer resolve to runners, so
    pydantic-ai would leave them frozen with stale "Active scope" text.
    Stripping them keeps history clean while letting the agent re-add the
    fresh content as an instruction message on the new turn.
    """
    for msg in history:
        parts = msg.get("parts", [])
        msg["parts"] = [
            p for p in parts
            if not (
                p.get("part_kind") == "system-prompt"
                and p.get("dynamic_ref")
            )
        ]
    return history


def _usage_fields(result: Any) -> dict[str, Any]:
    """Best-effort token accounting for the per-turn obs log.

    Pulls input / output / cached token counts and the LLM round-trip count
    off the pydantic-ai run result. One user message fans out to several LLM
    calls (each tool round-trip re-sends the system+tools prefix), so
    `llm_calls` × the prefix size is where most input tokens go.

    `cached_tokens` is the prompt-cache-hit portion of the input: when the
    OpenRouter→Gemini route honors implicit caching of the stable prefix it
    shows up here; a steady 0 means the prefix is billed at full rate every
    call — the signal to move to a direct Vertex client. Wrapped in
    getattr/try so a pydantic-ai field rename degrades to an empty dict
    instead of 500-ing a chat turn; this is telemetry, never load-bearing.
    """
    try:
        u = result.usage()
    except Exception:  # noqa: BLE001 - telemetry must never break the turn
        return {}

    def pick(*names: str) -> int:
        for n in names:
            v = getattr(u, n, None)
            if isinstance(v, int) and v:
                return v
        return 0

    cached = pick("cache_read_tokens")
    if not cached:
        # Fallback: some providers report the hit under usage `details`
        # (e.g. {"cached_tokens": N}) rather than the typed field.
        details = getattr(u, "details", None)
        if isinstance(details, dict):
            for k, v in details.items():
                if "cache" in str(k).lower() and isinstance(v, int) and v:
                    cached = v
                    break
    return {
        "llm_calls": pick("requests"),
        "input_tokens": pick("input_tokens", "request_tokens"),
        "cached_tokens": cached,
        "output_tokens": pick("output_tokens", "response_tokens"),
    }


@router.get("/modes")
def list_modes() -> dict:
    """Mode registry consumed by the web UI to render the mode selector and
    suggestion chips. Mirrors the career-ops pattern of surfacing each
    markdown mode file as a user-facing entry point."""
    return {"modes": _AVAILABLE_MODES}


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    req: ChatRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> ChatResponse:
    mode = req.mode
    if mode in _GATED_MODES and (user is None or not user.email_verified):
        raise HTTPException(status_code=401, detail="login required for this mode")
    if user is None:
        _enforce_anon_chat_limit(request)
    history = (
        ModelMessagesTypeAdapter.validate_python(
            _strip_dynamic_system_prompts_from_history(req.message_history)
        )
        if req.message_history
        else None
    )
    if mode:
        valid_ids = {m["id"] for m in _AVAILABLE_MODES}
        if mode not in valid_ids or mode == "ask":
            # Unknown mode or the default "ask" sentinel — don't overlay anything.
            mode = None
    if history:
        history_filters, last_total = _extract_active_filters(history)
    else:
        history_filters, last_total = {}, None
    # Client-supplied filters (e.g. from the browse panel "chat about these"
    # button) seed the scope; whatever the agent has narrowed across prior
    # turns layers on top so explicit refinements still win.
    client_filters = req.active_filters or {}
    active_filters = {**client_filters, **history_filters}
    deps = ChatDeps(
        active_filters=active_filters or None,
        last_total_count=last_total,
        mode=mode,
    )
    try:
        # Time the LLM round-trip on its own budget so slow-agent turns are
        # easy to spot in logs (auction.obs chat.agent_run ... elapsed_ms=...).
        with timed("chat.agent_run", slow_ms=SLOW_AGENT_MS,
                   mode=mode or "ask", history_msgs=len(history) if history else 0) as obs:
            result = await agent.run(req.message, message_history=history, deps=deps)
            # Attach token/cache counts to the obs line so a turn's cost — and
            # whether the stable prefix is hitting the implicit prompt cache —
            # is greppable next to its latency.
            usage = _usage_fields(result)
            obs.update(usage)
    except Exception:
        # Most often pydantic-ai's UnexpectedModelBehavior or a transient
        # OpenRouter error. Log with the user message so the failing input is
        # recoverable from Render logs, then surface a friendly 500.
        logger.exception("agent.run failed for message=%r mode=%r", req.message, mode)
        raise HTTPException(status_code=500, detail="chat agent failed — please retry")
    artifacts = _extract_artifacts(result.new_messages())
    logger.info(
        "chat turn ok mode=%s tool_calls=%d answer_chars=%d "
        "llm_calls=%s in_tok=%s cached_tok=%s out_tok=%s",
        mode or "ask", len(artifacts), len(result.output or ""),
        usage.get("llm_calls"), usage.get("input_tokens"),
        usage.get("cached_tokens"), usage.get("output_tokens"),
    )
    dumped_history = ModelMessagesTypeAdapter.dump_python(result.all_messages(), mode="json")
    # Strip `_ui_results` from the history echoed back to the client —
    # otherwise the client ships it back on the next /chat turn and it
    # re-enters the LLM's context, defeating the UI/LLM split.
    dumped_history = _strip_ui_rows_from_history(dumped_history)
    # Squeeze heavy tool results on stale turns down to breadcrumb stubs so a
    # long conversation's history stops re-billing the model for rows it has
    # already summarized. Recent turns stay fully detailed; the agent can
    # re-query for anything it trimmed. Artifacts (above) are extracted from
    # the live `new_messages()` objects, so the UI payload is unaffected.
    dumped_history = _trim_old_tool_results(dumped_history)
    return ChatResponse(
        answer=result.output,
        artifacts=artifacts,
        message_history=dumped_history,
    )
