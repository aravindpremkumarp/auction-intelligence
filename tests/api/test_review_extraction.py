"""Unit tests for the extraction-review field shaping (api/review/extraction.py).

Covers the pure logic — grounding flag, lot_index split-out, and reviewer
correction merge — without a DB/app, the way the markdown_match tests do.
"""
from __future__ import annotations

import json

from api.review.extraction import _build_fields


def test_grounded_flag_and_lot_split():
    ej = json.dumps([
        {"id": "0", "cls": "secured_creditor", "text": "Bank of Baroda",
         "start": 10, "end": 24,
         "attrs": {"legal_basis": "SARFAESI", "lot_index": "1"}},
        {"id": "1", "cls": "location", "text": "X Village",
         "start": None, "end": None, "attrs": {"village": "X"}},
    ])
    fields = _build_fields(ej, "{}")
    f0, f1 = fields
    assert f0.grounded is True          # has a char span
    assert f0.lot_index == "1"          # lot_index pulled out of attrs
    assert "lot_index" not in f0.attrs  # and removed from the attr bag
    assert f0.attrs == {"legal_basis": "SARFAESI"}
    assert f1.grounded is False         # no span -> ungrounded
    assert f1.corrected_value is None


def test_correction_is_merged_by_field_id():
    ej = json.dumps([
        {"id": "1", "cls": "location", "text": "X Village",
         "start": None, "end": None, "attrs": {"village": "X"}},
    ])
    cj = json.dumps({"1": {"value": "Corrected Village",
                           "by": "admin@example.com", "at": "2026-06-21"}})
    (f,) = _build_fields(ej, cj)
    assert f.corrected_value == "Corrected Village"
    assert f.corrected_by == "admin@example.com"
    assert f.corrected_at == "2026-06-21"


def test_robust_to_empty_and_malformed_json():
    assert _build_fields("", "") == []
    assert _build_fields("not json", "also not json") == []
    # a missing id falls back to the positional index
    (f,) = _build_fields(json.dumps([{"cls": "borrower", "text": "Mr X"}]), "{}")
    assert f.id == "0"
    assert f.grounded is False
