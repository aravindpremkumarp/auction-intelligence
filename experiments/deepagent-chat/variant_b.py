"""Variant B — the tiered loop: plan once → execute in parallel → answer once.

Tier 1: one planning call emits every graph query as JSON; code executes
them concurrently; one synthesis call writes the answer.
Tier 2: the synthesizer may reply NEED_MORE with a follow-up plan (capped
at `max_rounds`).
Tier 3: the planner may signal `cypher_request` for novel analytical
questions no typed tool expresses (per-group aggregates, computed grouping,
intersections, HAVING-style conditions). Code then fetches the live schema
(no LLM), one composer call writes read-only Cypher, `run_cypher` executes
it under production guardrails, with one error-feedback retry.

The planner itself is the router — no separate classification call.

Multi-turn: `run(question, state=...)` carries a SCOPE OBJECT instead of a
transcript — active search filters, the last result ids, and the last
total_count. Code merges the scope into every search_auctions call, so
carry-over is deterministic rather than a prompt hope; the model only
expresses *changes* (a new value overrides, an explicit null drops).
"""
from __future__ import annotations

import inspect as _inspect
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from llm import Usage, make_model  # noqa: E402
from spike_tools import CYPHER_TOOLS, TOOLS, load_instructions  # noqa: E402

_TOOL_MAP = {t.__name__: t for t in TOOLS}

_TOOL_CATALOG = "\n".join(
    f"- {t.__name__}{_inspect.signature(t)}\n  {' '.join(t.__doc__.split())}"
    for t in TOOLS
)

# Filters that persist across turns as the conversation's scope. Everything
# else (limit, order_by, aggregations, group_by) is per-question intent.
_SCOPE_KEYS = frozenset({
    "min_price", "max_price", "city", "area", "property_type",
    "asset_category", "bank", "borrower", "is_reauction",
    "starts_after", "starts_before", "deadline_within_days",
})

_PLAN_PROMPT = """{instructions}

You are the QUERY PLANNER. Emit every tool call needed to answer the user's
question — they run in parallel, so include all of them now. Reply with ONLY
a JSON object, no prose:

{{"calls": [{{"tool": "<name>", "args": {{...}}}}, ...],
  "cypher_request": null,
  "direct_answer": null}}

Tools:
{catalog}

Tool arg notes: dates are ISO strings; `aggregations` needs
`aggregate_field` (valid fields: reserve_price_num, emd_num) and computes
over the WHOLE filtered set — there is no per-group aggregation; `group_by`
returns COUNTS per group only.

Set "cypher_request" (one plain-English line stating exactly what to
compute) INSTEAD of "calls" only when no tool above can express the
question: ANY aggregate per group (lowest/highest/average/median per bank,
city or area — group_by cannot do this), grouping
by computed values (per month/quarter), percentiles other than
p25/median/p75, set intersections across filters, or conditions on group
counts (e.g. borrowers with more than one property). A raw-Cypher engine
with the live schema will handle it.

Off-graph factual questions (legal/RBI rules, SARFAESI/EMD explainers,
locality context): if `internet_search` is in the tool list above, CALL it —
answers must carry sources. Use "direct_answer" only for greetings/meta
questions, when no search tool exists for the topic, or to say the graph
cannot answer it.
{scope}
Question: {question}
{followup}"""

_SCOPE_BLOCK = """
Active search scope from earlier turns — these filters are merged into every
search_auctions call automatically; emit a filter only to CHANGE it (new
value) or DROP it (explicit null):
{filters}
Last result: total_count={total}, auction_ids={ids}
When the user says "these", "those", "of them", "the cheapest one", resolve
against those auction_ids (use get_auction_details for detail requests).
"""

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

# Distilled from production's deferred `cypher` capability instructions
# (api/agent.py) — each rule exists because the mistake silently returns
# zero rows.
_CYPHER_RULES = """Cypher rules:
- Read-only. MATCH from (a:AuctionProperty); domain edges exist only FROM
  AuctionProperty — never chain them from Bank/City/etc.
- auction_start_dt / auction_end_dt / application_deadline_dt are Neo4j
  ZONED DATETIME (UTC), not strings. Components: .year .month .day .quarter;
  never compare to a raw ISO string — wrap with datetime($iso).
- No total_area/village/taluk/district properties on AuctionProperty for
  filtering here; sizes and sub-locality live in description text.
- Prefer counts/aggregates over returning many rows; LIMIT everything.
- Live/future auctions: WHERE a.auction_start_dt >= datetime() unless the
  question is explicitly historical."""

_CYPHER_COMPOSE_PROMPT = """You write one read-only Neo4j Cypher query.

{rules}

Live schema (labels, relationships, properties, enums):
{schema}

Task: {request}
User question: {question}
{error_note}
Reply with ONLY a JSON object: {{"cypher": "<query>", "params": {{}},
"description": "<what it computes>"}}"""


def _parse_json(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _merge_scope(name: str, args: dict, scope: dict) -> dict:
    """Deterministic filter carry-over: scope filters ride on every
    search_auctions call; the planner's args express only changes. An
    explicit null in args drops the filter for this and later turns."""
    if name != "search_auctions":
        return {k: v for k, v in args.items() if v is not None}
    merged = {k: v for k, v in scope.items() if k in _SCOPE_KEYS}
    for k, v in args.items():
        if v is None:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


def _execute(calls: list[dict], usage: Usage, scope: dict) -> list[dict]:
    def one(call: dict) -> dict:
        name = call.get("tool", "")
        fn = _TOOL_MAP.get(name)
        if fn is None:
            return {"tool": name, "error": f"unknown tool {name!r}"}
        usage.tool_calls.append(name)
        args = _merge_scope(name, call.get("args") or {}, scope)
        try:
            return {"tool": name, "args": args, "result": fn(**args)}
        except Exception as e:  # surface, don't crash the turn
            return {"tool": name, "args": args, "error": str(e)}

    with ThreadPoolExecutor(max_workers=6) as pool:
        return list(pool.map(one, calls))


def _update_state(state: dict, results: list[dict]) -> None:
    ids: list[str] = list(state.get("last_ids") or [])
    for r in results:
        res = r.get("result")
        if not isinstance(res, dict):
            continue
        if r.get("tool") == "search_auctions" and "error" not in res:
            new_filters = {
                k: v for k, v in (r.get("args") or {}).items()
                if k in _SCOPE_KEYS
            }
            state["active_filters"] = new_filters
            if res.get("total_count") is not None:
                state["last_total_count"] = res["total_count"]
        rows = res.get("results")
        if isinstance(rows, list):
            turn_ids = [row.get("auction_id") for row in rows
                        if isinstance(row, dict) and row.get("auction_id")]
            if turn_ids:
                ids = turn_ids  # most recent result set wins
    state["last_ids"] = ids[:15]


def _schema_brief(usage: Usage, budget: int = 7000) -> str:
    usage.tool_calls.append("describe_schema")
    schema = CYPHER_TOOLS["describe_schema"]()
    if isinstance(schema, dict):
        schema = dict(schema)
        schema.pop("cypher_patterns", None)  # rules cover the same ground
    text = json.dumps(schema, default=str)
    return text[:budget]


def _tier3(request: str, question: str, model, usage: Usage) -> list[dict]:
    """Schema (code) → compose (LLM) → run_cypher, one error-feedback retry."""
    schema = _schema_brief(usage)
    error_note = ""
    out: list[dict] = []
    for _attempt in range(2):
        msg = model.invoke(_CYPHER_COMPOSE_PROMPT.format(
            rules=_CYPHER_RULES, schema=schema, request=request,
            question=question, error_note=error_note,
        ))
        usage.add_message(msg)
        spec = _parse_json(msg.content or "") or {}
        cypher = spec.get("cypher") or ""
        if not cypher:
            out.append({"tool": "run_cypher", "error": "composer produced no query"})
            break
        usage.tool_calls.append("run_cypher")
        result = CYPHER_TOOLS["run_cypher"](
            cypher=cypher, params=spec.get("params") or {},
            description=spec.get("description") or request,
        )
        entry = {"tool": "run_cypher", "args": {"cypher": cypher}, "result": result}
        failed = isinstance(result, dict) and result.get("error")
        if failed and _attempt == 0:
            error_note = (f"\nYour previous query failed — fix it.\n"
                          f"Query: {cypher}\nError: {result.get('error')}\n")
            out = [entry]  # keep the failure visible to the synthesizer
            continue
        out.append(entry)
        break
    return out


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


def run(question: str, state: dict | None = None, max_rounds: int = 2) -> dict:
    model = make_model()
    instructions = load_instructions()
    usage = Usage()
    t0 = time.perf_counter()

    state = state if state is not None else {}
    scope_filters = dict(state.get("active_filters") or {})
    scope_block = ""
    if scope_filters or state.get("last_ids"):
        scope_block = _SCOPE_BLOCK.format(
            filters=json.dumps(scope_filters, default=str),
            total=state.get("last_total_count"),
            ids=json.dumps((state.get("last_ids") or [])[:15]),
        )

    followup = ""
    all_results: list[dict] = []
    answer = ""

    for _round in range(max_rounds):
        plan_msg = model.invoke(_PLAN_PROMPT.format(
            instructions=instructions, catalog=_TOOL_CATALOG,
            question=question, followup=followup, scope=scope_block,
        ))
        usage.add_message(plan_msg)
        plan = _parse_json(plan_msg.content or "") or {}

        if plan.get("direct_answer"):
            answer = plan["direct_answer"]
            break

        if plan.get("cypher_request"):
            all_results.extend(
                _tier3(str(plan["cypher_request"]), question, model, usage))
        else:
            calls = plan.get("calls") or []
            if not calls:
                answer = plan_msg.content or "(planner produced no plan)"
                break
            all_results.extend(_execute(calls, usage, scope_filters))

        synth_msg = model.invoke(_SYNTH_PROMPT.format(
            instructions=instructions, question=question,
            results=_truncate_results(all_results),
        ))
        usage.add_message(synth_msg)
        content = (synth_msg.content or "").strip()

        if "NEED_MORE:" in content and not content.startswith("NEED_MORE:"):
            content = "NEED_MORE:" + content.split("NEED_MORE:", 1)[1]
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
            if extra.get("cypher_request"):
                all_results.extend(_tier3(
                    str(extra["cypher_request"]), question, model, usage))
            elif calls:
                all_results.extend(_execute(calls, usage, scope_filters))
            final = model.invoke(_SYNTH_PROMPT.format(
                instructions=instructions, question=question,
                results=_truncate_results(all_results),
            ) + "\n\nThis is the final round: write the answer now — NEED_MORE is not available.")
            usage.add_message(final)
            answer = (final.content or "").strip()
        else:
            answer = content
        break

    _update_state(state, all_results)

    return {
        "variant": "B tiered",
        "seconds": round(time.perf_counter() - t0, 1),
        "llm_calls": usage.llm_calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "tools": usage.tool_calls,
        "answer": answer,
        "state": state,
        "executed": [
            {"tool": r.get("tool"), "args": r.get("args")}
            for r in all_results
        ],
    }
