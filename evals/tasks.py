"""
evals/tasks.py
--------------
The eval "task" functions — one question (or one scripted conversation) run
through a real agent, reduced to the output shapes the evaluators score.

There are two agents now, so there are two of each binding, selected by
``EVAL_AGENT``:

    EVAL_AGENT=v1   (default)  /chat — the pydantic-ai ReAct agent
    EVAL_AGENT=v2              /chat/v2 — the tiered loop

Nothing else about the evals changes. `evals/cases.py`, `evals/dataset.py`,
`evals/conversations.py` and both evaluator modules are agent-agnostic, so
all 68 golden cases, all 8 conversations and all ten evaluators score v2 with
**exactly** the assertions they apply to v1. That is the point: a migration
gate is only meaningful if both sides are measured by the same ruler.

Both bindings compute surfaced / cited ids and panel state with the real
helpers in `api/chat/panel.py`, never a re-implementation — panel desync is
precisely the class of bug the conversation evaluators exist to catch, and an
eval that models the panel differently from production cannot catch it.
"""
from __future__ import annotations

import os
from typing import Any

from evals.conversation_evaluators import ConversationOutput, TurnOutput
from evals.dataset import ChatTaskOutput

#: Logical chat model to eval ("flash"/"pro"). Flash is both the cheaper eval
#: and the harder tool-routing bar.
CHAT_MODEL = os.getenv("EVAL_CHAT_MODEL", "flash")


#: The cost keys both agents report, so the two runs line up column for column.
USAGE_KEYS = ("llm_calls", "input_tokens", "cached_tokens", "output_tokens",
              "seconds")


def _sum_usage(per_turn: list[dict]) -> dict:
    """Add up per-turn usage for a whole conversation."""
    total: dict = {}
    for u in per_turn:
        for k in USAGE_KEYS:
            v = u.get(k)
            if isinstance(v, (int, float)):
                total[k] = round(total.get(k, 0) + v, 2)
    return total


def _v2_usage(result) -> dict:
    """Map a v2 `TurnResult` onto the shared key names.

    Reads defensively, matching `_usage_fields` on the v1 side: cost is
    telemetry, and a renamed field should degrade to "no data" rather than
    fail a correctness run that is otherwise fine.
    """
    fields = {
        "llm_calls": "model_calls",
        "input_tokens": "input_tokens",
        "cached_tokens": "cached_tokens",
        "output_tokens": "output_tokens",
        "seconds": "seconds",
    }
    out = {}
    for key, attr in fields.items():
        value = getattr(result, attr, None)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            out[key] = value
    return out


def agent_id() -> str:
    value = (os.getenv("EVAL_AGENT") or "v1").strip().lower()
    return value if value in {"v1", "v2"} else "v1"


# ── v1: the pydantic-ai ReAct agent ─────────────────────────────────────────

async def golden_v1(question: str) -> ChatTaskOutput:
    """Run one question through /chat's agent and capture the answer, the
    tools it called, and the auction_ids it surfaced/cited."""
    from pydantic_ai.messages import ToolReturnPart

    from api.agent import ChatDeps, agent, build_chat_run_overrides
    from api.chat.panel import cited_ids, known_auction_ids
    from api.chat.router import _usage_fields

    result = await agent.run(
        question, deps=ChatDeps(), **build_chat_run_overrides(CHAT_MODEL, None)
    )
    seen: set[str] = set()
    tools: list[str] = []
    tool_returns: list[tuple[str, object]] = []
    for msg in result.all_messages():
        for part in getattr(msg, "parts", []):
            name = getattr(part, "tool_name", None)
            if name and name not in seen:
                seen.add(name)
                tools.append(name)
            if isinstance(part, ToolReturnPart):
                tool_returns.append((part.tool_name, part.content))
    answer = result.output or ""
    surfaced = known_auction_ids(tool_returns)
    return ChatTaskOutput(
        answer=answer,
        tools_called=tools,
        surfaced_auction_ids=sorted(surfaced),
        cited_auction_ids=cited_ids(answer, surfaced),
        # Reuse the router's extractor rather than re-deriving: it is already
        # defensive about pydantic-ai field renames, degrading to {} instead
        # of raising, which is exactly what an eval wants.
        usage=_usage_fields(result),
    )


async def conversation_v1(convo) -> ConversationOutput:
    """Play one scripted conversation through /chat's agent, threading the
    message history and the panel exactly as the router does."""
    from pydantic_ai.messages import ToolCallPart

    from api.agent import ChatDeps, agent, build_chat_run_overrides
    from api.chat.panel import panel_sync_ids, turn_panel_ids
    from api.chat.router import _extract_active_filters, _tool_returns, _usage_fields

    overrides = build_chat_run_overrides(CHAT_MODEL, None)
    per_turn_usage: list[dict] = []
    history = None
    active_filters: dict = {}
    last_total: int | None = None
    panel: list[str] = []
    turns_out: list[TurnOutput] = []

    for turn in convo.turns:
        deps = ChatDeps(
            active_filters=active_filters or None,
            last_total_count=last_total,
            panel_auction_ids=panel or None,
        )
        result = await agent.run(
            turn.message, message_history=history, deps=deps, **overrides
        )
        history = result.all_messages()
        per_turn_usage.append(_usage_fields(result))
        active_filters, last_total = _extract_active_filters(history)

        seen: set[str] = set()
        tools: list[str] = []
        calls: list[dict] = []
        for msg in result.new_messages():
            for part in getattr(msg, "parts", []):
                if isinstance(part, ToolCallPart):
                    try:
                        args = part.args_as_dict()
                    except Exception:  # noqa: BLE001 - malformed args -> empty
                        args = {}
                    calls.append({"tool": part.tool_name, "args": args})
                    if part.tool_name not in seen:
                        seen.add(part.tool_name)
                        tools.append(part.tool_name)

        answer = result.output or ""
        panel_before = list(panel)
        turn_returns = _tool_returns(result.new_messages())
        all_returns = _tool_returns(history)
        synced = panel_sync_ids(answer, turn_returns, all_returns, panel_before)
        panel = synced or turn_panel_ids(turn_returns) or panel_before

        turns_out.append(TurnOutput(
            message=turn.message,
            answer=answer,
            tools_called=tools,
            tool_calls=calls,
            total_count=last_total,
            active_filters=dict(active_filters),
            panel_ids_before=panel_before,
            panel_ids=list(panel),
        ))
    return ConversationOutput(turns=turns_out, usage=_sum_usage(per_turn_usage))


# ── v2: the tiered loop ─────────────────────────────────────────────────────

def _returns(executed) -> list[tuple[str, Any]]:
    """`[(tool_name, content)]` — the shape every panel helper takes. v2's
    executed calls already carry both, so no message-part walking is needed."""
    return [(call.tool, call.result) for call in executed]


def _tools_called(executed) -> list[str]:
    """Ordered and de-duplicated, matching what ToolTrajectory scores on v1."""
    seen: set[str] = set()
    out: list[str] = []
    for call in executed:
        if call.tool not in seen:
            seen.add(call.tool)
            out.append(call.tool)
    return out


async def golden_v2(question: str) -> ChatTaskOutput:
    from api.chat.panel import cited_ids, known_auction_ids
    from api.chat.v2.loop import run_turn

    result = await run_turn(question, model_name=CHAT_MODEL)
    answer = result.answer or ""
    surfaced = known_auction_ids(_returns(result.executed))
    return ChatTaskOutput(
        answer=answer,
        tools_called=_tools_called(result.executed),
        surfaced_auction_ids=sorted(surfaced),
        cited_auction_ids=cited_ids(answer, surfaced),
        usage=_v2_usage(result),
    )


async def conversation_v2(convo) -> ConversationOutput:
    """Play one scripted conversation through the tiered loop.

    The scope object is threaded turn to turn in place of a message history —
    that substitution IS the v2 design, so this is the binding that proves it
    holds up over a real narrowing conversation rather than a single question.
    """
    from api.chat.panel import panel_sync_ids, turn_panel_ids
    from api.chat.v2.loop import run_turn

    scope: dict = {}
    last_total: int | None = None
    last_ids: list[str] = []
    panel: list[str] = []
    all_returns: list[tuple[str, Any]] = []
    turns_out: list[TurnOutput] = []
    per_turn_usage: list[dict] = []

    for turn in convo.turns:
        result = await run_turn(
            turn.message,
            scope=scope,
            last_ids=last_ids,
            last_total_count=last_total,
            model_name=CHAT_MODEL,
        )
        per_turn_usage.append(_v2_usage(result))
        scope = result.filters
        last_total = result.last_total_count
        last_ids = result.last_ids

        answer = result.answer or ""
        panel_before = list(panel)
        turn_returns = _returns(result.executed)
        all_returns.extend(turn_returns)
        synced = panel_sync_ids(answer, turn_returns, all_returns, panel_before)
        panel = synced or turn_panel_ids(turn_returns) or panel_before

        turns_out.append(TurnOutput(
            message=turn.message,
            answer=answer,
            tools_called=_tools_called(result.executed),
            tool_calls=[{"tool": c.tool, "args": c.args} for c in result.executed],
            total_count=last_total,
            active_filters=dict(scope),
            panel_ids_before=panel_before,
            panel_ids=list(panel),
        ))
    return ConversationOutput(turns=turns_out, usage=_sum_usage(per_turn_usage))


# ── selection ───────────────────────────────────────────────────────────────

_GOLDEN = {"v1": golden_v1, "v2": golden_v2}
_CONVERSATION = {"v1": conversation_v1, "v2": conversation_v2}


def golden_task():
    return _GOLDEN[agent_id()]


def conversation_task():
    return _CONVERSATION[agent_id()]
