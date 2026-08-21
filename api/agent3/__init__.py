"""
api/agent3
----------
The auction agent, built from scratch against the current graph.

Owes nothing to `api/agent.py` (v1, pydantic-ai), `api/chat/v2` or
`api/policy.py` — not the prompt, not the tools, not the enums. That is the
point: `modes/_shared.md` describes a graph that no longer exists, and an
agent told "no total_area/village/taluk/district props exist, never filter on
them" will refuse questions this graph answers exactly.

`docs/auction-deep-agent-2026-08.md` is the design and carries the profiling
numbers every decision here rests on.

**Two layers, one join.** A listing (`AuctionProperty`, 2,964 — keyed by
`auction_id`, which is what every URL, the matches panel and the saved-property
flow use) hangs off a sale notice (`Document` → `Lot` → `Auction`). The notice
layer is where extent, survey numbers, boundaries, possession, encumbrance and
loan outstanding live.

**The sharp edge, and the rule that follows from it.** A notice fans out to
4.4 lots on average; only 966 listings sit on a single-lot notice, and neither
`IS_PARCEL` nor anything else disambiguates the rest. So a lot fact is
per-property truth ONLY when the notice has one lot. Every lot-derived value
this package returns is tagged `scope: "lot"` or `scope: "notice"`, and the
agent may not state a `notice`-scoped value as a property fact. Stating "this
property is 2,400 sqft" off a six-lot notice is the failure mode this data
introduces, and `scope_honesty` in the evals is the gate for it.

**Why `api/agent3/` and not `api/chat/agent3/`.** `api/chat/__init__.py`
imports the chat router, so anything under that package drags FastAPI and the
whole web stack in at import time. The tools here must be importable by evals,
scripts and unit tests with nothing but the Neo4j driver installed — the same
reason `api/policy.py`, `api/model_selection.py` and `api/tool_returns.py`
already sit outside it. The agent loop, when it is built, can live under
`api/chat/` and import these.
"""
