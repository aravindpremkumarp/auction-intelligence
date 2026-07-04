"""
tests/api/test_mode_files.py
----------------------------
Guards against mode-file drift. Mode files are prompts the agent obeys, but
nothing type-checks them — twice now they've named tools that don't exist:
`compare.md` once instructed a `score_auction` tool before it was built, and
kept instructing it after it was removed. Tool docstrings are guarded by
test_prompt_budget; this gives the active mode files (and the shared
context + role prompt) the same protection.

Dependency-free by design (ast + regex, no api imports), like
test_prompt_budget.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT_PY = _REPO_ROOT / "api" / "agent.py"
_ROUTER_PY = _REPO_ROOT / "api" / "chat" / "router.py"
_MODES_DIR = _REPO_ROOT / "modes"

# Tools removed from the chat agent during the 2026-07 surface trim. A mode
# file (or the shared context / role prompt) mentioning one of these is
# instructing a tool that no longer exists.
RETIRED_TOOLS = {
    "match_pasted_listing",
    "upcoming_auctions",
    "borrower_lookup",
    "list_distinct",
    "score_auction",
    "watch_property",
    "list_alerts",
    "select_properties",  # replaced by the router's programmatic panel sync
}

# Backticked `name(...)`-shaped references — the way mode files cite tools.
_TOOL_CALL_RE = re.compile(r"`([a-z_][a-z0-9_]*)\(")


def _current_tools() -> set[str]:
    """Names of functions decorated @agent.tool / @agent.tool_plain, plus
    conditionally-registered tools (query_user_dossier)."""
    mod = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
    tools = {
        n.name
        for n in ast.walk(mod)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any("tool" in ast.dump(d) for d in n.decorator_list)
    }
    tools.add("query_user_dossier")  # registered when dossiers_enabled()
    return tools


def _active_mode_files() -> list[Path]:
    """modes/*.md excluding the _archive/ parking lot."""
    return sorted(p for p in _MODES_DIR.glob("*.md"))


def _role_prompt() -> str:
    mod = ast.parse(_AGENT_PY.read_text(encoding="utf-8"))
    for node in mod.body:
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "_ROLE_PROMPT" for t in node.targets
        ):
            return node.value.value
    raise AssertionError("_ROLE_PROMPT not found")


def test_no_retired_tool_named_in_active_prompts():
    offenders: list[str] = []
    sources = {p.name: p.read_text(encoding="utf-8") for p in _active_mode_files()}
    sources["_ROLE_PROMPT"] = _role_prompt()
    for name, text in sources.items():
        for tool in RETIRED_TOOLS:
            if tool in text:
                offenders.append(f"{name} mentions retired tool {tool!r}")
    assert not offenders, (
        "Prompt text instructs tools that were removed from the agent:\n  "
        + "\n  ".join(offenders)
    )


def test_every_tool_call_in_mode_files_exists():
    current = _current_tools()
    offenders: list[str] = []
    for path in _active_mode_files():
        for m in _TOOL_CALL_RE.finditer(path.read_text(encoding="utf-8")):
            name = m.group(1)
            # Only flag names that LOOK like tool citations: either a known
            # tool or a known retired one; other `foo(...)` snippets (Cypher
            # functions like `datetime()`, `count()`) are fine.
            if name not in current and name in RETIRED_TOOLS:
                offenders.append(f"{path.name} calls removed tool {name!r}")
    assert not offenders, "\n".join(offenders)


def test_registered_modes_have_files_and_vice_versa():
    """Every id in _AVAILABLE_MODES maps to a modes/<id>.md file, and every
    active mode file is registered (or is _shared.md). A file with no
    registry entry is dead weight; a registry entry with no file silently
    overlays nothing."""
    router_mod = ast.parse(_ROUTER_PY.read_text(encoding="utf-8"))
    registered: set[str] = set()
    for node in ast.walk(router_mod):
        # `_AVAILABLE_MODES: list[dict] = [...]` is an AnnAssign (single
        # .target); an unannotated assignment would be Assign (.targets).
        targets = (
            [node.target] if isinstance(node, ast.AnnAssign)
            else node.targets if isinstance(node, ast.Assign)
            else []
        )
        if any(getattr(t, "id", None) == "_AVAILABLE_MODES" for t in targets):
            if node.value is not None:
                for entry in ast.literal_eval(node.value):
                    registered.add(entry["id"])
    assert registered, "_AVAILABLE_MODES not found or empty"

    files = {p.stem for p in _active_mode_files()} - {"_shared"}
    # "ask" is the no-overlay default: registered, deliberately file-less.
    missing_files = (registered - {"ask"}) - files
    unregistered = files - registered
    assert not missing_files, f"modes registered without a file: {missing_files}"
    assert not unregistered, f"mode files not registered: {unregistered}"
