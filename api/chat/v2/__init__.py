"""
api/chat/v2
-----------
The chat agent's tiered loop: one planning call emits every graph query the
question needs, code runs them against Neo4j in parallel, one synthesis call
writes the answer. Escape hatches for a follow-up round (tier 2) and for a
composed read-only Cypher (tier 3).

Why this exists: /chat v1 is a ReAct loop and measures 73 s per turn — 5.6
sequential model calls, ~86 % of each call's input tokens being static prompt.
The tiered loop answered the same golden catalogue at 11.2 s and 2.15 calls.

**Import discipline.** The LangChain stack costs ~28 MB of RSS and the Render
instance is a 512 MB starter, so nothing in this package may be imported at
module scope by `api/main.py` or the v1 router. `api/chat/v2/router.py` builds
the agent on the first /chat/v2 request; until then an idle deploy and all v1
traffic stay at today's footprint.

`deepagents` is installed but deliberately **not** imported anywhere here: it
costs another ~107 MB, and the tiered loop does not use it — deepagents' own
loop is the ReAct shape v1 already has. It arrives with the subagent work.
"""
