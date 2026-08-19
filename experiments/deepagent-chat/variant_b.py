"""Variant B — the tiered loop: plan once → execute in parallel → answer once.

One planning call emits every graph query the question needs as JSON.
Code executes them concurrently (the tools average <1 s; the 12 s model
round trips are the cost being removed). One synthesis call writes the
answer from the results. The synthesizer may reply NEED_MORE with a new
plan (tier 2 escape hatch); hard cap 2 rounds.

The planner itself is the router: an easy question yields one round of
calls, a multi-hop one flags a second round, an off-graph one returns a
direct answer with no calls at all.
"""
from __future__ import annotations

import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import Usage, make_model  # noqa: E402
from spike_tools import TOOLS, load_instructions  # noqa: E402

_TOOL_MAP = {t.__name__: t for t in TOOLS}

import inspect as _inspect  # noqa: E402

_TOOL_CATALOG = "\n".join(
    f"- {t.__name__}{_inspect.signature(t)}\n  {' '.join(t.__doc__.split())}"
    for t in TOOLS
)

_PLAN_PROMPT = """{instructions}

You are the QUERY PLANNER. Emit every tool call needed to answer the user's
question — they run in parallel, so include all of them now. Reply with ONLY
a JSON object, no prose:

{{"calls": [{{"tool": "<name>", "args": {{...}}}}, ...],
  "direct_answer": null}}

Tools:
{catalog}

Tool arg notes: dates are ISO strings; `aggregations` needs
`aggregate_field`; `group_by` gives distributions. If the question needs no
graph data (definitions, legal explainers, greetings) put the full answer in
"direct_answer" and leave "calls" empty. If it cannot be answered from the
graph, say so in "direct_answer".

Question: {question}
{followup}"""

_SYNTH_PROMPT = """{instructions}

You are the ANSWER WRITER. Below are the tool results for the user's
question. Write the final answer, grounded ONLY in these results — cite
auction_ids, never invent numbers.

Reply `NEED_MORE:` + a JSON plan ONLY when the results are genuinely
insufficient AND one more round of specific tool calls would fix it (e.g.
detail lookups for auction_ids you just discovered). If the results already
answer the question — even a zero-result with refine diagnostics — write the
final answer now. Never tell the user to "see previous output"; restate the
facts.

Question: {question}

Tool results:
{results}"""


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _execute(calls: list[dict], usage: Usage) -> list[dict]:
    def one(call: dict) -> dict:
        name = call.get("tool", "")
        fn = _TOOL_MAP.get(name)
        if fn is None:
            return {"tool": name, "error": f"unknown tool {name!r}"}
        usage.tool_calls.append(name)
        try:
            return {"tool": name, "args": call.get("args", {}),
                    "result": fn(**(call.get("args") or {}))}
        except Exception as e:  # surface, don't crash the turn
            return {"tool": name, "args": call.get("args", {}), "error": str(e)}

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, calls))


def _truncate_results(results: list[dict], budget: int = 24000) -> str:
    text = json.dumps(results, default=str)
    if len(text) <= budget:
        return text
    # Trim per-result rows before giving up: keep counts + first rows.
    for r in results:
        res = r.get("result")
        if isinstance(res, dict) and isinstance(res.get("results"), list):
            res["results"] = res["results"][:5]
            res["_note"] = "rows truncated for context — re-plan a narrower query if needed"
    text = json.dumps(results, default=str)
    return text[:budget]


def run(question: str, max_rounds: int = 2) -> dict:
    model = make_model()
    instructions = load_instructions()
    usage = Usage()
    t0 = time.perf_counter()

    followup = ""
    all_results: list[dict] = []
    answer = ""

    for _round in range(max_rounds):
        plan_msg = model.invoke(_PLAN_PROMPT.format(
            instructions=instructions, catalog=_TOOL_CATALOG,
            question=question, followup=followup,
        ))
        usage.add_message(plan_msg)
        plan = _parse_json(plan_msg.content or "") or {}

        if plan.get("direct_answer"):
            answer = plan["direct_answer"]
            break

        calls = plan.get("calls") or []
        if not calls:
            answer = plan_msg.content or "(planner produced no plan)"
            break

        all_results.extend(_execute(calls, usage))

        synth_msg = model.invoke(_SYNTH_PROMPT.format(
            instructions=instructions, question=question,
            results=_truncate_results(all_results),
        ))
        usage.add_message(synth_msg)
        content = (synth_msg.content or "").strip()

        if content.startswith("NEED_MORE:") and _round + 1 < max_rounds:
            followup = (
                "\nResults so far (round 1):\n"
                + _truncate_results(all_results, budget=8000)
                + "\nPlan ONLY the additional calls requested here — do not "
                "repeat round-1 calls and do not answer via direct_answer "
                "unless no tool call is needed: "
                + content[len("NEED_MORE:"):]
            )
            continue
        if content.startswith("NEED_MORE:"):
            # Final round: execute the requested plan and force a last synth
            # (with NEED_MORE no longer an option) instead of leaking raw JSON.
            extra = _parse_json(content[len("NEED_MORE:"):]) or {}
            calls = extra.get("calls") or extra.get("plan") or []
            if calls:
                all_results.extend(_execute(calls, usage))
            final = model.invoke(_SYNTH_PROMPT.format(
                instructions=instructions, question=question,
                results=_truncate_results(all_results),
            ) + "\n\nThis is the final round: write the answer now — NEED_MORE is not available.")
            usage.add_message(final)
            answer = (final.content or "").strip()
        else:
            answer = content
        break

    return {
        "variant": "B tiered",
        "seconds": round(time.perf_counter() - t0, 1),
        "llm_calls": usage.llm_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "tools": usage.tool_calls,
        "answer": answer,
    }
