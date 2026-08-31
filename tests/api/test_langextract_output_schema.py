"""Static guards for the LangExtract output schema (pipeline/langextract_examples).

``LANGEXTRACT_OUTPUT_SCHEMA=1`` sends a HAND-WRITTEN JSON schema instead of the
example-derived one. That is the whole point — an example-derived schema
suppresses every attr no example demonstrates, which is how ``hobli`` silently
never appeared — but it means the schema no longer follows the guide
automatically. So ``ENTITY_ATTR_NAMES`` and the guide's ``attrs:`` prose have to
be held to each other here: an attr added to one and not the other is either
declared-but-unconstrainable or constrained-but-undocumented.

Like its sibling static guards, everything is parsed via ast/regex — no
``langextract`` import (dep is local/offline only, absent in CI).
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_SRC = _ROOT / "pipeline" / "langextract_examples.py"
_KINDS_JSON = _ROOT / "pipeline" / "lookups" / "identifier_kinds.json"

# Attrs the guide declares in prose that the schema table intentionally omits,
# or vice versa. Empty by design: the two must agree. An entry here needs the
# same justification an EXEMPT entry in test_langextract_prompt_coverage needs.
SCHEMA_ONLY: dict = {}


def _module_constant(name: str):
    """Literal-eval a module-level constant without importing the module."""
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = ([node.target] if isinstance(node, ast.AnnAssign)
                       else node.targets)
            for t in targets:
                if isinstance(t, ast.Name) and t.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {_SRC.name}")


def _declared_attrs() -> dict[str, set[str]]:
    """{class: {attr, ...}} parsed from the guide prose, via the sibling test."""
    import test_langextract_prompt_coverage as coverage  # same test dir
    return coverage._declared_attrs()


def test_schema_table_covers_every_guide_attr():
    """Every attr the guide declares is constrainable."""
    table = _module_constant("ENTITY_ATTR_NAMES")
    missing = []
    for cls, attrs in _declared_attrs().items():
        have = set(table.get(cls, ()))
        for a in sorted(attrs - have):
            if a not in SCHEMA_ONLY:
                missing.append(f"{cls}.{a}")
    assert not missing, (
        "guide-declared attrs absent from ENTITY_ATTR_NAMES — under "
        "LANGEXTRACT_OUTPUT_SCHEMA=1 the model cannot emit them at all: "
        f"{missing}")


def test_schema_table_declares_nothing_the_guide_does_not():
    """No attr is constrained that the prompt never asks the model for."""
    table = _module_constant("ENTITY_ATTR_NAMES")
    declared = _declared_attrs()
    extra = []
    for cls, attrs in table.items():
        known = declared.get(cls, set())
        for a in attrs:
            # lot_index is a guide-wide convention ("tag every per-lot entity
            # with lot_index=N"); the prose parser also drops it from `extras`,
            # whose attrs: list is trailed by explanatory text.
            if a in known or a == "lot_index":
                continue
            extra.append(f"{cls}.{a}")
    assert not extra, (
        "ENTITY_ATTR_NAMES declares attrs the guide never asks for — the model "
        f"is told nothing about them: {extra}")


def test_schema_classes_match_the_guide():
    """The schema covers exactly the guide's extraction classes.

    A class missing here cannot be emitted at all under a strict schema, and an
    extra one is dead weight in a 15-variant anyOf.
    """
    table = _module_constant("ENTITY_ATTR_NAMES")
    guide_classes = set(_declared_attrs()) | {"full_terms"}
    assert set(table) == guide_classes, (
        f"schema classes {sorted(set(table) ^ guide_classes)} differ from the "
        "guide's")


def test_identifier_kind_enum_is_the_canonical_lookup():
    """`kind` is enum-constrained from the one canonical list, not a copy."""
    src = _SRC.read_text(encoding="utf-8")
    assert "identifier_kinds.json" in src, (
        "identifier.kind must read pipeline/lookups/identifier_kinds.json — a "
        "second hand-written copy of the enum will drift")
    kinds = json.loads(_KINDS_JSON.read_text(encoding="utf-8"))["canonical"]
    assert len(kinds) == len(set(kinds)), "duplicate identifier kinds"
    import test_langextract_prompt_coverage as coverage
    assert set(kinds) == coverage._kind_enum(), (
        "identifier_kinds.json and the guide's kind enum disagree")


def test_full_terms_carries_no_lot_index():
    """full_terms is ONE notice-level span shared by every lot (see the guide)."""
    table = _module_constant("ENTITY_ATTR_NAMES")
    assert table["full_terms"] == (), (
        "full_terms must declare no attrs — a lot_index on it would invite one "
        "terms block per lot")
