"""
evals/tasks.py
--------------
The eval "task" functions — one question (or one scripted conversation) run
through a real agent, reduced to the output shapes the evaluators score.

The agent under test is `/chat/v2`, the tiered loop. The pydantic-ai agent
these evals were first written against (`/chat`, "v1") is retired, and the
`EVAL_AGENT` selector went with it. `evals/cases.py`, `evals/dataset.py`,
`evals/conversations.py` and both evaluator modules are agent-agnostic, so all
68 golden cases, all 8 conversations and all ten evaluators score v2 with the
assertions they used to apply to v1. The chat users talk to, agent3, has its
own tool catalogue in `evals/run_agent3.py`.

The bindings compute surfaced / cited ids and panel state with the real
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


#: The cost keys every run reports, so two runs line up column for column.
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

    Reads defensively: cost is telemetry, and a renamed field should degrade
    to "no data" rather than fail a correctness run that is otherwise fine.
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


# ── v2: the tiered loop ─────────────────────────────────────────────────────

def _returns(executed) -> list[tuple[str, Any]]:
    """`[(tool_name, content)]` — the shape every panel helper takes. v2's
    executed calls already carry both, so no message-part walking is needed."""
    return [(call.tool, call.result) for call in executed]


def _tools_called(executed) -> list[str]:
    """Ordered and de-duplicated — the list ToolTrajectory scores."""
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

def agent_id() -> str:
    """The agent under test, for run names and report headers."""
    return "v2"


def golden_task():
    return golden_v2


def conversation_task():
    return conversation_v2
