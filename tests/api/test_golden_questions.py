"""Offline shape test for the golden-question catalogue.

The catalogue itself now lives in `evals/cases.py` (the single source of truth,
shared with the live `pydantic-evals` runner). This test validates its shape so
additions stay well-formed and never reference a tool the agent doesn't expose.

The **live** end-to-end eval (run each question through the real agent and score
the trajectory + answer quality) moved to `evals/run_golden.py` and runs nightly
via `.github/workflows/golden.yml`:

    python -m evals.run_golden
"""
from __future__ import annotations

from evals.cases import EXPECTED_INTENTS, GOLDEN, KNOWN_TOOLS


def test_catalogue_well_formed() -> None:
    """Validates the catalogue structure so additions stay consistent."""
    assert len(GOLDEN) >= 40
    intents = {c.intent for c in GOLDEN}
    assert EXPECTED_INTENTS.issubset(intents)

    for c in GOLDEN:
        assert c.question.strip(), "question must be non-empty"
        assert c.acceptable_tools, f"{c.question!r} has no acceptable_tools"
        for t in c.acceptable_tools:
            assert t in KNOWN_TOOLS, f"unknown tool {t!r} on {c.question!r}"
