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

3. **The harness tools are ON and cannot be switched off.** This was
   originally documented here as "the filesystem/subagent middleware are
   off" — that was **wrong**, and `test_the_bound_tool_surface_is_pinned`
   exists because of it. `create_deep_agent` adds `FilesystemMiddleware` and
   the general-purpose subagent **unconditionally**: they are in its
   protected scaffolding set, `subagents=[]` does not suppress the `task`
   tool, and a `HarnessProfile(excluded_tools=...)` registered against a
   pre-built `BaseChatModel` does not remove them from the tool node either
   (verified against 0.7.7). So the deep loop's real tool surface is our four
   graph tools PLUS `ls`, `read_file`, `write_file`, `edit_file`, `delete`,
   `glob`, `grep`, `execute` and `task`.

   **What that does and does not mean.** The default backend is
   `StateBackend` — an in-memory virtual filesystem living in graph state. It
   has no `execute` method and does not satisfy `SandboxBackendProtocol`, so
   the `execute` tool returns an error string rather than running a shell
   command: no shell, no real disk, no path out of the process.
   `test_execute_cannot_reach_a_shell` pins that, and it is the assertion
   that must fail loudly if a future version ever swaps the default backend
   for a sandbox one.

   The cost is real even though the risk is not: nine extra tool schemas ride
   in every prompt, which inflates the deep loop's input tokens against the
   tiered loop it is being measured against. `docs/chat-loop-ab-2026-08.md`
   says so, so the A/B is read with that handicap in view rather than as a
   clean comparison.
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
    _assert_no_todo_list(stack)
    bound = _bind_tools(sink)
    return create_deep_agent(
        model=chat_model(model_name, reasoning_effort),
        tools=bound,
        system_prompt=system_prompt(),
        middleware=stack,
        # A NAMED subagent rather than the anonymous general-purpose one.
        # The `task` tool ships either way (see the module docstring), so the
        # choice is not "subagents or no subagents" — it is whether the model
        # delegates to something with a brief and a bounded tool set, or to a
        # blank general-purpose worker holding the same tools as its parent.
        subagents=list(_subagents(bound)),
        interrupt_on=INTERRUPT_ON or None,
        checkpointer=checkpointer,
    )


#: Tools whose call should pause the graph for a human to approve, in the
#: `interrupt_on` shape `create_deep_agent` takes.
#:
#: Deliberately EMPTY, and that is the honest state rather than an oversight.
#: Human-in-the-loop earns its keep in front of an action that is expensive or
#: hard to undo, and this surface has none: every graph tool is read-only, and
#: the filesystem tools write to an in-memory `StateBackend` that is discarded
#: with the thread. Wiring the parameter now means the day a write tool does
#: land (a save, a bid, an alert) it is one entry here rather than a plumbing
#: change through the loop and the router.
INTERRUPT_ON: dict[str, Any] = {}

#: One line per subagent, kept next to the prompt it delegates with.
DOSSIER_SUBAGENT_PROMPT = """You investigate ONE auction property in depth and report back.

Pull the full record with get_auction_detail, including its price_history —
the re-auction timeline is usually the most informative field, because a
property that failed to sell twice is telling you something the headline
price is not.

Use internet_search ONLY for off-graph context: what the locality is like,
what a legal term in the notice means, relevant RBI or bank news. NEVER for
prices, counts, deadlines or auction_ids — those come from the graph or they
do not get said.

Report back as compact prose the calling agent can quote: what it is, what
the price history shows, what is unusual, and what a bidder should check
before committing. Do not pad. If a field is missing, say it is missing."""


def _subagents(bound_tools: list[Callable]) -> list[dict[str, Any]]:
    """The named subagents the `task` tool may delegate to.

    Only one today. It exists because `deep-research` — a full due-diligence
    pass on a single property — is a genuinely different job from answering a
    search question, and it is the one mode both `/chat/v2` and `/chat/deep`
    currently reject and leave on v1.

    It is handed the SAME sink-wrapped tool objects as its parent, so a
    subagent's graph queries land in `ToolSink.calls` like any other and the
    matches panel, the answer gate and the eval's tool trajectory all see
    them. A subagent with its own unwrapped tools would do real work that the
    turn's record could not account for.
    """
    by_name = {t.__name__: t for t in bound_tools}
    dossier_tools = [
        by_name[name]
        for name in ("get_auction_detail", "internet_search")
        if name in by_name
    ]
    return [
        {
            "name": "property-dossier",
            "description": (
                "Deep due diligence on ONE auction property: full record, "
                "price history, re-auction timeline, and off-graph context. "
                "Delegate here instead of chaining get_auction_detail calls "
                "yourself when the user asks for everything about a property."
            ),
            "system_prompt": DOSSIER_SUBAGENT_PROMPT,
            "tools": dossier_tools,
        }
    ]


def _assert_no_todo_list(stack: list[Any]) -> None:
    """`TodoListMiddleware` must never reach the chat loop.

    Narrower than it used to be, and deliberately so. This previously also
    asserted `FilesystemMiddleware` was absent — an assertion that passed only
    because it inspected the middleware WE pass, never the stack
    `create_deep_agent` assembles around it, where the filesystem middleware
    is unconditional. It therefore certified something false for the whole
    life of the PR. The real surface is now pinned by
    `test_the_bound_tool_surface_is_pinned`, which asserts on the compiled
    graph rather than on our input.

    What is left is the claim this function can actually make: nothing in the
    middleware we hand over adds a todo-list planning call to a turn that has
    to answer in seconds.
    """
    names = {getattr(m, "name", type(m).__name__) for m in stack}
    assert "TodoListMiddleware" not in names, (
        "TodoListMiddleware reached the chat loop — it costs a whole model "
        "call writing a plan before the turn starts."
    )
