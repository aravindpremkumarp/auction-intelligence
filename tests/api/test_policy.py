"""
tests/api/test_policy.py
------------------------
**v2 must actually carry the scope boundary.** v2 originally shipped with the
schema brief but no policy, and the golden eval failed it on four refusal
cases the retired pydantic-ai agent passed: litigation, market valuations, and
two "track this for me" requests whose correct answer names the Save button.
These tests fail if that regression is reintroduced.
"""
from __future__ import annotations

import pytest

from api.policy import (
    GROUNDING,
    SCOPE_BOUNDARY,
    SHARED_POLICY,
    WEB_SEARCH,
)


def _flat(text: str) -> str:
    """Rule text is hard-wrapped, so line breaks must not decide whether a
    content assertion passes."""
    return " ".join(text.split())


# ── what v2 inherits ────────────────────────────────────────────────────────

@pytest.mark.parametrize("phrase", [
    "litigation",
    "market valuations",
    "court cases",
    "ownership chains",
    "credit history",
    "NO tracking, monitoring, alerting",
    "point the user to the Save button",
])
def test_shared_policy_carries_the_scope_boundary(phrase):
    """Each of these is a boundary the golden eval checks for by wording."""
    assert phrase in _flat(SHARED_POLICY)


def test_shared_policy_carries_grounding_and_web_search():
    flat = _flat(SHARED_POLICY)
    assert "Never invent auction_ids, prices" in flat
    assert "only for OFF-graph context" in flat


def test_shared_policy_is_the_three_rules_in_order():
    """Part of v2's cached prompt prefix: a reorder or an extra rule changes
    the prefix and is billed at the full rate until the cache warms again."""
    assert SHARED_POLICY == "\n".join([GROUNDING, WEB_SEARCH, SCOPE_BOUNDARY])


# ── the regression itself ───────────────────────────────────────────────────

def test_v2_prompts_actually_include_the_policy():
    """The specific regression: v2 had the schema brief and no policy, and lost
    four refusal cases the eval covers."""
    from api.chat.v2 import prompts

    planner = prompts.PLANNER_SYSTEM.format(
        shared=prompts.shared_context(), policy=prompts.SHARED_POLICY,
        catalogue="<catalogue>")
    synth = prompts.SYNTH_SYSTEM.format(
        shared=prompts.shared_context(), policy=prompts.SHARED_POLICY)

    for name, text in (("planner", planner), ("synth", synth)):
        flat = _flat(text)
        assert "point the user to the Save button" in flat, name
        assert "market valuations" in flat, name
        assert "litigation" in flat, name
