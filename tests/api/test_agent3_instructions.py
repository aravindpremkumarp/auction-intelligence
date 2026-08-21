"""
tests/api/test_agent3_instructions.py
--------------------------------------
Guards against prompt drift for `api/agent3/instructions.md` and its
skills — the same problem `tests/api/test_mode_files.py` and
`test_prompt_budget.py` guard for the v1 mode files, applied to agent3.

Nothing type-checks a markdown prompt. Twice already in this repo a mode
file has named a tool that didn't exist (`compare.md` once instructed a
`score_auction` tool before it was built, then kept instructing it after
removal). This file catches the agent3 equivalent: a skill or the core
prompt citing a tool that isn't in `api/agent3/`, or quoting an enum value
/ conversion factor that no longer matches the live source.

Dependency-free by design: reads files with `pathlib`, extracts tool names
with `ast` (not a live import — no NEO4J_* env needed), and imports only
`api.agent3.enums` and `pipeline.measures` directly, both pure stdlib with
no Neo4j/FastAPI dependency.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from api.agent3 import enums
from pipeline.measures import UNITS

_REPO_ROOT = Path(__file__).resolve().parents[2]
_AGENT3_DIR = _REPO_ROOT / "api" / "agent3"
_INSTRUCTIONS = _AGENT3_DIR / "instructions.md"
_SKILLS_DIR = _AGENT3_DIR / "skills"

# Backticked `name(`-shaped references — how the prompt cites a tool.
_TOOL_CALL_RE = re.compile(r"`([a-z_][a-z0-9_]*)\(")
# "load the `<name>` skill" / "load the <name> skill" — how the prompt cites
# a skill by name (both backticked and plain forms appear in the routing
# table and the skill files' own headers). \s+ (not a literal space) because
# instructions.md wraps this phrase across a line break in the routing list.
_SKILL_REF_RE = re.compile(r"load the\s+`?([a-z][a-z0-9_-]*)`?\s+skill")

#: The measured size after the 2026-08 authoring pass. The design doc's
#: illustrative target was ~600 tokens (~2,400 chars); this measured a
#: little over that to keep all four hard rules and the full routing table
#: intact. Still a ~74% cut from modes/_shared.md's ~2,600 tokens. Bump
#: this deliberately (with a comment, like test_prompt_budget.py) if you
#: add content on purpose.
BUDGET_CHARS = 3000


def _tool_functions() -> set[str]:
    """Public tool function names actually defined in api/agent3/*.py.

    AST-parsed rather than imported: find_properties.py etc. import
    api.neo4j_client at module scope, which needs NEO4J_* env vars this
    test shouldn't depend on.
    """
    names: set[str] = set()
    for path in _AGENT3_DIR.glob("*.py"):
        mod = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(mod):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("_"):
                    names.add(node.name)
    return names


def _skill_dirs() -> set[str]:
    return {p.parent.name for p in _SKILLS_DIR.glob("*/SKILL.md")}


def _prompt_sources() -> dict[str, str]:
    sources = {"instructions.md": _INSTRUCTIONS.read_text(encoding="utf-8")}
    for path in _SKILLS_DIR.glob("*/SKILL.md"):
        sources[f"skills/{path.parent.name}/SKILL.md"] = path.read_text(encoding="utf-8")
    return sources


def test_instructions_file_exists_and_is_nonempty():
    assert _INSTRUCTIONS.exists()
    assert len(_INSTRUCTIONS.read_text(encoding="utf-8").strip()) > 100


def test_instructions_stays_within_budget():
    size = len(_INSTRUCTIONS.read_text(encoding="utf-8"))
    assert size <= BUDGET_CHARS, (
        f"instructions.md is {size} chars, budget is {BUDGET_CHARS}. If this "
        f"growth is deliberate, raise BUDGET_CHARS in this commit with a "
        f"comment saying why — the point is that growth is a reviewed "
        f"decision, not drift.")


def test_every_tool_cited_in_prompts_actually_exists():
    """A prompt instructing a tool that isn't built is worse than no
    instruction — the agent will try to call it and get a hard failure, or
    (with a lenient harness) silently hallucinate around the gap."""
    tools = _tool_functions()
    offenders: list[str] = []
    for name, text in _prompt_sources().items():
        for m in _TOOL_CALL_RE.finditer(text):
            called = m.group(1)
            # Only flag names that plausibly ARE tool citations: skip
            # obvious non-tool code-ish snippets (Cypher/date functions,
            # etc.) by requiring the name look like our tool naming
            # convention (snake_case, no single-letter/common-builtin noise)
            # and NOT already a known tool.
            if called in tools:
                continue
            if called in _KNOWN_NON_TOOL_CALLS:
                continue
            offenders.append(f"{name} cites `{called}(...)`, which is not a "
                             f"function in api/agent3/*.py")
    assert not offenders, "\n".join(offenders)


#: Backticked `name(` patterns in the prompts that are NOT tool citations —
#: recorded explicitly so the scan above stays a real check, not a
#: allow-everything rubber stamp.
_KNOWN_NON_TOOL_CALLS: set[str] = set()


def test_every_skill_the_instructions_route_to_has_a_file():
    """'load the X skill' in instructions.md with no api/agent3/skills/X/
    is an instruction to load nothing."""
    referenced = set(_SKILL_REF_RE.findall(_INSTRUCTIONS.read_text(encoding="utf-8")))
    assert referenced, "expected at least one 'load the ... skill' reference"
    have = _skill_dirs()
    missing = referenced - have
    assert not missing, f"instructions.md routes to skills with no file: {missing}"


def test_every_skill_file_is_reachable_from_instructions():
    """A skill nothing routes to never loads — dead weight sitting in the
    repo, not a cost on any prompt, but also never doing its job."""
    referenced = set(_SKILL_REF_RE.findall(_INSTRUCTIONS.read_text(encoding="utf-8")))
    orphaned = _skill_dirs() - referenced
    assert not orphaned, f"skills with no routing reference: {orphaned}"


def test_possession_values_in_skills_match_the_live_enum():
    text = (_SKILLS_DIR / "diligence" / "SKILL.md").read_text(encoding="utf-8")
    for value in enums.POSSESSION_TYPES:
        assert f"**{value}**" in text, (
            f"diligence skill does not define possession value {value!r} — "
            f"POSSESSION_TYPES in enums.py has drifted from the skill text")


def test_identifier_kinds_in_skill_match_the_live_enum():
    text = (_SKILLS_DIR / "identifiers" / "SKILL.md").read_text(encoding="utf-8")
    missing = [k for k in enums.IDENTIFIER_KINDS if f"`{k}`" not in text]
    assert not missing, (
        f"identifiers skill does not document kind(s) {missing} — "
        f"IDENTIFIER_KINDS in enums.py has drifted from the skill text")


def test_unit_conversion_table_in_extent_skill_matches_the_pipeline():
    """The skill's conversion table is prose, not code — this is the only
    thing stopping it from silently disagreeing with the pipeline's actual
    normalisation in pipeline/measures.py, which is what sqft_norm on every
    Measurement node was actually computed with."""
    text = (_SKILLS_DIR / "extent" / "SKILL.md").read_text(encoding="utf-8")
    for unit, factor in UNITS.items():
        if unit == "sq_ft":
            continue  # the 1:1 identity row isn't worth a distinct-looking check
        # Match the factor either as an integer ("43,560") or with the
        # pipeline's own decimal precision ("10.76391").
        as_int = f"{factor:,.0f}"
        as_given = str(factor)
        assert as_int in text or as_given in text or f"{factor:,}" in text, (
            f"extent skill's conversion table doesn't show {unit} = {factor} "
            f"sqft — pipeline/measures.py::UNITS has drifted from the skill")


def test_sqft_band_in_extent_skill_matches_the_tool_constants():
    """common.py::SQFT_FLOOR/SQFT_CEIL are the actual clamp find_properties
    and get_property apply. If those constants ever change, the skill's
    explanation of the band should change with them."""
    from api.agent3.common import SQFT_CEIL, SQFT_FLOOR

    text = (_SKILLS_DIR / "extent" / "SKILL.md").read_text(encoding="utf-8")
    assert f"{int(SQFT_FLOOR)}" in text
    assert f"{int(SQFT_CEIL):,}" in text


def test_no_reference_to_tools_not_yet_built():
    """Tools the design doc specs for later steps (benchmark_price,
    reauction_history, run_cypher) must not appear as if callable yet —
    that is the exact drift test_mode_files.py was written to catch."""
    not_yet_built = {"benchmark_price", "reauction_history", "run_cypher"}
    offenders = []
    for name, text in _prompt_sources().items():
        for tool in not_yet_built:
            if f"`{tool}(" in text or f"`{tool}`" in text:
                offenders.append(f"{name} references unbuilt tool {tool!r}")
    assert not offenders, "\n".join(offenders)
