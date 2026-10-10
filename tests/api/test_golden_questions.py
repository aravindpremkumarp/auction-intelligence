"""Offline shape test for the golden-question catalogue.

The catalogue itself lives in `evals/cases.py` (the single source of truth,
shared with the live `pydantic-evals` runner). This test validates its shape so
additions stay well-formed and never reference a tool the agent under test
doesn't expose. That agent is the tiered loop (`/chat/v2`); the pydantic-ai
agent the catalogue was first written for is retired. KNOWN_TOOLS is pinned
to v2's actual tool surface by `tests/api/test_chat_v2_tools.py`.

The **live** end-to-end eval (run each question through the real loop and
score the trajectory + answer quality) is `evals/run_golden.py`, run from
`.github/workflows/golden.yml`:

    python -m evals.run_golden
"""
from __future__ import annotations

from evals.cases import EXPECTED_INTENTS, GOLDEN, KNOWN_TOOLS


def test_catalogue_well_formed() -> None:
    """Validates the catalogue structure so additions stay consistent."""
    assert len(GOLDEN) >= 55
    intents = {c.intent for c in GOLDEN}
    assert EXPECTED_INTENTS.issubset(intents)
    # No stray intents that aren't declared — keeps the two in sync.
    assert intents == EXPECTED_INTENTS

    for c in GOLDEN:
        assert c.question.strip(), "question must be non-empty"
        if c.expect_refusal:
            # Refusal cases route through no data tool; they gate on the
            # decline lexicon instead.
            assert not c.acceptable_tools, (
                f"refusal case {c.question!r} should not list acceptable_tools"
            )
            assert c.refusal_required_any, (
                f"refusal case {c.question!r} needs a refusal_required_any lexicon"
            )
        else:
            assert c.acceptable_tools, f"{c.question!r} has no acceptable_tools"
            for t in c.acceptable_tools:
                assert t in KNOWN_TOOLS, f"unknown tool {t!r} on {c.question!r}"
        # Citation discipline is a listing-answer property; a refusal cites
        # nothing by definition.
        if c.expect_refusal:
            assert not c.expect_citations, (
                f"refusal case {c.question!r} can't expect citations"
            )

    # The citation gate must actually cover a meaningful slice of the
    # catalogue (the bulk-flag loop in evals/cases.py ran).
    assert sum(1 for c in GOLDEN if c.expect_citations) >= 20
