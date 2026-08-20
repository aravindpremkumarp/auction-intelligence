"""
api/chat/deep/agent.py
----------------------
Builds the Deep Agents agent for one turn.

Everything that is not the loop is **shared with `/chat/v2` by import, never
by copy**: the same six tool implementations, the same `modes/_shared.md`
domain brief, the same `api/policy.py::SHARED_POLICY`, the same OpenRouter
model settings. That is what makes the A/B honest — if the deep loop wins or
loses on the golden catalogue, the loop shape is the only thing that could
have caused it.

Three harness facts this file exists to handle, all found by the spike
(`experiments/deepagent-chat/README.md`) and all still true in deepagents
0.7.7:

1. **The tool node re-raises tool exceptions**, killing the whole turn. The
   spike hit it with an invalid `aggregate_field`. The production tools are
   already wrapped in `@model_visible_errors`, which turns a ValueError into
   `{"error": ...}` the model can read and correct — so the fix is inherited
   rather than rebuilt, and `test_tool_errors_come_back_as_data` pins it.

2. **`_ui_results` must never reach the transcript.** The tools attach up to
   500 full rows for the matches panel. In the tiered loop the executor
   splits them out; here the tool return goes straight into a `ToolMessage`
   that is then checkpointed and re-sent on every later turn, so an unsplit
   payload would be re-billed for the rest of the conversation. `_bind_tools`
   strips them into a per-turn sink.

3. **`TodoListMiddleware` and the filesystem/subagent middleware are off.**
   `create_deep_agent`'s defaults are built for long autonomous coding runs.
   A chat turn that has to answer in seconds cannot afford a planning call
   that writes a todo list first, and there is no filesystem to give it.
   `subagents=[]` keeps the same discipline `v2/agents.py::_assert_no_todo_list`
   enforces on the tiers.
"""
from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

#: Hard ceiling on graph supersteps. A ReAct loop's failure mode is looping,
#: and the spike measured one multi-hop question at 9 model calls; 24 leaves
#: room for a genuinely hard question while still terminating. Matches the
#: `recursion_limit` the spike used so the A/B is comparable.
RECURSION_LIMIT = 24

#: Ceiling on model calls in one turn, enforced by middleware inside the
#: graph. The tiered loop's equivalent is `TurnBudget.max_model_calls`.
MAX_MODEL_CALLS = 12


@dataclass
class ToolSink:
    """Per-turn record of what the graph actually called.

    The tiered loop gets this from its executor. Here the calls happen inside
    the graph, so the tool wrappers write to a sink bound in at build time.
    Bound per turn rather than held in a ContextVar because LangChain may run
    a sync tool on a worker thread, and context propagation across that
    boundary is a version-dependent detail this should not rest on.
    """

    calls: list[Any] = field(default_factory=list)


def _bind_tools(sink: ToolSink) -> list[Callable]:
    """The v2 tool surface, wrapped to record calls and strip UI overflow."""
    from api.chat.v2.executor import ExecutedCall, _split_ui_rows  # noqa: PLC2701
    from api.chat.v2.tools import PLANNER_TOOLS

    bound: list[Callable] = []
    for name, fn in PLANNER_TOOLS.items():
        bound.append(_wrap(name, fn, sink, ExecutedCall, _split_ui_rows))
    return bound


def _wrap(name: str, fn: Callable, sink: ToolSink, ExecutedCall, split):
    @functools.wraps(fn)
    def wrapped(**kwargs):
        started = time.perf_counter()
        try:
            raw = fn(**kwargs)
        except Exception as exc:  # noqa: BLE001
            # `model_visible_errors` already converts ValueError/TypeError.
            # Anything reaching here is a real fault; the harness would
            # re-raise and kill the turn, so it becomes data instead and the
            # model gets to write around it.
            logger.exception("chat deep: tool %s failed", name)
            payload = {"error": f"{name} failed: {exc}"}
            sink.calls.append(ExecutedCall(
                tool=name, args=dict(kwargs), result=payload,
                ms=int((time.perf_counter() - started) * 1000),
                error=str(exc),
            ))
            return payload

        model_visible, ui_rows = split(raw)
        sink.calls.append(ExecutedCall(
            tool=name, args=dict(kwargs), result=model_visible,
            ms=int((time.perf_counter() - started) * 1000),
            ui_rows=ui_rows,
        ))
        return model_visible

    return wrapped


def system_prompt() -> str:
    """The ReAct-loop instructions, over the shared brief and policy.

    Note what is NOT shadowed here. `v2/prompts.py` deliberately overrides
    `_shared.md`'s loop-discipline rules ("batch independent tool calls", "one
    search per question", "filter carry-over") because in the tiered loop
    those are structural — code does them. In a ReAct loop they are advice the
    model has to follow, which is exactly what `_shared.md` was written for,
    so it is left to stand.
    """
    from api.chat.v2 import prompts

    return DEEP_SYSTEM.format(
        shared=prompts.shared_context(),
        policy=prompts.SHARED_POLICY,
    )


DEEP_SYSTEM = """{shared}

---

{policy}

---

You are the assistant for a Tamil Nadu bank-auction search, working over a
Neo4j knowledge graph through the tools you have been given.

You hold the full conversation. Resolve "these", "those", "of them", "the
cheapest one", "these areas", "that bank" against what was actually said
earlier — the questions, your own answers, and the tool results behind them.
Never ask the user to restate something already in this conversation.

Carry filters forward yourself. If the user narrowed to Chennai three turns
ago and now says "under 40 lakhs", the search is Chennai AND under 40 lakhs.
When the subject changes entirely, drop the old filters rather than silently
answering about the previous city.

Batch independent tool calls in one step. Two searches that do not depend on
each other should be issued together, not one after the other.

Ground every claim in tool output and cite auction_ids. If the graph cannot
support the question — anything about price trends over time, appreciation,
growth, demand or future value — say plainly that the data is current auction
listings only, with no history to measure change from, then answer the
closest question it CAN support. Never present a listing count as a trend.

Answer in markdown. Be direct: lead with the answer, then the supporting
detail."""


def build_deep_agent(
    *,
    sink: ToolSink,
    model_name: str = "flash",
    reasoning_effort: str | None = None,
    checkpointer: Any = None,
):
    """One turn's agent. Built per turn so the tool sink can be bound in.

    Graph compilation is microseconds against a model call's seconds, so
    per-turn construction costs nothing measurable and buys a sink that needs
    no cross-thread context propagation.
    """
    from deepagents import create_deep_agent

    from api.chat.v2.agents import chat_model, model_middleware

    # NOT the tier default: a tier is one call by construction, a ReAct turn
    # is many. See `model_middleware`'s docstring.
    stack = model_middleware(run_limit=MAX_MODEL_CALLS)
    _assert_chat_shaped(stack)
    return create_deep_agent(
        model=chat_model(model_name, reasoning_effort),
        tools=_bind_tools(sink),
        system_prompt=system_prompt(),
        middleware=stack,
        # Off on purpose — see the module docstring.
        subagents=[],
        checkpointer=checkpointer,
    )


def _assert_chat_shaped(stack: list[Any]) -> None:
    """The chat loop must not inherit the long-run harness defaults.

    `v2/agents.py::_assert_no_todo_list` makes the same assertion for the
    tiers. Here it also guards the middleware `create_deep_agent` composes
    around what we pass: a dependency bump that starts injecting a todo list
    or a filesystem into every agent would add a model call to every chat
    turn, and that must fail a test rather than show up as a latency
    regression on the A/B.
    """
    names = {getattr(m, "name", type(m).__name__) for m in stack}
    forbidden = {"TodoListMiddleware", "FilesystemMiddleware", "MemoryMiddleware"}
    overlap = names & forbidden
    assert not overlap, (
        f"long-run harness middleware reached the chat loop: {sorted(overlap)}. "
        "These are built for autonomous coding runs and cost a model call per "
        "turn in a loop that has to answer in seconds."
    )
