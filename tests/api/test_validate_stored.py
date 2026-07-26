"""Guard: validate_stored (the shared shim used by extract_batch.py --from-graph
and scripts/backfill_extraction_scores.py) must score persisted
Document.extraction_json dicts identically to validate() scoring the same
data as live LangExtract Extraction objects — it's a pure re-shaping, not a
different scoring path. Pure test (no langextract / API key / DB)."""
from __future__ import annotations

from types import SimpleNamespace

from pipeline.validators import validate, validate_stored


def test_validate_stored_matches_validate_on_equivalent_input():
    live = [
        SimpleNamespace(
            extraction_class="secured_creditor", extraction_text="Bank of Baroda",
            attributes={"legal_basis": "SARFAESI", "lot_index": "1"},
            char_interval=SimpleNamespace(start_pos=0, end_pos=14)),
        SimpleNamespace(
            extraction_class="location", extraction_text="X Village",
            attributes={"village": "X", "lot_index": "1"},
            char_interval=None),  # ungrounded
    ]
    stored = [
        {"id": "0", "cls": "secured_creditor", "text": "Bank of Baroda",
         "start": 0, "end": 14, "attrs": {"legal_basis": "SARFAESI", "lot_index": "1"}},
        {"id": "1", "cls": "location", "text": "X Village",
         "start": None, "end": None, "attrs": {"village": "X", "lot_index": "1"}},
    ]
    md = "Bank of Baroda ... X Village"
    r_live = validate(live, source_text=md)
    r_stored = validate_stored(stored, source_text=md)
    assert r_stored["score"] == r_live["score"]
    assert r_stored["issues"] == r_live["issues"]
    assert r_stored["fields"] == r_live["fields"]
    assert r_stored["stats"] == r_live["stats"]


def test_validate_stored_handles_empty_entities():
    """No entities at all -> same result as validate([]) — every required-field
    issue fires, none of the entity-shaped checks crash on an empty list."""
    assert validate_stored([], source_text="") == validate([], source_text="")
