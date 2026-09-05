"""Unit tests for the class-alias correction in `_entities`.

An unrecognised `cls` is not an error anywhere downstream — the dispatch in
`apply_extractions` and `promote_extractions` is an if/elif chain with no
fallback, so the entity matches nothing and vanishes without a word. That makes
a one-letter typo a silent data loss, which is what these cover.
"""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.load_extractions import _entities


def _res(cls: str, text: str = "Mr/Mrs Bharathaselvan T", attrs=None):
    return SimpleNamespace(extractions=[SimpleNamespace(
        extraction_class=cls, extraction_text=text,
        attributes=attrs or {}, char_interval=SimpleNamespace(
            start_pos=10, end_pos=20))])


def test_a_misspelled_class_is_corrected():
    out = _entities(_res("borower"))
    assert out[0]["cls"] == "borrower"


def test_the_original_spelling_is_kept_beside_it():
    """Same contract as the identifier-kind normalisation: correct the value,
    never lose what the model actually said."""
    out = _entities(_res("borower"))
    assert out[0]["attrs"]["cls_raw"] == "borower"


def test_a_correct_class_is_untouched_and_gains_no_marker():
    out = _entities(_res("borrower"))
    assert out[0]["cls"] == "borrower"
    assert "cls_raw" not in out[0]["attrs"]


def test_a_class_outside_the_schema_is_not_guessed_at():
    """`extraction_text` carries real content under a label whose intended
    class cannot be recovered. Renaming it would be a guess with a span and a
    borrower name on the end of it, so it is left exactly as emitted."""
    out = _entities(_res("extraction_text", text="Indian Overseas Bank"))
    assert out[0]["cls"] == "extraction_text"
    assert "cls_raw" not in out[0]["attrs"]


def test_the_span_and_text_survive_the_correction():
    out = _entities(_res("borower"))
    assert (out[0]["start"], out[0]["end"]) == (10, 20)
    assert out[0]["text"] == "Mr/Mrs Bharathaselvan T"


def test_identifier_normalisation_still_runs_after_the_alias_step():
    """The two corrections share a variable; the kind fix must still see the
    class it keys on."""
    out = _entities(_res("identifier", text="T.S.No 45", attrs={"kind": "T.S.No"}))
    assert out[0]["cls"] == "identifier"
    assert out[0]["attrs"]["kind_raw"] == "T.S.No"
