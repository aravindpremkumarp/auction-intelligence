"""
api/policy.py
-------------
What the admin chat loops (`/chat/v2`, `/chat/deep`) are allowed to say, in
one place. agent3 carries its own rules in `api/agent3/instructions.md`.

These rules were first written for the pydantic-ai agent (`/chat`, now
retired). When /chat/v2 was built with `modes/_shared.md` as its domain brief
it silently inherited the schema and the enums but **not** the policy. The
golden eval caught it immediately: v2 failed four refusal cases the old agent
passed — the litigation and market-value boundaries, and the two "track this
for me" requests whose correct answer is to point at the Save button.

The three rules that crossed over are the ones about truthfulness and scope:
`GROUNDING`, `WEB_SEARCH`, `SCOPE_BOUNDARY`. They keep their original numbers
(1, 3, 4) because `SHARED_POLICY` is part of v2's cached prompt prefix, and
the provider bills a changed prefix at the full rate instead of the cache-hit
rate. `tests/api/test_policy.py` pins that v2 actually carries the boundary.
"""
from __future__ import annotations

# SHARED — never invent a number. The rule AnswerGate checks in code.
GROUNDING = '''1. Ground every answer in tool output. Never invent auction_ids, prices,
   counts, enums, or filter thresholds. Cite by `auction_id`.'''

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

#: What v2 needs: what is true, what is off-limits, and what to say instead.
SHARED_POLICY = "\n".join([GROUNDING, WEB_SEARCH, SCOPE_BOUNDARY])
