"""
tests/api/test_policy.py
------------------------
Two guarantees about `api/policy.py`, both learned the hard way.

**v1's role prompt must stay byte-identical.** It is the stable leading prefix
of every /chat call, and the provider bills a changed prefix at the full rate
instead of the cache-hit rate. Refactoring it into constants is only safe if
the composed string is exactly what it was.

**v2 must actually carry the scope boundary.** v2 originally shipped with the
schema brief but no policy, and the golden eval failed it on four refusal
cases v1 passes: litigation, market valuations, and two "track this for me"
requests whose correct answer names the Save button. These tests fail if that
regression is reintroduced.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from api.policy import (
    FORMATTING,
    GROUNDING,
    INTRO,
    ROLE_PROMPT,
    SCOPE_BOUNDARY,
    SHARED_POLICY,
    TOOL_PREFERENCE,
    WEB_SEARCH,
)


def _flat(text: str) -> str:
    """Rule text is hard-wrapped, so line breaks must not decide whether a
    content assertion passes."""
    return " ".join(text.split())


# ── v1's prompt prefix ──────────────────────────────────────────────────────

def test_role_prompt_is_composed_from_the_rules_in_order():
    assert ROLE_PROMPT == "\n".join([
        INTRO, GROUNDING, TOOL_PREFERENCE, WEB_SEARCH, SCOPE_BOUNDARY, FORMATTING,
    ])


def test_agent_keeps_no_copy_of_the_prompt():
    """Asserted against the source, not the import: tests/api/conftest.py
    stubs `api.agent` so api.main loads without credentials."""
    source = (Path(__file__).resolve().parents[2] / "api" / "agent.py").read_text()

    assert "_ROLE_PROMPT = ROLE_PROMPT" in source
    assert "from api.policy import ROLE_PROMPT" in source
    assert '_ROLE_PROMPT = """' not in source, "agent.py grew its own copy again"


def test_rules_are_numbered_one_to_five_in_order():
    """The composed prompt must still read as a numbered list — a reordering
    would renumber the rules the answers refer to ("Rule 4")."""
    for n, rule in enumerate(
        [GROUNDING, TOOL_PREFERENCE, WEB_SEARCH, SCOPE_BOUNDARY, FORMATTING], start=1
    ):
        assert rule.startswith(f"{n}. "), f"rule {n} is out of order"


def test_role_prompt_size_is_stable():
    """A guard on the cache-keyed prefix. If this trips, the change was
    intentional or it was not — either way it should be noticed."""
    assert 2000 <= len(ROLE_PROMPT) <= 2300, len(ROLE_PROMPT)


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


def test_shared_policy_omits_the_v1_only_rules():
    """`TOOL_PREFERENCE` describes loading a deferred capability, which is a v1
    mechanism (v2 routes to tier 3 through the planner's typed field), and
    `FORMATTING` is v1's markdown house style — v2's shape comes from the
    Recommendation schema. Carrying either would be prompt tokens spent on
    instructions that do not apply."""
    assert TOOL_PREFERENCE not in SHARED_POLICY
    assert FORMATTING not in SHARED_POLICY
    assert INTRO not in SHARED_POLICY


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


def test_v2_and_v1_read_the_same_boundary_text():
    """Not a paraphrase — the identical string. A paraphrase is how the two
    endpoints drift into answering the same question differently."""
    assert SCOPE_BOUNDARY in ROLE_PROMPT
    assert SCOPE_BOUNDARY in SHARED_POLICY
