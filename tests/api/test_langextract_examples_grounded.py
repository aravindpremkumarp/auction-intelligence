"""Static guards for the LangExtract few-shot examples (pipeline/langextract_examples).

LangExtract grounds every extraction by locating its ``extraction_text`` as a
verbatim substring of the source — so an example whose text is NOT a substring of
its own ``SINGLE_TEXT`` / ``MULTI_TEXT`` silently teaches the model a bad span and
loses source grounding. These tests parse the module with :mod:`ast` (NO
``langextract`` import — that dep is local/offline only, absent in CI) and assert:

  1. every ``E(cls, text, ...)`` span is a verbatim substring of its example text;
  2. the ``full_description`` whole-block key is declared in the prompt guide and
     demonstrated in BOTH examples (single + multi), so the review surface always
     has a grounded whole-description span to show.
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "pipeline" / "langextract_examples.py"


def _load():
    """Return (raw_source, ast_tree, {SINGLE_TEXT,MULTI_TEXT}, multi_text_lineno)."""
    raw = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    texts: dict[str, str] = {}
    multi_lineno = 0
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in ("SINGLE_TEXT", "MULTI_TEXT"):
                    texts[t.id] = node.value.value
                    if t.id == "MULTI_TEXT":
                        multi_lineno = node.lineno
    assert {"SINGLE_TEXT", "MULTI_TEXT"} <= texts.keys(), "source texts not found"
    return raw, tree, texts, multi_lineno


def _spans(tree, multi_lineno):
    """Yield (lineno, which, cls, extraction_text) for each E(...) call."""
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "E"):
            which = "single" if call.lineno < multi_lineno else "multi"
            yield (call.lineno, which, ast.literal_eval(call.args[0]),
                   ast.literal_eval(call.args[1]))


def test_every_example_span_is_verbatim_substring():
    _, tree, texts, multi_lineno = _load()
    src = {"single": texts["SINGLE_TEXT"], "multi": texts["MULTI_TEXT"]}
    bad = [(ln, which, cls, text[:50])
           for ln, which, cls, text in _spans(tree, multi_lineno)
           if text not in src[which]]
    assert not bad, "non-verbatim example spans (break LangExtract grounding): " + str(bad)


def test_full_description_is_declared_and_demonstrated():
    raw, tree, _, multi_lineno = _load()
    # declared in the prompt guide and listed in the module's class roster
    assert "- full_description :" in raw, "full_description missing from the prompt guide"
    # demonstrated as a grounded span in BOTH the single and multi examples
    seen = {which for _, which, cls, _ in _spans(tree, multi_lineno)
            if cls == "full_description"}
    assert seen == {"single", "multi"}, (
        f"full_description should be demonstrated in both examples, saw {seen or 'none'}")
