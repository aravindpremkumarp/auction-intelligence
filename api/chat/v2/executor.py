"""
api/chat/v2/executor.py
-----------------------
Runs a planned batch of tool calls concurrently and returns them as data.

This is where the tiered loop's latency win actually lands. v1 issues tool
calls one at a time because a ReAct loop can only decide the next call after
seeing the last result; the planner emits them all up front, so they run
together and the turn costs the slowest query, not their sum.

**Why a private thread pool.** The production tools in `api/tools/cypher_tools.py`
are synchronous — every query goes through `run_read_query`. Writing async
twins would mean forking ~20 call sites of code that /chat v1 still runs live,
so instead the sync tools are dispatched to an executor. Not
`asyncio.to_thread`, though: that uses the loop's *default* executor, which is
already shared with panel synthesis and the blocking `urllib` call inside
`_http_run`. On a 0.5 vCPU box, six parallel graph queries on the default pool
starve panel sync. A private pool bounds the blast radius.

**Two middleware that cannot reach here.** `ToolErrorMiddleware` and
`ToolCallLimitMiddleware` only fire inside a LangChain graph node that
executes tools, and these calls do not run in one. Their semantics are
implemented here instead — error-as-data via
`api.chat.v2.tools.model_visible_errors`, and the per-turn cap via
`TurnBudget` — so the behaviour exists exactly once rather than twice.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Callable

from api.chat.v2.tools import ALL_TOOLS

logger = logging.getLogger(__name__)

# Six covers the widest plan the spike ever produced (a multi-hop question
# fanned to five searches) with one slot spare. It must also stay under the
# Neo4j pool ceiling: workers x uvicorn workers <= the driver's connection
# limit. The starter instance runs one uvicorn worker, so six is safe.
#
# NB `semantic_search` fans out to a blocking Gemini embedding call and shares
# this pool, so the count is not purely a Neo4j budget.
TOOL_WORKERS = int(os.getenv("CHAT_V2_TOOL_WORKERS", "6"))

# A single tool call that outlives this is reported to the synthesizer as an
# error rather than allowed to hold the whole turn. `run_cypher` already caps
# itself at 10 s server-side; this is the outer backstop.
TOOL_TIMEOUT_S = float(os.getenv("CHAT_V2_TOOL_TIMEOUT_S", "20"))

# Ceiling on tool calls per turn across every tier. The planner is capped
# structurally (one plan, one optional follow-up round), so this only catches
# a pathological plan.
MAX_TOOL_CALLS_PER_TURN = int(os.getenv("CHAT_V2_MAX_TOOL_CALLS", "10"))

_TOOL_POOL = ThreadPoolExecutor(
    max_workers=TOOL_WORKERS, thread_name_prefix="chatv2-tool"
)


@dataclass
class TurnBudget:
    """Per-turn caps shared across all three tiers.

    `ModelCallLimitMiddleware` counts per agent per run, and the tiered loop
    runs up to three different agents in one turn — so it cannot see the whole
    turn. This can.
    """

    max_tool_calls: int = MAX_TOOL_CALLS_PER_TURN
    max_model_calls: int = 8
    tool_calls: int = 0
    model_calls: int = 0

    def take_tool_calls(self, n: int) -> int:
        """Reserve up to `n` tool calls; returns how many were granted."""
        granted = max(0, min(n, self.max_tool_calls - self.tool_calls))
        self.tool_calls += granted
        return granted

    def take_model_call(self) -> bool:
        if self.model_calls >= self.max_model_calls:
            return False
        self.model_calls += 1
        return True


@dataclass
class ExecutedCall:
    """One tool call and what came back. The loop's unit of record: the
    synthesizer reads it, the scope is harvested from it, the panel is built
    from it, and the eval's tool-trajectory assertion is scored on it."""

    tool: str
    args: dict[str, Any]
    result: Any = None
    ms: int = 0
    tier: int = 1
    error: str | None = None
    ui_rows: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": self.args, "result": self.result,
                "ms": self.ms, "tier": self.tier, "error": self.error}


async def execute_plan(
    calls: list[dict[str, Any]],
    *,
    budget: TurnBudget,
    tier: int = 1,
    registry: dict[str, Callable] | None = None,
    on_complete: Callable[[ExecutedCall], None] | None = None,
) -> list[ExecutedCall]:
    """Run every call in `calls` concurrently and return them in plan order.

    `on_complete` fires as each call lands, not at the end — that is what lets
    the SSE stream tick a status line per result instead of going silent for
    the whole execute phase.

    Nothing raises. A tool that fails becomes an `ExecutedCall` carrying an
    `error`, which the synthesizer can see and write around; killing the turn
    over one bad argument is the failure mode this loop exists to avoid.
    """
    registry = registry if registry is not None else ALL_TOOLS
    granted = budget.take_tool_calls(len(calls))
    if granted < len(calls):
        logger.warning(
            "chat v2: tool budget capped plan from %d to %d calls",
            len(calls), granted,
        )
    planned = calls[:granted]

    results: list[ExecutedCall | None] = [None] * len(planned)

    async def _one(index: int, call: dict[str, Any]) -> None:
        name = call.get("tool") or ""
        args = call.get("args") or {}
        executed = ExecutedCall(tool=name, args=args, tier=tier)
        started = time.perf_counter()
        fn = registry.get(name)
        if fn is None:
            executed.error = (
                f"unknown tool {name!r}; available: {sorted(registry)}"
            )
            executed.result = {"error": executed.error}
        else:
            try:
                executed.result = await asyncio.wait_for(
                    _invoke(fn, args), timeout=TOOL_TIMEOUT_S
                )
            except asyncio.TimeoutError:
                executed.error = f"{name} timed out after {TOOL_TIMEOUT_S:.0f}s"
                executed.result = {"error": executed.error}
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                logger.exception("chat v2 tool %s failed", name)
                executed.error = f"{type(exc).__name__}: {exc}"
                executed.result = {"error": executed.error}
        executed.result, executed.ui_rows = _split_ui_rows(executed.result)
        if isinstance(executed.result, dict) and executed.result.get("error"):
            executed.error = executed.error or str(executed.result["error"])
        executed.ms = int((time.perf_counter() - started) * 1000)
        results[index] = executed
        if on_complete is not None:
            on_complete(executed)

    await asyncio.gather(*(_one(i, c) for i, c in enumerate(planned)))
    return [r for r in results if r is not None]


def _split_ui_rows(result: Any) -> tuple[Any, list[dict]]:
    """Separate the UI overflow rows from what the model sees.

    The tools layer attaches up to 500 full rows under `_ui_results` so the
    matches panel can render every hit. Those rows must never enter a prompt:
    in v1 one such search inflated a request from 38k to 109k input tokens.
    Splitting here — once, on the way out of the executor — means every
    downstream consumer gets the right half by default.
    """
    if not isinstance(result, dict) or "_ui_results" not in result:
        return result, []
    trimmed = {k: v for k, v in result.items() if k != "_ui_results"}
    rows = result.get("_ui_results")
    return trimmed, rows if isinstance(rows, list) else []


async def _invoke(fn: Callable, args: dict[str, Any]) -> Any:
    """Await an async tool directly; send a sync one to the private pool."""
    if asyncio.iscoroutinefunction(fn):
        return await fn(**args)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_TOOL_POOL, partial(fn, **args))
