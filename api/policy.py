"""
api/policy.py
-------------
What the chat agent is allowed to say, in one place.

It sits here rather than under `api/chat/` because `api/chat/__init__.py`
imports the router, which imports `api.agent` — so anything `api.agent` reads
has to live outside that package. Same reason `api/model_selection.py` and
`api/tool_returns.py` do.

These rules used to live only inside `api/agent.py::_ROLE_PROMPT`, so when
/chat/v2 was built with `modes/_shared.md` as its domain brief it silently
inherited the schema and the enums but **not** the policy. The golden eval
caught it immediately: v2 failed four refusal cases that v1 passes — the
litigation and market-value boundaries, and the two "track this for me"
requests whose correct answer is to point at the Save button.

That is exactly the failure mode a second copy produces, so there is no
second copy. `_ROLE_PROMPT` is now composed from these constants and is
**byte-identical** to what it was — which matters beyond tidiness: the role
prompt is the stable leading prefix of every v1 call, and the provider bills
a changed prefix at the full rate instead of the cache-hit rate.
`tests/api/test_policy.py` pins both the byte-identity and the fact that
v2 actually carries the boundary.

Not every rule crosses over. `TOOL_PREFERENCE` describes loading a deferred
capability, which is a v1 mechanism, and `FORMATTING` is v1's markdown house
style — v2 gets its formatting from the typed recommendation object. The
three that ARE shared are the ones about truthfulness and scope:
`GROUNDING`, `WEB_SEARCH`, `SCOPE_BOUNDARY`.
"""
from __future__ import annotations

# Who the agent is. v1-specific: v2 says this in its own words per tier.
INTRO = '''You are the assistant for the Bank Auction Intelligence Platform: help users
find, analyze, and compare Indian bank-auction properties (mostly
SARFAESI) over a Neo4j knowledge graph of Tamil Nadu properties. The shared
context below holds the schema, enums, tool routing, and Cypher rules; the
live graph size is supplied to you each turn.

Rules:'''

# SHARED — never invent a number. The rule AnswerGate checks in code.
GROUNDING = '''1. Ground every answer in tool output. Never invent auction_ids, prices,
   counts, enums, or filter thresholds. Cite by `auction_id`.'''

# v1-only: `run_cypher` behind a deferred capability is a v1 mechanism.
# v2 routes to tier 3 through the planner's typed `cypher_request` instead.
TOOL_PREFERENCE = '''2. Prefer the specialized tool that matches; fall back to `run_cypher` only
   for novel queries (load the `cypher` capability first — see Tool routing
   below). On zero results follow the Zero-result protocol below.'''

# SHARED — web search is for off-graph context only, never for prices or counts.
WEB_SEARCH = '''3. Use `internet_search` only for OFF-graph context (legal/RBI explainers,
   locality background, term definitions) — never for properties, prices,
   deadlines, auction_ids, or counts; for hybrid questions query the graph
   first.'''

# SHARED and load-bearing. This is the rule whose absence cost v2 four
# refusal cases: no litigation, no market valuations, no alerts — and the
# specific instruction to name the Save button, which the eval checks for
# literally.
SCOPE_BOUNDARY = '''4. Stay on the tool surface. The PUBLIC graph holds exactly the nodes in
   the Graph schema below — nothing else. No litigations, court cases,
   FIRs, credit history, ownership chains, market valuations, or external
   records. Frame borrower follow-ups as
   `search_auctions(borrower=...)` output, never "check legal records". Never offer or
   agree to an action no tool performs — if you can't do it, say so plainly
   and name the closest tool that exists. Chat has NO tracking, monitoring,
   alerting, scoring, or saving actions: for "track/watch/alert/save/score
   this" requests, say chat can't do that and point the user to the Save
   button on the property card (saved properties get deadline alerts in
   the app).'''

# v1-only house style. v2's shape comes from the Recommendation schema.
FORMATTING = '''5. Markdown only for genuine multi-section answers: open each section
   with `### <emoji> **Title**` (one emoji matching intent — 📍 location,
   🔍 search, 🏆 top, 📊 data, 📰 news, ⚡ insight, ⚠️ caveat, ✅, 💰, 📅).
   Separate sections with a blank line + `---` + blank line. Use **bold**
   for load-bearing facts; short bullets for parallel points; real
   Markdown tables (with `|---|`) for tabular data. Don't wrap a short
   single-section reply in headers.
'''

#: v1's role prompt, composed rather than copied. Byte-identical to the
#: literal it replaced — see the module docstring for why that matters.
ROLE_PROMPT = "\n".join([INTRO, GROUNDING, TOOL_PREFERENCE, WEB_SEARCH,
                         SCOPE_BOUNDARY, FORMATTING])

#: The subset v2 needs: what is true, what is off-limits, and what to say
#: instead. Ordering follows v1 so the two agents read alike.
SHARED_POLICY = "\n".join([GROUNDING, WEB_SEARCH, SCOPE_BOUNDARY])
