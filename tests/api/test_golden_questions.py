"""Offline shape test for the golden-question catalogue.

The catalogue itself now lives in `evals/cases.py` (the single source of truth,
shared with the live `pydantic-evals` runner). This test validates its shape so
additions stay well-formed and never reference a tool the agent doesn't expose.

It is intentionally **dependency-free**: it reads `api/agent.py` with `ast`
(same philosophy as `tests/api/test_prompt_budget.py`) rather than importing
the live agent, so the tool-registry cross-check runs without OpenRouter/Neo4j
credentials.

The **live** end-to-end eval (run each question through the real agent and score
the trajectory + answer quality) moved to `evals/run_golden.py` and runs nightly
via `.github/workflows/golden.yml`:

    python -m evals.run_golden
"""
from __future__ import annotations

import ast
from pathlib import Path

from evals.cases import EXPECTED_INTENTS, GOLDEN, KNOWN_TOOLS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_PY = _REPO_ROOT / "api" / "agent.py"


def _agent_tool_names() -> set[str]:
    """Names of functions decorated with @agent.tool / @agent.tool_plain in
    api/agent.py — the always-on, model-visible tool surface, read statically.

    Excludes `query_user_dossier`, which ships dark: it is registered with a
    conditional `agent.tool(query_user_dossier)` call (no decorator), so it
    doesn't appear here — exactly like it's absent from KNOWN_TOOLS.
    """
    mod = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(mod):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            "tool" in ast.dump(dec) for dec in node.decorator_list
        ):
            names.add(node.name)
    return names


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


def test_known_tools_match_agent() -> None:
    """KNOWN_TOOLS must equal the agent's actually-registered tool surface.

    This is the guard that would have caught the `semantic_search` rename:
    the catalogue referenced the tool's dead pre-rename name, so every semantic
    case silently failed the live trajectory gate while this offline test —
    which only checked membership in a hand-maintained KNOWN_TOOLS — stayed
    green. Cross-checking against api/agent.py closes that gap.
    """
    assert KNOWN_TOOLS == _agent_tool_names(), (
        "KNOWN_TOOLS is out of sync with the tools decorated in api/agent.py. "
        f"catalogue={sorted(KNOWN_TOOLS)} agent={sorted(_agent_tool_names())}"
    )
