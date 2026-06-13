"""
tests/api/test_prompt_budget.py
-------------------------------
Guards the size of the agent's *static prompt prefix* — the role prompt,
`modes/_shared.md`, and every tool docstring. This block is serialized into
the system prompt + tool schemas on EVERY model call (and re-sent on each
tool round-trip), so it dominates per-call input-token cost. Without an
upper bound it tends to creep back up as rules get appended.

The test is intentionally dependency-free: it reads the prompt sources with
`ast`/`pathlib` rather than importing `api.agent` (which would build a live
OpenRouter client and is stubbed out by conftest anyway). Same philosophy as
`evals/cases.py`.

If you intentionally add prompt content, bump `BUDGET_CHARS` in the same
commit — the point is that the increase is a deliberate, reviewed decision,
not an accident.
"""
from __future__ import annotations

import ast
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_PY = _REPO_ROOT / "api" / "agent.py"
_SHARED_MD = _REPO_ROOT / "modes" / "_shared.md"

# Measured static-prefix size after the de-duplication trim is ~13,194 chars
# (~3,300 tokens), down from ~14,361 pre-trim. The ceiling leaves a little
# headroom for small edits while still asserting we stay below the pre-trim
# size. Raise this deliberately (with justification) if you add prompt
# content on purpose.
#
# 2026-06: +700 for the `select_properties` tool (docstring + role rule +
# routing bullet) — keeps the UI matches panel in sync when the agent
# answers about a subset of earlier results ("top three of those") without
# a fresh search. Measured ~13,982 after trimming; ceiling at 14,200.
#
# 2026-06: +~2,300 for the deadline-alerts capability — two new tools
# (`watch_property`, `list_alerts`), role rule 7, and a routing bullet. This
# is the grounding that stops the assistant from offering tracking it can't
# do (it previously promised "set up tracking" with no backing tool), so the
# extra description earns its per-call cost. Measured ~16,483; ceiling 16,700.
BUDGET_CHARS = 16_700


def _agent_module() -> ast.Module:
    return ast.parse(_AGENT_PY.read_text(encoding="utf-8"))


def _role_prompt(mod: ast.Module) -> str:
    for node in mod.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_ROLE_PROMPT" for t in node.targets
        ):
            assert isinstance(node.value, ast.Constant), "_ROLE_PROMPT must be a literal"
            return node.value.value
    raise AssertionError("_ROLE_PROMPT not found in api/agent.py")


def _tool_docstrings(mod: ast.Module) -> dict[str, str]:
    """Docstrings of functions decorated with @agent.tool / @agent.tool_plain."""
    docs: dict[str, str] = {}
    for node in ast.walk(mod):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and any(
            "tool" in ast.dump(dec) for dec in node.decorator_list
        ):
            docs[node.name] = ast.get_docstring(node) or ""
    return docs


def test_static_prompt_prefix_under_budget():
    mod = _agent_module()
    role = _role_prompt(mod)
    shared = _SHARED_MD.read_text(encoding="utf-8")
    tool_docs = _tool_docstrings(mod)

    # Sanity: the prefix is actually assembled from these pieces. A tool that
    # loses its docstring (or a rename that drops it from the decorated set)
    # should be noticed here, not silently shrink the "budget".
    assert "search_auctions" in tool_docs, "search_auctions tool not found"
    assert len(tool_docs) >= 9, f"expected >=9 tools, found {sorted(tool_docs)}"
    assert all(tool_docs.values()), (
        "every tool needs a docstring (it IS the tool description sent to the "
        f"model): missing for {[n for n, d in tool_docs.items() if not d]}"
    )

    total = len(role) + len(shared) + sum(len(d) for d in tool_docs.values())
    assert total <= BUDGET_CHARS, (
        f"static prompt prefix is {total} chars (~{total // 4} tokens), over the "
        f"{BUDGET_CHARS}-char budget. This text rides on every model call. Trim "
        f"it, or bump BUDGET_CHARS deliberately with justification."
    )
