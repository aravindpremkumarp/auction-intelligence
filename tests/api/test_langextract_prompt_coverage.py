"""Static coverage guards for the LangExtract prompt (pipeline/langextract_examples).

Why these exist: on langextract's Gemini model_id path the response schema is
derived FROM THE EXAMPLES — an attribute key demonstrated in no example used to
be suppressed at generation time regardless of what the prose guide said (this
is how `hobli` silently never appeared). Even unconstrained, prose-only attrs
demonstrably underperform demonstrated ones. So: every attr key the guide
declares must be demonstrated by at least one example, or exempted here with a
reason. Like test_langextract_examples_grounded.py, everything is parsed via
ast/regex — no langextract import (dep is local/offline only, absent in CI).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "pipeline" / "langextract_examples.py"
_SCHEME = _ROOT / "pipeline" / "prompts" / "extract_enrichment.txt"
_KINDS_JSON = _ROOT / "pipeline" / "lookups" / "identifier_kinds.json"

# Attr keys declared in the guide but demonstrated in no example — each with a
# reason. Prose-only is acceptable for genuinely rare fields now that both
# provider paths run unconstrained (no example-derived schema suppression), but
# every entry here is a standing invitation: if a good source notice shows up,
# demonstrate it and delete the exemption.
EXEMPT: dict = {
    "liquidator":          "IBC-only; ~2 notices in corpus, neither example-worthy",
    "predecessor_entity":  "renamed/amalgamated lenders; very rare",
    "latitude":            "printed in ~3 notices only",
    "longitude":           "printed in ~3 notices only",
    "sarfaesi_stage":      "rarely stated as such",
    "landmark":            "no example source states a property landmark",
    "municipality_corporation": "no example source contains the phrase",
    "ward_no":             "no example source contains a property ward",
    "state":               "TN notices rarely restate the state",
    "carpet_area":         "no example source states carpet area",
    "super_built_up_area": "in gold fixtures only (752245) — using them would leak",
    "construction_type":   "no example source states RCC/tiled/thatched",
    "occupancy_status":    "no example source states vacant/tenanted",
    "branch_of_lot":       "multi-branch mega notices are gold fixtures (750348)",
    "chitta":              "TN e-Chitta: zero occurrences in extracted corpus so far",
    "khata":               "Karnataka khata: only gold fixture 737508 shows one",
}


def _guide_text() -> str:
    """The _LANGEXTRACT_GUIDE module constant, extracted via AST."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id == "_LANGEXTRACT_GUIDE":
                    assert isinstance(node.value, ast.Constant)
                    return node.value.value
    raise AssertionError("_LANGEXTRACT_GUIDE not found")


def _strip_parens(s: str) -> str:
    """Remove (possibly multi-line) parenthesised explanations."""
    return re.sub(r"\([^)]*\)", "", s, flags=re.S)


def _declared_attrs() -> dict[str, set[str]]:
    """{class: {attr, ...}} parsed from the guide's `attrs:` lists."""
    guide = _guide_text()
    # class blocks: "- name : ... attrs: a, b, c." up to the next "- name :"
    blocks = re.findall(
        r"^- ([a-z_]+)\s*:(.*?)(?=^- [a-z_]+\s*:|^CONVENTIONS)", guide,
        flags=re.M | re.S)
    out: dict[str, set[str]] = {}
    for cls, body in blocks:
        m = re.search(r"attrs:\s*(.*)", body, flags=re.S)
        if not m:
            continue
        attrs = {tok.strip().rstrip(".") for tok in
                 _strip_parens(m.group(1)).replace("\n", " ").split(",")}
        out[cls] = {a for a in attrs if re.fullmatch(r"[a-z][a-z0-9_]*", a)}
    assert out, "no attrs: lists parsed from the guide"
    return out


def _demonstrated() -> dict[str, set[str]]:
    """{class: {kwarg, ...}} from every E(cls, text, **attrs) call, via AST."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    out: dict[str, set[str]] = {}
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "E"):
            cls = ast.literal_eval(call.args[0])
            out.setdefault(cls, set()).update(
                kw.arg for kw in call.keywords if kw.arg)
    assert out, "no E(...) calls found"
    return out


def _kind_enum() -> set[str]:
    """The identifier-kind enum parsed from the guide's `kind (...)` list."""
    m = re.search(r"kind \(([a-z_|\s]+)\)", _guide_text())
    assert m, "identifier kind enum not found in guide"
    return {k.strip() for k in m.group(1).split("|") if k.strip()}


def test_every_declared_attr_demonstrated():
    declared = _declared_attrs()
    demonstrated = _demonstrated()
    missing = []
    for cls, attrs in declared.items():
        have = demonstrated.get(cls, set())
        for a in sorted(attrs - have):
            if a == "lot_index" or a in EXEMPT:
                continue
            missing.append(f"{cls}.{a}")
    assert not missing, (
        "guide-declared attrs demonstrated in no example (demonstrate them or "
        f"add an EXEMPT entry with a reason): {missing}")


def test_exempt_entries_are_real_and_needed():
    """EXEMPT must stay honest: every entry is declared somewhere and NOT
    already demonstrated (else the exemption is stale — delete it)."""
    declared_all = set().union(*_declared_attrs().values()) | _kind_enum()
    demonstrated_all = set().union(*_demonstrated().values())
    kind_values = _demonstrated_kind_values()
    for name, reason in EXEMPT.items():
        assert reason.strip(), f"EXEMPT entry {name} has no justification"
        assert name in declared_all, f"EXEMPT entry {name} is not declared anywhere"
        if name in _kind_enum():
            assert name not in kind_values, (
                f"EXEMPT entry {name} is already demonstrated as a kind — remove it")
        else:
            assert name not in demonstrated_all, (
                f"EXEMPT entry {name} is already demonstrated as an attr — remove it")


def _demonstrated_kind_values() -> set[str]:
    """Every kind=... literal used in the examples."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    kinds: set[str] = set()
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "E"):
            for kw in call.keywords:
                if kw.arg == "kind" and isinstance(kw.value, ast.Constant):
                    kinds.add(kw.value.value)
    return kinds


def test_identifier_kinds_consistent():
    """Guide enum == lookups/identifier_kinds.json canonical set; every
    example kind and every alias target belongs to it."""
    enum = _kind_enum()
    data = json.loads(_KINDS_JSON.read_text(encoding="utf-8"))
    assert set(data["canonical"]) == enum, (
        "identifier_kinds.json canonical set out of sync with the guide enum")
    bad_kinds = _demonstrated_kind_values() - enum
    assert not bad_kinds, f"example kind= values outside the enum: {bad_kinds}"
    bad_targets = set(data["aliases"].values()) - enum
    assert not bad_targets, f"alias targets outside the enum: {bad_targets}"


def test_catalogue_framing_present():
    """The scheme must be framed as a catalogue, not an output shape, and
    enriched_description must be explicitly out of scope."""
    guide = _guide_text()
    assert "do NOT output that JSON shape" in guide
    assert "NEVER emit" in guide  # enriched_description rule
    scheme = _SCHEME.read_text(encoding="utf-8")
    assert "FIELD CATALOGUE" in scheme
    assert "Return ONLY this JSON object" not in scheme
    assert "OUT OF SCOPE for extraction" in scheme  # enriched_description entry


def test_extras_class_declared_and_demonstrated():
    guide = _guide_text()
    assert "- extras " in guide, "extras class missing from the guide"
    demonstrated = _demonstrated()
    assert "extras" in demonstrated, "no example demonstrates an extras entity"
    assert {"key", "value"} <= demonstrated["extras"], (
        "extras demonstrations must carry key and value attrs")


# ── validator regression guards (pure python, no LLM) ────────────────────────

def _shim(cls, attrs):
    from types import SimpleNamespace
    return SimpleNamespace(extraction_class=cls, attributes=attrs,
                           char_interval=SimpleNamespace())


def test_validator_legal_basis_first_non_null_wins():
    from pipeline.validators import validate
    ents = [
        _shim("secured_creditor", {"legal_basis": "SARFAESI", "bank_name": "X"}),
        _shim("secured_creditor", {"bank_name": "branch repeat, no basis"}),
        _shim("borrower", {"role": "borrower"}),
        _shim("location", {"village": "V"}),
        _shim("auction_terms", {"reserve_price_num": "1000000"}),
        _shim("extent", {"total_area": "100 sq.ft"}),
    ]
    codes = [i["code"] for i in validate(ents, source_text="")["issues"]]
    assert "legal_basis_bad" not in codes, codes


def test_validator_kind_invalid_fires_only_for_unmappable():
    from pipeline.validators import validate
    base = [
        _shim("secured_creditor", {"legal_basis": "SARFAESI"}),
        _shim("borrower", {"role": "borrower"}),
        _shim("location", {"village": "V"}),
        _shim("auction_terms", {"reserve_price_num": "1000000"}),
        _shim("extent", {"total_area": "100"}),
    ]
    mappable = base + [_shim("identifier", {"kind": "T.S.No", "value": "1"})]
    codes = [i["code"] for i in validate(mappable, source_text="")["issues"]]
    assert "kind_invalid" not in codes, codes
    # and the normalized kind lands in present_fields
    assert "survey_old" in validate(mappable, source_text="")["fields"]

    unmappable = base + [_shim("identifier", {"kind": "shop", "value": "2"})]
    codes = [i["code"] for i in validate(unmappable, source_text="")["issues"]]
    assert "kind_invalid" in codes, codes


def test_normalize_identifier_kind_roundtrip():
    from pipeline.validators import CANONICAL_KINDS, normalize_identifier_kind
    assert normalize_identifier_kind("T.S.No") == ("survey_old", True)
    assert normalize_identifier_kind("Re Sy No") == ("survey_new", True)
    assert normalize_identifier_kind("survey_old") == ("survey_old", False)
    assert normalize_identifier_kind("shop") == ("shop", False)
    for k in CANONICAL_KINDS:  # canonical values are stable
        assert normalize_identifier_kind(k) == (k, False)


# ── per-notice lot-count prompt priming ─────────────────────────────────────
# langextract isn't importable in CI, so exec just the prompt_description_for
# function's source (extracted via ast) against a stub PROMPT_DESCRIPTION —
# same no-import discipline as the rest of this file.


# prompt_description_for now composes the portal-roster block, so its helpers
# have to come along or the exec'd copy hits a NameError.
_PROMPT_FNS = ("_roster_row", "portal_roster_block", "prompt_description_for")


def _prompt_ns():
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    wanted = [n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name in _PROMPT_FNS]
    assert len(wanted) == len(_PROMPT_FNS), "prompt helper renamed or removed"
    # MAX_ROSTER_ROWS is a module-level constant the block reads.
    consts = [n for n in tree.body
              if isinstance(n, ast.Assign)
              and any(getattr(t, "id", None) == "MAX_ROSTER_ROWS" for t in n.targets)]
    ns = {"PROMPT_DESCRIPTION": "BASE_PROMPT"}
    exec(compile(ast.Module(body=consts + wanted, type_ignores=[]), "<ast>", "exec"), ns)
    return ns


def _prompt_description_for():
    return _prompt_ns()["prompt_description_for"]


def test_lot_count_none_leaves_prompt_unchanged():
    f = _prompt_description_for()
    assert f(None) == "BASE_PROMPT"


def test_lot_count_multi_names_the_count_and_lot_index():
    f = _prompt_description_for()
    out = f(5)
    assert out.startswith("BASE_PROMPT")
    assert "EXACTLY 5" in out
    assert "lot_index 1 through 5" in out


def test_lot_count_single_says_one_lot():
    f = _prompt_description_for()
    out = f(1)
    assert out.startswith("BASE_PROMPT")
    assert "EXACTLY ONE lot" in out


def test_extract_signature_accepts_expected_lot_count():
    """extract() must expose the parameter the batch/rerun callers now pass."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "extract")
    assert "expected_lot_count" in [a.arg for a in fn.args.args]


def test_roster_block_is_appended_and_marked_reference_only():
    """The portal roster rides along with the lot-count hint, and must always
    carry its two guardrails: don't copy values, don't read it as lot order."""
    ns = _prompt_ns()
    out = ns["prompt_description_for"](2, [{"reserve": 100, "village": "X"},
                                           {"reserve": 200, "village": "Y"}])
    assert out.startswith("BASE_PROMPT")
    assert "EXACTLY 2" in out
    assert "PORTAL LISTINGS" in out
    assert "NEVER copy a value" in out
    assert "no particular order" in out


def test_no_roster_leaves_the_prompt_exactly_as_before():
    ns = _prompt_ns()
    assert ns["prompt_description_for"](None, None) == "BASE_PROMPT"
    assert ns["prompt_description_for"](3, []) == ns["prompt_description_for"](3)
