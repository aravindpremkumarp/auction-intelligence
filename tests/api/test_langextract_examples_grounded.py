"""Static guards for the LangExtract few-shot examples (pipeline/langextract_examples).

LangExtract grounds every extraction by locating its ``extraction_text`` as a
verbatim substring of the source — so an example whose text is NOT a substring of
its own ``*_TEXT`` silently teaches the model a bad span and loses source
grounding. These tests parse the module with :mod:`ast` (NO ``langextract``
import — that dep is local/offline only, absent in CI) and assert:

  1. every ``E(cls, text, ...)`` span is a verbatim substring of ITS example's
     text (each example is ``<NAME>_TEXT`` + ``<NAME>_EXAMPLE``, in source order);
  2. the ``full_description`` whole-block key is declared in the prompt guide and
     demonstrated in EVERY example — so the review surface always has a grounded
     whole-description span regardless of property type (land / multi-lot / flat).
"""
from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[2] / "pipeline" / "langextract_examples.py"


def _parse():
    """Return (raw_source, tree, [(lineno, name, value)] for each ``*_TEXT``)."""
    raw = _SRC.read_text(encoding="utf-8")
    tree = ast.parse(raw)
    texts = []
    for node in tree.body:
        if (isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id.endswith("_TEXT"):
                    texts.append((node.lineno, t.id, node.value.value))
    texts.sort()
    assert texts, "no *_TEXT example sources found"
    return raw, tree, texts


def _owning_text(call_lineno, texts):
    """The example a span belongs to: the nearest ``*_TEXT`` defined above it."""
    owner = None
    for lineno, name, value in texts:
        if lineno < call_lineno:
            owner = (name, value)
        else:
            break
    return owner


def _spans(tree, texts):
    """Yield (owner_name, owner_value, cls, extraction_text) for each E(...) call."""
    for call in ast.walk(tree):
        if (isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
                and call.func.id == "E"):
            owner = _owning_text(call.lineno, texts)
            assert owner, f"E(...) at line {call.lineno} precedes every *_TEXT"
            yield (owner[0], owner[1], ast.literal_eval(call.args[0]),
                   ast.literal_eval(call.args[1]))


def test_every_example_span_is_verbatim_substring():
    _, tree, texts = _parse()
    bad = [(owner, cls, text[:50])
           for owner, src, cls, text in _spans(tree, texts) if text not in src]
    assert not bad, "non-verbatim spans (break LangExtract grounding): " + str(bad)


def test_full_description_declared_and_demonstrated_in_every_example():
    raw, tree, texts = _parse()
    assert "- full_description :" in raw, "full_description missing from the prompt guide"
    examples_with_spans, with_full_desc = set(), set()
    for owner, _src, cls, _text in _spans(tree, texts):
        examples_with_spans.add(owner)
        if cls == "full_description":
            with_full_desc.add(owner)
    missing = examples_with_spans - with_full_desc
    assert not missing, f"examples missing a full_description span: {sorted(missing)}"


def test_full_terms_declared_and_demonstrated():
    """full_terms is notice-level (one block shared across lots), so unlike
    full_description it need not appear in every example — but it must be declared
    in the guide and demonstrated at least once, with no per-lot tagging."""
    raw, tree, texts = _parse()
    assert "- full_terms " in raw, "full_terms missing from the prompt guide"
    assert any(cls == "full_terms"
               for _o, _s, cls, _t in _spans(tree, texts)), \
        "no example demonstrates a full_terms span"
