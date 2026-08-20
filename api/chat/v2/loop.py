"""
api/chat/v2/loop.py
-------------------
The tiered loop.

    tier 1   one planning call emits every query the question needs
             -> executor runs them against Neo4j in parallel
             -> one synthesis call writes the answer
    tier 2   the synthesizer may ask for one more round (`need_more`)
    tier 3   the planner may ask for composed read-only Cypher instead

Measured against the same 68-case golden catalogue as /chat v1: 11.2 s and
2.15 model calls per turn, versus 73 s and 5.6 calls. The saving is not a
faster model — it is that a ReAct loop can only choose its next tool call
after seeing the last result, so its queries are necessarily serial, while a
plan can be executed all at once.

The planner is also the router; there is no separate classification call.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from api.chat.v2 import prompts
from api.chat.v2.executor import ExecutedCall, TurnBudget, execute_plan
from api.chat.v2.schemas import CypherSpec, Plan, Recommendation, Synthesis
from api.chat.v2.scope import harvest_scope, merge_scope, sanitize_ids, sanitize_scope
from api.chat.v2.tools import CYPHER_TOOLS, PLANNER_TOOLS, render_catalogue

logger = logging.getLogger(__name__)

MAX_ROUNDS = 2

# Ceiling on the tool-result JSON handed to the synthesizer. Generous, because
# the whole point is that this is the ONLY place bulk results enter a prompt —
# v1 pays for them on every subsequent call in the turn, we pay once.
RESULTS_BUDGET = 24_000
FOLLOWUP_BUDGET = 8_000

# The schema brief for tier 3. `cypher_patterns` is stripped because the rules
# are already in the composer's system prompt.
SCHEMA_BUDGET = 7_000

_IDS_IN_PROMPT = 15


@dataclass
class TurnResult:
    answer: str = ""
    recommendation: Recommendation | None = None
    executed: list[ExecutedCall] = field(default_factory=list)
    filters: dict[str, Any] = field(default_factory=dict)
    last_total_count: int | None = None
    last_ids: list[str] = field(default_factory=list)
    tier: int = 1
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    seconds: float = 0.0


async def run_turn(
    question: str,
    *,
    scope: dict[str, Any] | None = None,
    last_ids: list[str] | None = None,
    last_total_count: int | None = None,
    model_name: str = "flash",
    reasoning_effort: str | None = None,
    budget: TurnBudget | None = None,
    max_rounds: int = MAX_ROUNDS,
    on_event: Callable[[str, dict], None] | None = None,
) -> TurnResult:
    """Run one chat turn. Never raises for a tool failure — a failed call
    becomes a result the synthesizer can write around."""
    from api.chat.v2.agents import build_tier_agent

    started = time.perf_counter()
    budget = budget or TurnBudget()
    filters = sanitize_scope(scope or {})
    last_ids = sanitize_ids(last_ids or [])
    result = TurnResult(filters=filters, last_total_count=last_total_count,
                        last_ids=last_ids)

    def emit(event: str, payload: dict) -> None:
        if on_event is not None:
            on_event(event, payload)

    catalogue = render_catalogue(PLANNER_TOOLS)
    planner = build_tier_agent(
        system_prompt=prompts.PLANNER_SYSTEM.format(
            shared=prompts.shared_context(), catalogue=catalogue),
        response_format=Plan,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )
    synthesizer = build_tier_agent(
        system_prompt=prompts.SYNTH_SYSTEM.format(shared=prompts.shared_context()),
        response_format=Synthesis,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )

    scope_block = _scope_block(filters, last_total_count, last_ids)
    followup = ""

    for round_index in range(max_rounds):
        final_round = round_index + 1 >= max_rounds

        emit("status", {"label": "Planning…"})
        plan = await _ask(planner, prompts.PLANNER_USER.format(
            scope=scope_block, question=question, followup=followup,
        ), Plan, budget, result)

        if plan is None:
            result.answer = _CANT_PLAN
            break

        # A topic switch must drop the carried filters, or the answer is
        # silently about the previous question's city.
        if plan.scope == "reset":
            filters = {}
            scope_block = ""

        if plan.direct_answer and not plan.calls and not plan.cypher_request:
            result.answer = plan.direct_answer
            break

        if plan.cypher_request:
            result.tier = 3
            emit("status", {"label": "Reading the graph schema…"})
            result.executed.extend(
                await _tier3(plan.cypher_request, question, model_name,
                             reasoning_effort, budget, result, emit)
            )
        elif plan.calls:
            calls = [
                {"tool": c.tool,
                 "args": merge_scope(filters, c.args) if c.tool == "search_auctions"
                         else {k: v for k, v in c.args.items() if v is not None}}
                for c in plan.calls
            ]
            emit("plan", {"tier": result.tier,
                          "calls": [{"tool": c["tool"]} for c in calls]})
            result.executed.extend(await execute_plan(
                calls, budget=budget, tier=result.tier,
                on_complete=lambda c: emit("status", {"label": _status_label(c)}),
            ))
        else:
            result.answer = plan.direct_answer or _CANT_PLAN
            break

        emit("status", {"label": "Writing the answer…"})
        synthesis = await _ask(synthesizer, prompts.SYNTH_USER.format(
            question=question, results=_truncate(result.executed, RESULTS_BUDGET),
        ) + (prompts.FINAL_ROUND_NOTE if final_round else ""), Synthesis, budget, result)

        if synthesis is None:
            result.answer = _CANT_ANSWER
            break

        # `need_more` is a typed field, not a `NEED_MORE:` prefix, so it can
        # never leak into the user's answer the way the spike's marker did.
        if synthesis.need_more and not final_round:
            result.tier = max(result.tier, 2)
            followup = (
                "\nResults so far:\n"
                + _truncate(result.executed, FOLLOWUP_BUDGET)
                + "\nPlan ONLY the additional calls needed. Do not repeat the "
                  "calls above."
            )
            continue

        result.answer = synthesis.answer
        result.recommendation = synthesis.recommendation
        break

    result.filters, harvested_total, harvested_ids = harvest_scope(
        [c.as_dict() for c in result.executed], previous=filters,
    )
    if harvested_total is not None:
        result.last_total_count = harvested_total
    if harvested_ids:
        result.last_ids = harvested_ids
    result.seconds = round(time.perf_counter() - started, 2)
    return result


_CANT_PLAN = (
    "I couldn't work out how to look that up. Could you rephrase it, or name "
    "a city, bank, or price range to start from?"
)
_CANT_ANSWER = (
    "I found results but couldn't write them up. Please try that again."
)


async def _ask(agent, user_message: str, schema, budget: TurnBudget,
               result: TurnResult):
    """One structured model call, with the turn budget enforced across tiers."""
    if not budget.take_model_call():
        logger.warning("chat v2: model-call budget exhausted")
        return None
    state = await agent.ainvoke({"messages": [("user", user_message)]})
    result.model_calls += 1
    _accumulate_usage(state, result)
    structured = state.get("structured_response")
    if isinstance(structured, schema):
        return structured
    logger.warning("chat v2: %s call returned %r", schema.__name__, type(structured))
    return None


def _accumulate_usage(state: dict, result: TurnResult) -> None:
    """Map LangChain's usage metadata onto the same fields v1's obs log
    reports, so the existing dashboards keep working across both endpoints."""
    for message in reversed(state.get("messages") or []):
        usage = getattr(message, "usage_metadata", None)
        if not usage:
            continue
        result.input_tokens += usage.get("input_tokens", 0) or 0
        result.output_tokens += usage.get("output_tokens", 0) or 0
        details = usage.get("input_token_details") or {}
        result.cached_tokens += details.get("cache_read", 0) or 0
        return


async def _tier3(request: str, question: str, model_name: str,
                 reasoning_effort: str | None, budget: TurnBudget,
                 result: TurnResult, emit) -> list[ExecutedCall]:
    """Schema (code, no model) -> compose (one model call) -> run_cypher,
    with a single error-feedback retry.

    The retry exists because the first failure is usually a property name the
    composer guessed; handing back the real error text fixes it far more often
    than re-composing blind.
    """
    from api.chat.v2.agents import build_tier_agent

    schema_call = (await execute_plan(
        [{"tool": "describe_schema", "args": {}}],
        budget=budget, tier=3, registry=CYPHER_TOOLS,
    ))[0]
    schema_text = _schema_brief(schema_call.result)

    composer = build_tier_agent(
        system_prompt=prompts.CYPHER_SYSTEM.format(rules=prompts.cypher_rules()),
        response_format=CypherSpec,
        model_name=model_name,
        reasoning_effort=reasoning_effort,
    )

    out: list[ExecutedCall] = [schema_call]
    error_note = ""
    for attempt in range(2):
        spec = await _ask(composer, prompts.CYPHER_USER.format(
            schema=schema_text, request=request, question=question,
            error_note=error_note,
        ), CypherSpec, budget, result)
        if spec is None or not spec.cypher:
            out.append(ExecutedCall(tool="run_cypher", args={}, tier=3,
                                    error="composer produced no query",
                                    result={"error": "composer produced no query"}))
            break

        emit("status", {"label": f"Querying the graph: {spec.description or request}"})
        executed = (await execute_plan(
            [{"tool": "run_cypher", "args": {
                "cypher": spec.cypher, "params": spec.params,
                "description": spec.description or request}}],
            budget=budget, tier=3, registry=CYPHER_TOOLS,
        ))[0]
        out.append(executed)

        if executed.error and attempt == 0:
            error_note = (
                f"\nYour previous query failed — fix it.\n"
                f"Query: {spec.cypher}\nError: {executed.error}\n"
            )
            continue
        break
    return out


def _schema_brief(schema: Any, budget: int = SCHEMA_BUDGET) -> str:
    if not isinstance(schema, dict):
        return "{}"
    trimmed = dict(schema)
    trimmed.pop("cypher_patterns", None)  # already in the composer's prompt
    return json.dumps(trimmed, default=str)[:budget]


def _scope_block(filters: dict, total: int | None, ids: list[str]) -> str:
    if not filters and not ids:
        return ""
    return prompts.SCOPE_BLOCK.format(
        filters=json.dumps(filters, default=str),
        total=total,
        ids=json.dumps(ids[:_IDS_IN_PROMPT]),
    )


def _truncate(executed: list[ExecutedCall], budget: int) -> str:
    """Serialize the results for the synthesizer, trimming rows before
    truncating text — a half-cut JSON blob is worse than fewer complete rows."""
    payload = [
        {"tool": c.tool, "args": c.args, "result": c.result, "error": c.error}
        for c in executed
    ]
    text = json.dumps(payload, default=str)
    if len(text) <= budget:
        return text
    for entry in payload:
        res = entry.get("result")
        if isinstance(res, dict) and isinstance(res.get("results"), list):
            res["results"] = res["results"][:5]
            res["_note"] = ("rows truncated for context — counts in "
                            "total_count remain exact")
    return json.dumps(payload, default=str)[:budget]


def _status_label(call: ExecutedCall) -> str:
    """The progress line the panel shows while the plan runs."""
    pretty = call.tool.replace("_", " ")
    if call.error:
        return f"{pretty} — no result"
    total = None
    if isinstance(call.result, dict):
        total = call.result.get("total_count")
        if total is None and isinstance(call.result.get("results"), list):
            total = len(call.result["results"])
    return f"{pretty} · {total} match" if total is not None else pretty
