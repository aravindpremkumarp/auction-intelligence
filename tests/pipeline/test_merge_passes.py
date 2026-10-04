"""pipeline/langextract_examples.merge_passes: merging LangExtract's extraction
passes keeps nested spans (full_description over property, location, ...)."""
from __future__ import annotations

from langextract import annotation as A
from langextract import data

import pipeline.langextract_examples as LX


def _x(cls, start, end, lot="1"):
    return data.Extraction(extraction_class=cls, extraction_text=cls,
                           char_interval=data.CharInterval(start_pos=start, end_pos=end),
                           attributes={"lot_index": lot})


def _read():
    """One pass over a lot: the description wraps everything else."""
    return [_x("property", 100, 300), _x("full_description", 97, 500),
            _x("location", 250, 300), _x("identifier", 140, 150),
            _x("extent", 165, 210), _x("boundary", 400, 420)]


def _classes(xs):
    return sorted(e.extraction_class for e in xs)


def test_an_empty_first_pass_loses_nothing_from_the_second():
    """The bug: pass 1 failed to parse, so pass 2 was merged against itself
    and every span inside the property span — the description included —
    was dropped."""
    assert _classes(LX.merge_passes([[], _read()])) == _classes(_read())


def test_a_class_the_first_pass_missed_is_taken_from_the_second():
    first = [e for e in _read() if e.extraction_class != "full_description"]
    merged = LX.merge_passes([first, _read()])
    assert _classes(merged) == _classes(_read())


def test_the_same_span_read_twice_is_kept_once():
    merged = LX.merge_passes([_read(), _read()])
    assert _classes(merged) == _classes(_read())


def test_langextract_calls_the_replacement():
    assert A._merge_non_overlapping_extractions is LX.merge_passes


def test_single_pass_is_untouched():
    assert _classes(LX.merge_passes([_read()])) == _classes(_read())
    assert LX.merge_passes([]) == []
