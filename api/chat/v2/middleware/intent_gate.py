"""
api/chat/v2/middleware/intent_gate.py
-------------------------------------
Refuses harvesting-shaped requests before any expensive call runs.

**This protects the people in the data, not the business.** Bulk scraping is
better caught by the quota — it needs many requests. The query this exists to
stop is a single one, well under any rate limit: "list every defaulter in
Coimbatore with their addresses". Borrower names in SARFAESI notices are
legally public, and searching them is a product feature; enumerating them all
with contact details is what turns this into a harassment tool. Those are
different requests, and only one of them is refused here.

**Why in code rather than in the prompt.** The evidence is on the record: in
the spike's golden run, enabling web search *softened* a refusal case, because
the policy lived in prompt text and another instruction competed with it. A
policy that can be argued out of by the next prompt edit is not a policy.

Phase 1 ships the regex tier only. The small-model tier for borderline
phrasing is deferred until the fire rate says it is needed — an extra model
call on every turn is exactly the cost this design avoids.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# "Give me all/every <people-ish thing>" — the enumeration shape. Each pattern
# needs BOTH a bulk quantifier and a person-identifying target, so "all
# auctions in Chennai" (a normal search) does not match.
_BULK = r"(?:all|every|each|complete|full|entire|list of|whole)"
_PEOPLE = r"(?:defaulter|borrower|owner|debtor)s?"
_CONTACT = r"(?:address|phone|mobile|contact|email|e-mail|number)"

_HARVEST_PATTERNS = [
    # every borrower + their contact details
    re.compile(rf"\b{_BULK}\b[^.?!]{{0,60}}\b{_PEOPLE}\b[^.?!]{{0,80}}\b{_CONTACT}",
               re.I),
    re.compile(rf"\b{_PEOPLE}\b[^.?!]{{0,40}}\b{_CONTACT}[^.?!]{{0,40}}\b{_BULK}\b",
               re.I),
    # export / dump / scrape shapes
    re.compile(rf"\b(?:export|dump|scrape|csv|spreadsheet|database)\b[^.?!]{{0,60}}"
               rf"\b{_PEOPLE}\b", re.I),
    re.compile(rf"\b{_BULK}\b[^.?!]{{0,40}}\b{_PEOPLE}\b[^.?!]{{0,40}}"
               rf"\b(?:export|csv|spreadsheet|download|dump)\b", re.I),
]

REFUSAL = (
    "I can't produce lists of borrowers with their contact details. "
    "Auction notices are public, and I'm happy to look up a specific "
    "property or search by borrower name — but not to compile a directory "
    "of people."
)


@dataclass(frozen=True)
class IntentVerdict:
    allowed: bool
    reason: str = ""
    refusal: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def classify_intent(message: str) -> IntentVerdict:
    """Cheap, deterministic check. Runs before the planner, costs microseconds."""
    if not message:
        return IntentVerdict(allowed=True)
    for pattern in _HARVEST_PATTERNS:
        if pattern.search(message):
            return IntentVerdict(
                allowed=False,
                reason="bulk personal-data enumeration",
                refusal=REFUSAL,
            )
    return IntentVerdict(allowed=True)
