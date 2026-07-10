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
#
# 2026-06: the private-dossier Q&A capability (the `query_user_dossier` tool +
# a rule-4 exception) briefly pushed this to ~18,069 (ceiling 18,300). The
# feature now ships dark behind DOSSIERS_ENABLED: the tool is registered
# conditionally (no @agent.tool decorator, so it drops out of this static scan)
# and its prompt fragment lives in `_DOSSIER_RULE_EXCEPTION`, appended to the
# role prompt only when the flag is on. So the always-sent prefix measured here
# is back to the pre-dossier baseline (~16,508); ceiling 16,700. When the
# feature is re-enabled for launch, fold the tool docstring + exception back
# into the measure and raise this ceiling in the same commit.
#
# 2026-06: +~557 for the `score_auction` tool (405-char docstring) plus a
# routing line in modes/_shared.md. Exposes the existing 10-dimension scorer
# (scoring/auction_scorer.py) to the agent so the compare/report modes can
# actually score, instead of instructing a tool that didn't exist. Measured
# ~17,065; ceiling 17,300.
#
# 2026-07: +~674 for two round-trip-reduction edits — a tighter rule-5 /
# routing bullet telling the model to SKIP a redundant `select_properties`
# when this turn's search/detail already put that set in the panel, and a
# "batch independent tool calls" note in modes/_shared.md so parallelizable
# lookups (multi-city compares/counts) collapse into one round-trip. The added
# ~168 tokens/call buy the removal of whole redundant round-trips (~3,300
# tokens of re-sent prefix each), so this earns its per-call cost. Measured
# ~17,974; ceiling 18,200.
#
# 2026-07 (anti-flail trim): NET −163 while adding the zero-result protocol.
# Telemetry showed the old "on zero results, loosen before declaring no
# matches" rule driving 26-tool-call retry loops (paraphrased semantic_search
# ×4, per-area splits of an already-tried list filter, a 10× detail fan-out).
# Replaced with a bounded protocol (read the tool's `hint`, max two follow-up
# variations, one `select_properties` call for multi-id rows) and de-duplicated
# the rules that appeared 3-4×: old role rule 5 (select_properties) and rule 7
# (alerts) now live only in docstrings + one routing bullet; the property-type
# synonym map moved from the search_auctions docstring into the _shared enums
# section. Review pass restored three load-bearing clauses the trim had
# dropped ("name the closest tool", "withdrawal" in the no-such-alerts list,
# the compare-mode exception to the no-detail-loop rule). Measured ~17,811;
# ceiling 17,900.
# 2026-07 (tool-surface reduction): the chat agent dropped from 12 tools to 7
# — match_pasted_listing removed; upcoming_auctions, borrower_lookup, and
# list_distinct folded into search_auctions (deadline_within_days, borrower,
# group_by); score_auction, watch_property, and list_alerts removed (weak
# heuristics / duplicated the UI's Save button — rule 4 now points there).
# Measured ~15,972. Ceiling ratcheted down so the ~480 tokens/call saved
# can't silently creep back; raise deliberately as always.
#
# 2026-07 (anti-re-query on success): +~1.3k for guidance stopping the model
# from re-running a SUCCESSFUL semantic_search/internet_search with reworded
# queries (quotes/OR/case/synonyms) — telemetry showed dense-vector searches
# fired 3× per turn with near-identical rankings, and each result set is
# replayed through every later step of the turn. Added to both tool docstrings
# and a "one search per question" note in modes/_shared.md; the existing
# zero-result rules only covered the EMPTY case. One avoided re-query saves a
# whole LLM round-trip plus a re-sent ~20k-char result block, so this earns its
# per-call cost many times over. Measured ~14,615; ceiling held at 16,200.
#
# 2026-07 (pydantic-ai v2 deferred Cypher capability): NET −1,155. The raw-
# Cypher tools (run_cypher + describe_schema, behind the `cypher` capability)
# no longer serialize their docstrings into every call — they collapse to a
# one-line catalog description until the model loads them — and the Cypher-
# only blocks of modes/_shared.md (DATETIME handling, MATCH-shape rule, ~600
# chars) moved into the capability's load-time instructions. Only the Cypher
# tools are deferred: production telemetry showed raw Cypher on ~6% of turns
# (deferring wins) but internet_search on ~24% (deferring would cost more in
# per-load round-trips on Flash than the prefix saving), so internet_search
# stays always-on and its docstring still counts here. NOT captured by this
# static scan: pydantic-ai's own load_capability/search_tools schemas (a fixed
# per-call overhead) — the net per-call saving is still positive, and the many
# turns that never touch raw Cypher keep a smaller, stable cache prefix.
# Measured ~13,460; ceiling ratcheted to 13,700 so the savings can't silently
# creep back.
BUDGET_CHARS = 13_700


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


def _decorator_owner(dec: ast.expr) -> str | None:
    """Name the object a @<owner>.tool / @<owner>.tool_plain decorator hangs
    off ('agent', '_CYPHER_CAPABILITY', …); None for non-tool decorators."""
    if isinstance(dec, ast.Call):
        dec = dec.func
    if (
        isinstance(dec, ast.Attribute)
        and dec.attr in ("tool", "tool_plain")
        and isinstance(dec.value, ast.Name)
    ):
        return dec.value.id
    return None


def _tool_docstrings(mod: ast.Module) -> tuple[dict[str, str], dict[str, str]]:
    """Docstrings of decorated tools, split into (always_on, deferred).

    @agent.* tools are serialized into EVERY model call. Tools registered on
    a deferred Capability (see api/agent.py) stay out of the prompt until the
    model loads them, so only their capability's one-line catalog
    `description` (measured separately below) rides per call.
    """
    always_on: dict[str, str] = {}
    deferred: dict[str, str] = {}
    for node in ast.walk(mod):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        owners = {o for o in map(_decorator_owner, node.decorator_list) if o}
        if not owners:
            continue
        bucket = always_on if "agent" in owners else deferred
        bucket[node.name] = ast.get_docstring(node) or ""
    return always_on, deferred


def _capability_descriptions(mod: ast.Module) -> dict[str, str]:
    """`description=` literals of Capability(...) constructions — the
    one-line catalog entries pydantic-ai appends to the instructions on every
    call while the capability stays unloaded. (The `instructions=` payloads
    deliberately do NOT count: they ride only after a load.)"""
    descs: dict[str, str] = {}
    for node in ast.walk(mod):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "Capability"
        ):
            continue
        kw = {k.arg: k.value for k in node.keywords}
        cap_id = kw["id"].value if isinstance(kw.get("id"), ast.Constant) else "?"
        desc = kw.get("description")
        assert isinstance(desc, ast.Constant), (
            f"Capability {cap_id!r} needs a literal description — it is the "
            "catalog line the model routes on"
        )
        descs[cap_id] = desc.value
    return descs


def test_static_prompt_prefix_under_budget():
    mod = _agent_module()
    role = _role_prompt(mod)
    shared = _SHARED_MD.read_text(encoding="utf-8")
    tool_docs, deferred_docs = _tool_docstrings(mod)
    cap_descs = _capability_descriptions(mod)

    # Sanity: the prefix is actually assembled from these pieces. A tool that
    # loses its docstring (or a rename that drops it from the decorated set)
    # should be noticed here, not silently shrink the "budget".
    assert "search_auctions" in tool_docs, "search_auctions tool not found"
    # 4 always-on tools after the 2026-07 deferred-capability move: raw Cypher
    # (run_cypher + describe_schema) rides behind the deferred `cypher`
    # capability, out of the per-call prefix; internet_search is deliberately
    # kept always-on (used too often to defer — see api/agent.py). (The earlier
    # 12→6 surface trim is documented in the BUDGET_CHARS history above.)
    assert len(tool_docs) >= 4, f"expected >=4 always-on tools, found {sorted(tool_docs)}"
    assert "internet_search" in tool_docs, "internet_search must be always-on, not deferred"
    assert {"run_cypher", "describe_schema"} <= set(deferred_docs), (
        f"expected the Cypher tools on the deferred capability, found {sorted(deferred_docs)}"
    )
    assert set(cap_descs) == {"cypher"}, (
        f"expected only the `cypher` deferred capability, found {sorted(cap_descs)}"
    )
    assert all(tool_docs.values()) and all(deferred_docs.values()), (
        "every tool needs a docstring (it IS the tool description sent to the "
        f"model): missing for "
        f"{[n for n, d in (tool_docs | deferred_docs).items() if not d]}"
    )

    # Deferred tool docstrings don't ride per call, but their capability
    # catalog descriptions do — count those.
    total = (
        len(role)
        + len(shared)
        + sum(len(d) for d in tool_docs.values())
        + sum(len(d) for d in cap_descs.values())
    )
    assert total <= BUDGET_CHARS, (
        f"static prompt prefix is {total} chars (~{total // 4} tokens), over the "
        f"{BUDGET_CHARS}-char budget. This text rides on every model call. Trim "
        f"it, or bump BUDGET_CHARS deliberately with justification."
    )
