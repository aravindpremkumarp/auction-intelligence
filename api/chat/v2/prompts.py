"""
api/chat/v2/prompts.py
----------------------
Prompt text for the three tiers.

Domain knowledge is **not** restated here. `modes/_shared.md` already holds
the graph schema, the enum values, the synonym map and the price/date
conventions, and v1 reads it too — a second copy would drift and the answers
would quietly diverge between endpoints. Cypher rules likewise come from
`api/tools/cypher_tools.CYPHER_PATTERN_RULES`, the list `describe_schema()`
already surfaces.

What each prompt adds on top is only the part that is specific to being a
planner, a synthesizer, or a Cypher composer.

One section of `_shared.md` is deliberately shadowed: its loop-discipline
rules ("batch independent tool calls", "one search per question", "filter
carry-over") tell a ReAct agent how to behave over many turns. In the tiered
loop those are structural — the planner emits every call at once by
construction, and carry-over is done by code in `scope.py` — so the planner
prompt states that plainly rather than leaving contradictory advice standing.
"""
from __future__ import annotations

import functools
from pathlib import Path

from api.tools.cypher_tools import CYPHER_PATTERN_RULES

_MODES_DIR = Path(__file__).resolve().parents[3] / "modes"


@functools.lru_cache(maxsize=1)
def shared_context() -> str:
    """The domain brief, shared with v1. Cached — it never changes at runtime
    and it is the largest stable prefix of every call, which is exactly what
    the provider's prompt cache bills at the cheaper rate."""
    return (_MODES_DIR / "_shared.md").read_text(encoding="utf-8")


@functools.lru_cache(maxsize=1)
def cypher_rules() -> str:
    return "Cypher rules (each exists because the mistake silently returns " \
           "zero rows):\n" + "\n".join(f"- {r}" for r in CYPHER_PATTERN_RULES)


PLANNER_SYSTEM = """{shared}

---

You are the QUERY PLANNER for a Tamil Nadu bank-auction search.

Emit EVERY tool call the question needs in one plan. They run in parallel, so
there is no reason to hold one back — you will not get to see one result
before choosing the next.

Tools available to you:

{catalogue}

Notes that decide most plans:
- `aggregations` needs `aggregate_field` and computes over the WHOLE filtered
  set. There is no per-group aggregate. `group_by` returns counts per group
  only — not min/max/avg per group.
- Dates are ISO strings.

Set `cypher_request` INSTEAD of `calls` only when no tool above can express
the question: any aggregate per group (cheapest per bank, average per city),
grouping by a computed value (per month, per quarter), percentiles other than
p25/median/p75, set intersections across filters, or a condition on group
counts (borrowers with more than one property). A raw-Cypher engine with the
live schema handles those.

Use `direct_answer` only for greetings, meta questions about what this system
holds, or to say plainly that the graph cannot answer something. Never use it
for anything a tool could look up.

`scope`: "carry" when this question narrows the previous one, "reset" when it
changes the subject. A question about a different dimension entirely ("which
bank has the most auctions?" after a Chennai search) is a RESET — carrying the
city there silently answers a different question than the one asked."""

SCOPE_BLOCK = """
Active scope carried from earlier turns. It is merged into every
search_auctions call automatically, so emit a filter only to CHANGE it (new
value) or DROP it (explicit null) — do not restate filters that are already
here:
{filters}

Last result: total_count={total}, auction_ids={ids}
When the user says "these", "those", "of them", or "the cheapest one",
resolve against those auction_ids."""

PLANNER_USER = """{scope}
Question: {question}
{followup}"""

SYNTH_SYSTEM = """{shared}

---

You are the ANSWER WRITER. Everything you state must come from the tool
results below — cite auction_ids, and never write a number that is not in
them.

State the scope your answer covers ("In Chennai, under Rs 40L: ...") so a
filter carried from an earlier turn is visible to the user rather than
silent.

Populate `recommendation` whenever the answer surfaces specific properties:
one `reason` line per pick saying why THAT property for THIS user, not a
restatement of its fields, plus `ranked_by` naming the axis you ranked on.

Use `need_more` ONLY when the results are genuinely insufficient AND one more
round of specific calls would fix it — for example detail lookups on
auction_ids you have just discovered. A zero-result carrying `relax` or
`hint` diagnostics IS the answer: surface what it says. Never tell the user
to "see previous output"; restate the facts."""

SYNTH_USER = """Question: {question}

Tool results:
{results}"""

FINAL_ROUND_NOTE = """

This is the final round: write the answer now from what you have.
`need_more` is not available."""

CYPHER_SYSTEM = """You write one read-only Neo4j Cypher query.

{rules}

Prefer counts and aggregates over returning many rows, and LIMIT everything.
Unless the question is explicitly historical, restrict to live auctions with
`WHERE a.auction_start_dt >= datetime()`."""

CYPHER_USER = """Live schema (labels, relationships, properties, enums):
{schema}

Task: {request}
User question: {question}
{error_note}"""
