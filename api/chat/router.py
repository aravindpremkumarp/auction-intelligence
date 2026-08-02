"""
api/chat/router.py
------------------
`/chat` (run the agent) + `/modes` (mode registry). Extracted from
api/main.py. Owns the rolling-scope extraction, UI/LLM result split, and
stale-turn history trimming that keep multi-turn conversations cheap, plus
the anonymous-chat throttle and per-turn latency observability.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    FinalResultEvent,
    FunctionToolCallEvent,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.run import AgentRunResultEvent
from pydantic_ai.usage import UsageLimits

from api.agent import ChatDeps, agent, build_chat_run_overrides
from api.chat.panel import panel_sync_ids
from api.chat.suggestions import build_suggestions
from api.tools import cypher_tools as cypher_T
from api.auth import get_optional_user
from api.auth import repository as auth_repo
from api.auth.schemas import UserOut
from api.model_selection import (
    CHAT_MODEL_OPTIONS,
    DEFAULT_PAID_MODEL,
    EFFORT_RANK,
    FREE_TIER_EFFORT,
    FREE_TIER_MODEL,
    REASONING_EFFORT_OPTIONS,
    resolve_chat_model,
    resolve_reasoning_effort,
)
from api.observability import SLOW_AGENT_MS, timed

logger = logging.getLogger(__name__)

router = APIRouter()

# `semantic_property_search` is the tool's pre-rename name — kept so scope
# extraction still works on stored histories from before the rename.
_SEARCH_TOOLS = {"search_auctions", "semantic_search", "semantic_property_search"}

# Args that describe scope we want to carry across turns — every filter
# that narrows the user's target set. Excludes output controls (limit,
# order_by) and aggregate/grouping knobs (they shape the current call, not
# the scope), and `include_past` (a one-off retrospective retry shouldn't
# stick to the whole conversation). Keep this in sync when search_auctions
# grows a filter: a key missing here means the "Active search scope" block
# silently drops that scope on follow-up turns (bit us when borrower/EMD/
# platform/is_reauction were added without updating this set).
_CARRY_FORWARD_FILTER_KEYS = {
    "min_price", "max_price",
    "min_emd", "max_emd",
    "city", "area",
    "property_type", "asset_category",
    "bank", "borrower",
    "auction_type", "branch_name",
    "service_provider",
    "is_reauction",
    "starts_after", "starts_before",
    "deadline_within_days",
}

_GATED_MODES = {"deep-research"}

# Upper bound on panel auction_ids forwarded into the agent's context per turn.
# The panel rarely holds more than a shortlist, but a wide browse selection
# could, and we don't want a runaway list re-billed every turn — the whole
# point of passing ids (not rows) is to stay cheap. 50 short ids is still a
# tiny prefix; beyond that, the top of the ranked/sorted list is what matters.
_MAX_PANEL_IDS = 50

# Chat rate limiting is a durable day + month quota (see the quota helpers
# below): anonymous callers are capped per hashed IP, logged-in users per
# account. No in-memory/hourly throttle — the monthly window must survive
# restarts, which an in-memory counter can't.

# Per-run ceiling on LLM round-trips. pydantic-ai's default is 50, which at
# reasoning-effort pricing lets one pathological tool loop cost ~50x a normal
# turn before the per-user daily cap even notices. Normal "ask" turns take
# 1-3 requests; deep-research's 7-step flow peaks around 10-12. Read per call
# (not at import) so tests and ops can retune without a restart.
_CHAT_REQUEST_LIMIT_DEFAULT = 15

# Per-run ceiling on *input* tokens, summed across the turn's round-trips.
# `request_limit` alone bounds the number of steps but not their size, and
# input grows superlinearly within a turn: every step re-sends the whole
# accumulated prefix, so step N costs roughly the sum of everything before
# it. Observed worst case: a 13-step turn billed 634,750 input tokens to
# produce 2,632 tokens of answer, and a second one hit `request_limit`
# after 318,856 input tokens and then failed — the user paid for all of it
# and got an error.
#
# 250k leaves ample headroom over the observed p95 turn (~195k) while
# turning the unbounded tail into a bounded, explainable failure. Counted
# against cumulative usage, so cache hits still count — this is a blast
# radius guard, not a cost target.
_CHAT_INPUT_TOKEN_LIMIT_DEFAULT = 250_000


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _usage_limits() -> UsageLimits:
    return UsageLimits(
        request_limit=_env_int("CHAT_REQUEST_LIMIT", _CHAT_REQUEST_LIMIT_DEFAULT),
        input_tokens_limit=_env_int(
            "CHAT_INPUT_TOKEN_LIMIT", _CHAT_INPUT_TOKEN_LIMIT_DEFAULT
        ),
    )


# Friendly status labels for the streaming UI, keyed by tool name. Anything
# unlisted falls back to a generic label so new tools degrade gracefully.
_TOOL_STATUS_LABELS = {
    "search_auctions": "Searching auctions…",
    "semantic_search": "Searching notices semantically…",
    "get_auction_detail": "Fetching auction details…",
    "describe_schema": "Reading the graph schema…",
    "run_cypher": "Querying the graph…",
    "internet_search": "Searching the web…",
    # pydantic-ai's framework tools for deferred capabilities (see
    # api/agent.py): the model opens a tool bundle before first use.
    "load_capability": "Loading extra tools…",
    "search_tools": "Loading extra tools…",
}


def _tool_status(part: ToolCallPart) -> str:
    label = _TOOL_STATUS_LABELS.get(part.tool_name, "Working…")
    if part.tool_name == "run_cypher":
        # run_cypher carries a one-sentence intent summary meant for UI chips —
        # reuse it so the status line says what the query is actually doing.
        args = part.args
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                args = {}
        desc = (args or {}).get("description") if isinstance(args, dict) else None
        if desc:
            label = f"Querying the graph: {desc}"
    return label
# Day + month quota caps, read per call so ops/tests can retune without a
# restart. Anonymous (per hashed IP) is the tightest and its monthly window is
# the main nudge to log in. Free logged-in gets a larger allowance; paid gets
# the big daily cost-guard cap and no monthly cap (paying customers aren't
# throttled by month).
_CHAT_ANON_DAILY_LIMIT_DEFAULT = 10
_CHAT_ANON_MONTHLY_LIMIT_DEFAULT = 30
_CHAT_FREE_DAILY_LIMIT_DEFAULT = 20
_CHAT_FREE_MONTHLY_LIMIT_DEFAULT = 100
_CHAT_PAID_DAILY_LIMIT_DEFAULT = 1000


def _ratelimit_disabled() -> bool:
    return os.environ.get("RATELIMIT_DISABLED", "").lower() in {"1", "true", "yes"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _anon_caps() -> tuple[int, int]:
    """(daily, monthly) caps for anonymous callers."""
    return (
        _int_env("CHAT_ANON_DAILY_LIMIT", _CHAT_ANON_DAILY_LIMIT_DEFAULT),
        _int_env("CHAT_ANON_MONTHLY_LIMIT", _CHAT_ANON_MONTHLY_LIMIT_DEFAULT),
    )


def _user_caps(user: UserOut) -> tuple[int, int | None]:
    """(daily, monthly) caps for a logged-in user. Paid has no monthly cap."""
    if user.tier == "paid":
        return _int_env("CHAT_PAID_DAILY_LIMIT", _CHAT_PAID_DAILY_LIMIT_DEFAULT), None
    return (
        _int_env("CHAT_FREE_DAILY_LIMIT", _CHAT_FREE_DAILY_LIMIT_DEFAULT),
        _int_env("CHAT_FREE_MONTHLY_LIMIT", _CHAT_FREE_MONTHLY_LIMIT_DEFAULT),
    )


def _today_bucket() -> str:
    """UTC day key for the quota window (e.g. ``20260614``)."""
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def _month_bucket() -> str:
    """UTC month key for the quota window (e.g. ``202606``)."""
    return datetime.now(timezone.utc).strftime("%Y%m")


def _hash_ip(ip: str) -> str:
    """Stable, non-reversible key for an anon caller. Salted so the stored
    counters aren't a plain list of visitor IPs."""
    salt = os.environ.get("QUOTA_IP_SALT", "auctionscope-quota")
    return hashlib.sha256(f"{salt}:{ip}".encode()).hexdigest()


async def _enforce_anon_chat_quota(request: Request) -> None:
    """Durable per-IP day + month cap for anonymous callers. One atomic Cypher
    bump per turn (correct under concurrency, survives restarts); the UTC
    buckets reset their windows implicitly. Fails open on a Neo4j hiccup so a
    DB blip can't take chat down — the prepaid OpenRouter key is the hard
    backstop either way."""
    if _ratelimit_disabled():
        return
    ip = request.client.host if request.client else "unknown"
    try:
        counts = await auth_repo.bump_anon_quota(
            _hash_ip(ip), _today_bucket(), _month_bucket()
        )
    except Exception:  # noqa: BLE001 - availability over strict enforcement
        logger.exception("anon chat quota check failed — failing open")
        return
    daily, monthly = _anon_caps()
    if counts["day"] > daily:
        raise HTTPException(
            status_code=429,
            detail="daily chat limit reached — log in for more, or try again tomorrow",
        )
    if counts["month"] > monthly:
        raise HTTPException(
            status_code=429,
            detail="monthly chat limit reached — log in for a higher limit",
        )


async def _enforce_user_chat_quota(user: UserOut) -> None:
    """Durable, tier-aware day + month cap, keyed by account (IP-independent).
    One atomic Cypher bump per turn, correct under concurrent tabs and durable
    across restarts. Counts attempts, and fails open on a Neo4j hiccup so a DB
    blip can't take chat down."""
    if _ratelimit_disabled():
        return
    try:
        counts = await auth_repo.bump_chat_quota(
            user.id, _today_bucket(), _month_bucket()
        )
    except Exception:  # noqa: BLE001 - availability over strict enforcement
        logger.exception("chat quota check failed for user=%s — failing open", user.id)
        return
    if counts is None:
        logger.warning("chat quota: no :User row for %s — failing open", user.id)
        return
    daily, monthly = _user_caps(user)
    if counts["day"] > daily:
        detail = (
            "daily chat limit reached — try again tomorrow"
            if user.tier == "paid"
            else "daily chat limit reached — upgrade for more, or try again tomorrow"
        )
        raise HTTPException(status_code=429, detail=detail)
    if monthly is not None and counts["month"] > monthly:
        raise HTTPException(
            status_code=429,
            detail="monthly chat limit reached — upgrade for more, or try again next month",
        )


class ChatRequest(BaseModel):
    message: str
    message_history: list[dict[str, Any]] | None = None
    mode: str | None = None
    # Filters the client wants the agent to scope to on the next turn — e.g.
    # the browse panel's current selection. History-extracted filters layer
    # on top so the rolling-scope behavior across turns still works.
    active_filters: dict[str, Any] | None = None
    # auction_ids currently shown in the UI matches panel, in display order.
    # The panel can be populated outside the chat (browse filters, a restored
    # conversation, an earlier turn whose rows were trimmed from history), so
    # without this a "compare these / the matches" turn had no ids to resolve.
    # Kept separate from `active_filters` (filters are scope; these are a
    # concrete selection). Cleaned + bounded by `_clean_panel_ids`.
    panel_auction_ids: list[str] | None = None
    # User-selectable model ("flash"/"pro") and reasoning effort
    # ("off"/"medium"/"high"/…). Advisory only — both are resolved + gated
    # server-side in `_prepare_turn` (free/anon users are forced onto Flash),
    # so a tampered client can't unlock the pricey model or an unknown effort.
    # `None` = use the tier default (Flash for free, Pro for paid) / server
    # default effort.
    model: str | None = None
    reasoning_effort: str | None = None


# Whitelist of modes the agent will overlay. Each maps to a modes/<id>.md file.
# Keeping this explicit prevents arbitrary file reads from the modes/ dir.
# `compare` and `report` are parked in modes/_archive/ (2026-07) — an unknown
# mode from a stale client falls back to plain ask, so removal is graceful.
# Example ids use the REAL format (plain 6-digit strings like "750879") —
# the old "AUC-12345" chips taught users an id shape that can't exist, so
# copying them seeded guaranteed not-found lookups.
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
        "description": "Full due-diligence workflow on one auction_id.",
        "examples": [
            "Deep research on auction 750879",
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


def _clean_panel_ids(raw: Any) -> list[str] | None:
    """Sanitize client-supplied `panel_auction_ids` into a bounded, ordered,
    de-duplicated list of non-empty id strings (or None when there's nothing
    usable). Order is preserved (it's the panel's display/ranked order, which
    the model is told to respect); the first occurrence of a duplicate wins.
    Caps at `_MAX_PANEL_IDS` so a huge selection can't bloat the prompt.
    """
    if not isinstance(raw, list):
        return None
    seen: set[str] = set()
    out: list[str] = []
    for item in raw:
        if item is None:
            continue
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        out.append(s)
        if len(out) >= _MAX_PANEL_IDS:
            break
    return out or None


def _split_ui_rows(result: Any) -> tuple[Any, list[dict[str, Any]] | None]:
    """Pop the UI-only overflow from a search-tool result.

    The agent now moves `_ui_results` onto ToolReturn metadata before the
    model ever sees it (see `_split_ui_overflow` in api/agent.py), so new
    runs hit the metadata path in `_extract_artifacts` instead. This
    content-side split is kept for stored histories from before that change,
    which may still carry `_ui_results` inside the tool-return content. We
    copy the dict (so we don't mutate the in-memory ToolReturnPart content),
    pop `_ui_results`, and return the trimmed copy for the LLM + the raw
    list for the UI.
    """
    if not isinstance(result, dict) or "_ui_results" not in result:
        return result, None
    trimmed = {k: v for k, v in result.items() if k != "_ui_results"}
    ui_rows = result.get("_ui_results")
    if not isinstance(ui_rows, list):
        ui_rows = None
    return trimmed, ui_rows


def _metadata_ui_rows(part: Any) -> list[dict[str, Any]] | None:
    """UI overflow rows riding on ToolReturnPart.metadata (never model-visible)."""
    meta = getattr(part, "metadata", None)
    if isinstance(meta, dict):
        rows = meta.get("ui_rows")
        if isinstance(rows, list):
            return rows
    return None


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
                    if ui_rows is None:
                        ui_rows = _metadata_ui_rows(part)
                    artifacts.append(ToolArtifact(
                        tool=part.tool_name,
                        args=args,
                        result=result,
                        ui_rows=ui_rows,
                    ))
    return artifacts


def _strip_ui_rows_from_history(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove UI overflow rows from a dumped message history: `_ui_results`
    inside tool-return content (pre-metadata stored histories) and the
    `ui_rows` metadata the agent now attaches instead. Metadata is never
    sent to the model, but it would bloat the history payload the client
    stores and echoes back on every turn.
    """
    for msg in history:
        for part in msg.get("parts", []):
            if part.get("part_kind") != "tool-return":
                continue
            content = part.get("content")
            if isinstance(content, dict) and "_ui_results" in content:
                content.pop("_ui_results", None)
            metadata = part.get("metadata")
            if isinstance(metadata, dict) and "ui_rows" in metadata:
                metadata.pop("ui_rows", None)
                if not metadata:
                    part["metadata"] = None
    return history


# ── old tool-result trimming ───────────────────────────────────────────────
# Once the model has read a heavy tool result and written its prose answer,
# the raw rows rarely need to re-enter context on later turns — the agent can
# always re-query the graph. We keep the most recent turns fully detailed and
# squeeze older, heavy tool returns down to a breadcrumb stub, shrinking the
# history the client echoes back into the LLM on the next /chat turn. This is
# the same philosophy as `_strip_ui_rows_from_history`, applied to the rows the
# model *did* see (but only on stale turns).
# Default 1 = keep only the current turn's rows full; older turns collapse to a
# breadcrumb stub (auction_ids + counts) the model can re-query. Holding two
# full turns re-billed the prior turn's heavy rows on every follow-up call;
# bump via env if a flow genuinely needs the prior turn's rows verbatim.
_HISTORY_KEEP_FULL_TURNS = max(1, int(os.getenv("CHAT_HISTORY_KEEP_FULL_TURNS", "1")))
# Only trim tool returns whose JSON is at least this many chars — leaves small
# aggregate/stat results (distributions) untouched so the
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
            # Never touch pydantic-ai's capability-loading returns: the
            # load_capability result IS the loaded capability's instructions,
            # and the call/return pair is how the framework reconstructs
            # which deferred capabilities are open when a stored conversation
            # resumes (see api/agent.py).
            if part.get("tool_name") in ("load_capability", "search_tools"):
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
        u = result.usage
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


# Live starter chips for the chat landing (see api/chat/suggestions.py). Built
# from single-dimension `search_auctions(group_by=...)` distributions — which
# are future-only by default, so a chip's count matches what a click returns
# and no chip can dead-end. Cached in-process for _SUGGESTIONS_TTL_SECONDS: the
# buckets only move when the loader ingests/expires auctions, so an hourly
# refresh is plenty (same rationale as graph_property_count_async's cache) and
# keeps the landing off the graph on every page load.
_SUGGESTIONS_CACHE: dict[str, tuple[float, list[dict]]] = {}
_SUGGESTIONS_TTL_SECONDS = 3600.0
# One distribution query each; order here doesn't matter (the pure builder's
# _PICK_ORDER decides the chip mix), but keep it to dimensions the builder
# knows how to phrase.
_SUGGESTION_DIMS = ("city", "property_type", "asset_category", "area")


def _load_suggestion_distributions() -> dict[str, list[dict]]:
    """Pull each dimension's live distribution. A single dimension's failure
    degrades to fewer chips rather than none; a full outage returns {} and the
    endpoint serves the last good set (or lets the UI keep its fallback)."""
    out: dict[str, list[dict]] = {}
    for dim in _SUGGESTION_DIMS:
        try:
            res = cypher_T.search_auctions(group_by=dim)
        except Exception:  # noqa: BLE001 - one bad dim must not sink the rest
            logger.exception("suggestions: distribution query failed for %r", dim)
            continue
        dist = res.get("distribution") if isinstance(res, dict) else None
        if isinstance(dist, list) and dist:
            out[dim] = dist
    return out


@router.get("/suggestions")
def list_suggestions() -> dict:
    """Data-driven starter chips for the chat landing, cached hourly. Sync (runs
    in FastAPI's threadpool) because the underlying graph reads are blocking.
    Best-effort throughout: on a cold cache + DB hiccup it returns an empty list
    and the web UI keeps its hardcoded fallback chips."""
    now = time.time()
    cached = _SUGGESTIONS_CACHE.get("default")
    if cached and (now - cached[0]) < _SUGGESTIONS_TTL_SECONDS:
        return {"suggestions": cached[1]}
    chips = build_suggestions(_load_suggestion_distributions())
    if chips:
        _SUGGESTIONS_CACHE["default"] = (now, chips)
    elif cached:
        # Refresh hit an empty/failed read — serve the last good set rather
        # than blanking the landing.
        return {"suggestions": cached[1]}
    return {"suggestions": chips}


@router.get("/chat/models")
def list_chat_models(user: UserOut | None = Depends(get_optional_user)) -> dict:
    """Model + reasoning-effort options for the chat toggles, tier-aware.

    Each model carries `locked`: true when the caller's tier can't use it
    (free/anon → Pro is locked). The UI should render locked models disabled
    with an upgrade prompt; the server enforces the same rule on /chat
    regardless, so this is purely to drive the toggle UI. `defaults` is what
    the server will use when the client sends no explicit choice.
    """
    tier = user.tier if user else "free"
    is_paid = tier == "paid"
    models = [
        {**opt, "locked": opt["min_tier"] == "paid" and not is_paid}
        for opt in CHAT_MODEL_OPTIONS
    ]
    # Efforts above the free-tier ceiling are clamped server-side, so render
    # them locked for free/anon — picking them would silently do nothing.
    # At/below the ceiling (notably Off) stays available to everyone.
    efforts = [
        {
            **opt,
            "locked": not is_paid
            and EFFORT_RANK[opt["id"]] > EFFORT_RANK[FREE_TIER_EFFORT],
        }
        for opt in REASONING_EFFORT_OPTIONS
    ]
    return {
        "tier": tier,
        "models": models,
        "reasoning_efforts": efforts,
        "defaults": {
            "model": DEFAULT_PAID_MODEL if is_paid else FREE_TIER_MODEL,
            # Free/anon effort is capped server-side; report the real value so
            # the toggle UI doesn't promise reasoning levels they won't get.
            "reasoning_effort": "high" if is_paid else FREE_TIER_EFFORT,
        },
    }


# Covers both usage ceilings in `_usage_limits()` — too many round-trips
# (`request_limit`) and too much accumulated context (`input_tokens_limit`).
# Deliberately vague about which one tripped: the user's remedy is the same
# either way, and naming the limit invites "raise it" rather than "narrow it".
_USAGE_LIMIT_DETAIL = (
    "this question grew too large to answer in one go — try asking it more narrowly"
)


async def _prepare_turn(
    request: Request, req: ChatRequest, user: UserOut | None
) -> tuple[list | None, ChatDeps, str | None, dict]:
    """Shared per-turn gating + setup for /chat and /chat/stream.

    Raises HTTPException for auth/rate failures, validates the incoming
    history, builds ChatDeps with the rolling search scope, and resolves the
    per-request model + reasoning effort (gating free/anon users to Flash).

    Returns (history, deps, mode, run_ctx). `run_ctx` carries the
    `agent.run` overrides under "overrides" plus the resolved "model" /
    "reasoning_effort" names for the obs log.
    """
    mode = req.mode
    if mode in _GATED_MODES and (user is None or not user.email_verified):
        raise HTTPException(status_code=401, detail="login required for this mode")
    if user is None:
        await _enforce_anon_chat_quota(request)
    else:
        await _enforce_user_chat_quota(user)
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
        supabase_id=user.id if user else None,
        panel_auction_ids=_clean_panel_ids(req.panel_auction_ids),
    )
    # Model + effort gating. Anonymous callers (user is None) and the free tier
    # are locked to Flash; only paid users can opt into Pro. Reasoning effort is
    # likewise clamped for free/anon (reasoning tokens bill as output), so the
    # client toggle only takes effect for paid users.
    tier = user.tier if user else "free"
    model_name = resolve_chat_model(tier, req.model)
    effort = resolve_reasoning_effort(req.reasoning_effort, tier)
    run_ctx = {
        "overrides": build_chat_run_overrides(model_name, effort),
        "model": model_name,
        "reasoning_effort": effort,
    }
    return history, deps, mode, run_ctx


def _tool_returns(messages) -> list[tuple[str, Any]]:
    """(tool_name, content) for every ToolReturnPart, in order — the input
    shape api.chat.panel works over."""
    out: list[tuple[str, Any]] = []
    for msg in messages:
        for part in getattr(msg, "parts", []):
            if isinstance(part, ToolReturnPart):
                out.append((part.tool_name, part.content))
    return out


async def _synthesize_panel_artifact(
    result: Any, deps: ChatDeps | None
) -> ToolArtifact | None:
    """Programmatic matches-panel sync (replaces the select_properties TOOL).

    The model cites auction_ids in its answer (role rule 1); the system
    extracts them, and when they re-present a subset/re-ranking, fetches
    fresh rows and appends a synthetic select_properties artifact — the
    frontend renders the last search-shaped artifact, so it needs no
    changes. One Neo4j query, zero LLM round-trips. Best-effort: a failure
    here must never fail the turn."""
    try:
        ids = panel_sync_ids(
            result.output or "",
            _tool_returns(result.new_messages()),
            _tool_returns(result.all_messages()),
            deps.panel_auction_ids if deps else None,
        )
        if not ids:
            return None
        rows = await asyncio.to_thread(cypher_T.get_auctions_by_ids, ids)
        return ToolArtifact(
            tool="select_properties",
            args={"auction_ids": ids, "synthetic": True},
            result=rows,
        )
    except Exception:  # noqa: BLE001 - panel sync is cosmetic, never fatal
        logger.exception("panel sync failed — leaving panel as-is")
        return None


async def _build_chat_response(
    result: Any, mode: str | None, usage: dict[str, Any],
    model: str | None = None, deps: ChatDeps | None = None,
) -> ChatResponse:
    """Post-run packaging shared by /chat and /chat/stream."""
    artifacts = _extract_artifacts(result.new_messages())
    panel_artifact = await _synthesize_panel_artifact(result, deps)
    if panel_artifact is not None:
        artifacts.append(panel_artifact)
    logger.info(
        "chat turn ok mode=%s model=%s tool_calls=%d answer_chars=%d "
        "llm_calls=%s in_tok=%s cached_tok=%s out_tok=%s",
        mode or "ask", model or DEFAULT_PAID_MODEL, len(artifacts), len(result.output or ""),
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


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: Request,
    req: ChatRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> ChatResponse:
    history, deps, mode, run_ctx = await _prepare_turn(request, req, user)
    try:
        # Time the LLM round-trip on its own budget so slow-agent turns are
        # easy to spot in logs (auction.obs chat.agent_run ... elapsed_ms=...).
        with timed("chat.agent_run", slow_ms=SLOW_AGENT_MS,
                   mode=mode or "ask", history_msgs=len(history) if history else 0,
                   model=run_ctx["model"],
                   reasoning_effort=run_ctx["reasoning_effort"] or "default") as obs:
            result = await agent.run(
                req.message, message_history=history, deps=deps,
                usage_limits=_usage_limits(), **run_ctx["overrides"],
            )
            # Attach token/cache counts to the obs line so a turn's cost — and
            # whether the stable prefix is hitting the implicit prompt cache —
            # is greppable next to its latency.
            usage = _usage_fields(result)
            obs.update(usage)
    except UsageLimitExceeded:
        # The per-run request ceiling tripped — almost always a tool loop, not
        # a transient fault, so 422 (don't invite a blind retry of the same
        # question the way the 502/504 paths do).
        logger.exception("agent.run hit usage limit for message=%r mode=%r", req.message, mode)
        raise HTTPException(status_code=422, detail=_USAGE_LIMIT_DETAIL)
    except (httpx.TimeoutException, TimeoutError):
        # Upstream LLM hung — retryable, so tell the client with a 504.
        logger.exception("agent.run timed out for message=%r mode=%r", req.message, mode)
        raise HTTPException(status_code=504, detail="the model took too long — please retry")
    except Exception:
        # Most often pydantic-ai's UnexpectedModelBehavior or a transient
        # OpenRouter error. Log with the user message so the failing input is
        # recoverable from Render logs, then surface a friendly 502 (retryable
        # upstream failure, not a bug in this service).
        logger.exception("agent.run failed for message=%r mode=%r", req.message, mode)
        raise HTTPException(status_code=502, detail="chat agent failed — please retry")
    return await _build_chat_response(result, mode, usage, run_ctx["model"], deps)


def _sse(event: str, data: dict[str, Any]) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _stream_turn(
    message: str, history: list | None, deps: ChatDeps, mode: str | None,
    run_ctx: dict,
) -> AsyncIterator[str]:
    """Run the agent and yield SSE frames.

    Event protocol (each frame is `event: <name>` + JSON `data:`):
      status — {label}: a tool started (or the model is reasoning); the UI
               swaps the "thinking" indicator text.
      delta  — {text}: a chunk of the FINAL answer. Text the model emits on
               intermediate turns (before tool calls) is suppressed so
               scratch prose never leaks into the streamed reply.
      final  — the full ChatResponse payload (answer/artifacts/history),
               identical to what blocking /chat returns.
      error  — {detail}: terminal failure. HTTP status is already 200 by the
               time the agent fails, so errors ride in-band; the client maps
               them onto the same messaging as /chat's 4xx/5xx paths.
    """
    try:
        with timed("chat.agent_run", slow_ms=SLOW_AGENT_MS,
                   mode=mode or "ask", history_msgs=len(history) if history else 0,
                   model=run_ctx["model"],
                   reasoning_effort=run_ctx["reasoning_effort"] or "default") as obs:
            result = None
            # Text parts arriving before FinalResultEvent may be scratch
            # ("let me search…") — buffer the current part and only flush
            # once pydantic-ai marks the run's output as started.
            final_started = False
            pending_text = ""
            async with agent.run_stream_events(
                message, message_history=history, deps=deps,
                usage_limits=_usage_limits(), **run_ctx["overrides"],
            ) as stream:
                async for event in stream:
                    if isinstance(event, AgentRunResultEvent):
                        result = event.result
                    elif isinstance(event, FunctionToolCallEvent):
                        yield _sse("status", {"label": _tool_status(event.part)})
                    elif isinstance(event, FinalResultEvent):
                        final_started = True
                        if pending_text:
                            yield _sse("delta", {"text": pending_text})
                            pending_text = ""
                    elif isinstance(event, PartStartEvent):
                        if isinstance(event.part, ThinkingPart):
                            yield _sse("status", {"label": "Reasoning…"})
                        elif isinstance(event.part, TextPart):
                            if final_started:
                                if event.part.content:
                                    yield _sse("delta", {"text": event.part.content})
                            else:
                                pending_text = event.part.content or ""
                    elif isinstance(event, PartDeltaEvent) and isinstance(
                        event.delta, TextPartDelta
                    ):
                        if final_started:
                            if event.delta.content_delta:
                                yield _sse("delta", {"text": event.delta.content_delta})
                        else:
                            pending_text += event.delta.content_delta
            if result is None:  # defensive: stream ended without a result event
                raise RuntimeError("agent stream ended without a result")
            usage = _usage_fields(result)
            obs.update(usage)
    except UsageLimitExceeded:
        logger.exception("agent stream hit usage limit for message=%r mode=%r", message, mode)
        yield _sse("error", {"detail": _USAGE_LIMIT_DETAIL})
        return
    except (httpx.TimeoutException, TimeoutError):
        logger.exception("agent stream timed out for message=%r mode=%r", message, mode)
        yield _sse("error", {"detail": "the model took too long — please retry"})
        return
    except Exception:
        logger.exception("agent stream failed for message=%r mode=%r", message, mode)
        yield _sse("error", {"detail": "chat agent failed — please retry"})
        return
    final = await _build_chat_response(result, mode, usage, run_ctx["model"], deps)
    yield _sse("final", final.model_dump(mode="json"))


@router.post("/chat/stream")
async def chat_stream(
    request: Request,
    req: ChatRequest,
    user: UserOut | None = Depends(get_optional_user),
) -> StreamingResponse:
    """Streaming twin of /chat. Same request body, same gating; answers as
    Server-Sent Events so the UI can show tool progress and stream the reply
    instead of spinning silently through a long reasoning turn."""
    # Gating runs BEFORE the response starts so auth/rate failures still
    # surface as real HTTP statuses (401/429), matching blocking /chat.
    history, deps, mode, run_ctx = await _prepare_turn(request, req, user)
    return StreamingResponse(
        _stream_turn(req.message, history, deps, mode, run_ctx),
        media_type="text/event-stream",
        headers={
            # SSE must not be buffered: X-Accel-Buffering for nginx-style
            # proxies (Render), no-cache for anything else in the path.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
