"""lot_under_recall (pipeline/validators.py) trusts the reviewer's lot count.

The "S.No" marker count matches table headers and survey numbers, so on the
reviewed corpus it flagged ~940 notices whose lots were all read. When the
reviewer's count is known it is the only test; the marker count is the
fallback for a notice nobody has counted. Pure tests — no DB, no LLM.
"""
from __future__ import annotations

from pipeline.validators import validate_stored

# Six "S.No" headers, one lot read: the marker heuristic calls this short.
MD = "\n".join(f"S.No | Description | Reserve  (table {i})" for i in range(6))


def _lots(n: int) -> list[dict]:
    return [{"id": str(i), "cls": "property", "text": f"Plot {i}", "start": None,
             "end": None, "attrs": {"lot_index": str(i), "property_type": "plot"}}
            for i in range(1, n + 1)]


def _codes(report: dict) -> set[str]:
    return {i["code"] for i in report["issues"]}


def test_no_flag_when_every_counted_lot_is_read():
    report = validate_stored(_lots(1), source_text=MD, expected_lot_count=1)
    assert "lot_under_recall" not in _codes(report)


def test_flag_when_fewer_lots_than_the_reviewer_counted():
    report = validate_stored(_lots(2), source_text=MD, expected_lot_count=3)
    hit = [i for i in report["issues"] if i["code"] == "lot_under_recall"]
    assert hit and "reviewer counted 3" in hit[0]["msg"]


def test_extra_lots_are_not_under_recall():
    report = validate_stored(_lots(4), source_text=MD, expected_lot_count=3)
    assert "lot_under_recall" not in _codes(report)


def test_marker_heuristic_is_the_fallback_without_a_count():
    report = validate_stored(_lots(1), source_text=MD)
    assert "lot_under_recall" in _codes(report)


def test_a_count_with_no_source_text_still_applies():
    report = validate_stored(_lots(1), source_text="", expected_lot_count=2)
    assert "lot_under_recall" in _codes(report)
