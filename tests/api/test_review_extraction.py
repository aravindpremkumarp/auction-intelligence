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


def test_detail_includes_source_metadata(monkeypatch):
    """public_url/doc_type/content_type flow from the stored row to the response
    so the UI can render the original notice next to the markdown."""
    import api.review.extraction as ex
    monkeypatch.setattr(ex, "get_extraction", lambda fn: {
        "filename": fn, "markdown": "x", "extraction_json": "[]",
        "corrections_json": "{}", "status": "pending",
        "verified_by": None, "verified_at": None,
        "public_url": "https://r2.example/notices/A1/n.jpg",
        "doc_type": "image", "content_type": "image/jpeg"})
    out = ex.extraction_detail("n.jpg", None)  # _admin unused past Depends
    assert out.public_url == "https://r2.example/notices/A1/n.jpg"
    assert out.doc_type == "image"
    assert out.content_type == "image/jpeg"
    # absent props (older Documents) must stay optional
    monkeypatch.setattr(ex, "get_extraction", lambda fn: {
        "filename": fn, "markdown": "x", "extraction_json": "[]",
        "corrections_json": "{}", "status": "pending",
        "verified_by": None, "verified_at": None})
    out = ex.extraction_detail("n.jpg", None)
    assert out.public_url is None and out.doc_type is None


def test_queue_defaults_to_recent_and_passes_extraction_at(monkeypatch):
    """The queue endpoint defaults to newest-first sorting and surfaces each
    document's extraction_at so the UI can group a freshly re-run batch."""
    import api.review.extraction as ex
    seen = {}

    def fake_list(status, limit, sort, score_min=None, score_max=None):
        seen["sort"] = sort
        seen["score_min"] = score_min
        seen["score_max"] = score_max
        return [
            {"filename": "b.pdf", "status": "pending", "score": 72,
             "extraction_at": "2026-07-08T10:00:00Z", "extraction_batch": 7,
             "extraction_json": json.dumps([{"id": "0", "start": 1},
                                            {"id": "1", "start": None}])},
            {"filename": "a.pdf", "status": "verified", "score": None,
             "extraction_at": None, "extraction_batch": None,
             "extraction_json": "[]"},
        ]

    monkeypatch.setattr(ex, "list_extraction_queue", fake_list)
    out = ex.extraction_queue(status=None, limit=200, sort="recent",
                              score_min=None, score_max=None, _admin=None)
    assert seen["sort"] == "recent"                 # default forwarded to the query
    assert out.total == 2
    r0, r1 = out.rows
    assert r0.filename == "b.pdf"
    assert r0.extraction_at == "2026-07-08T10:00:00Z"
    assert r0.extraction_batch == 7                 # batch tag flows to the UI
    assert r0.n_fields == 2 and r0.n_ungrounded == 1
    assert r0.score == 72                            # score flows to the UI
    assert r1.extraction_at is None                 # untracked rows stay optional
    assert r1.extraction_batch is None
    assert r1.score is None                          # unscored rows stay optional


def test_queue_honours_name_sort(monkeypatch):
    import api.review.extraction as ex
    seen = {}
    monkeypatch.setattr(ex, "list_extraction_queue",
                        lambda status, limit, sort, score_min=None, score_max=None:
                            seen.update(sort=sort) or [])
    ex.extraction_queue(status=None, limit=200, sort="name",
                        score_min=None, score_max=None, _admin=None)
    assert seen["sort"] == "name"


def test_queue_forwards_score_bounds(monkeypatch):
    """score_min/score_max on the endpoint reach list_extraction_queue unchanged,
    so the review UI can filter the queue by extraction quality score."""
    import api.review.extraction as ex
    seen = {}
    monkeypatch.setattr(ex, "list_extraction_queue",
                        lambda status, limit, sort, score_min=None, score_max=None:
                            seen.update(score_min=score_min, score_max=score_max) or [])
    ex.extraction_queue(status=None, limit=200, sort="recent",
                        score_min=40.0, score_max=79.0, _admin=None)
    assert seen["score_min"] == 40.0
    assert seen["score_max"] == 79.0


def test_queue_order_clause_selects_recent_vs_name():
    """list_extraction_queue builds a newest-first ORDER BY for 'recent' and an
    alphabetical one for 'name' — verified via the Cypher handed to the driver."""
    import api.review.extraction as ex
    captured = {}

    def fake_run(cypher, params, **kw):
        captured["cypher"] = cypher
        return []

    orig = ex.run_read_query
    try:
        ex.run_read_query = fake_run
        ex.list_extraction_queue(None, 200, "recent")
        assert "d.extraction_at DESC" in captured["cypher"]
        assert "d.extraction_batch" in captured["cypher"]   # batch tie-break + selected
        ex.list_extraction_queue(None, 200, "name")
        assert "ORDER BY d.filename" in captured["cypher"]
        assert "extraction_at DESC" not in captured["cypher"]
    finally:
        ex.run_read_query = orig


def test_queue_score_bounds_add_where_clauses_and_params():
    """score_min/score_max, when given, add a WHERE bound on d.extraction_score
    and are passed through as query params — omitted bounds add no clause."""
    import api.review.extraction as ex
    captured = {}

    def fake_run(cypher, params, **kw):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    orig = ex.run_read_query
    try:
        ex.run_read_query = fake_run
        ex.list_extraction_queue(None, 200, "recent")
        assert "d.extraction_score >=" not in captured["cypher"]
        assert "d.extraction_score <=" not in captured["cypher"]
        ex.list_extraction_queue(None, 200, "recent", score_min=40.0, score_max=79.0)
        assert "d.extraction_score >= $score_min" in captured["cypher"]
        assert "d.extraction_score <= $score_max" in captured["cypher"]
        assert captured["params"]["score_min"] == 40.0
        assert captured["params"]["score_max"] == 79.0
    finally:
        ex.run_read_query = orig


def test_next_batch_increments_from_max(monkeypatch):
    """load_extractions._next_batch returns max(existing)+1, or 1 when none exist."""
    import pipeline.load_extractions as le
    monkeypatch.setattr(le, "run_read_query", lambda *a, **k: [{"m": 6}])
    assert le._next_batch() == 7
    monkeypatch.setattr(le, "run_read_query", lambda *a, **k: [{"m": None}])
    assert le._next_batch() == 1                     # first-ever run
    monkeypatch.setattr(le, "run_read_query", lambda *a, **k: [])
    assert le._next_batch() == 1                     # empty result


def test_robust_to_empty_and_malformed_json():
    assert _build_fields("", "") == []
    assert _build_fields("not json", "also not json") == []
    # a missing id falls back to the positional index
    (f,) = _build_fields(json.dumps([{"cls": "borrower", "text": "Mr X"}]), "{}")
    assert f.id == "0"
    assert f.grounded is False


# ── corrections -> gold wiring (evals/export_review_gold.py) ──────────────────
def test_reviewer_correction_flows_into_exported_gold():
    from evals.export_review_gold import _records_from_stored, _gold_fields
    from evals.langextract_eval import flatten_records
    ents = json.dumps([
        {"id": "0", "cls": "secured_creditor", "text": "Canara Bank",
         "attrs": {"legal_basis": "SARFAESI", "bank_name": "Canara Bank"}},
        {"id": "1", "cls": "borrower", "text": "Komla SJ", "attrs": {"lot_index": "1"}},
        {"id": "2", "cls": "auction_terms", "text": "Rs.1,34,00,000",
         "attrs": {"reserve_price_num": "13400000"}},
        {"id": "3", "cls": "identifier", "text": "Khata no 1394",
         "attrs": {"kind": "khata", "value": "1394"}},
    ])
    corrections = json.dumps({"1": {"value": "Komala SJ", "by": "a@b.com"}})
    flat = flatten_records(_records_from_stored(ents, corrections))
    fields, identifiers = _gold_fields(flat)
    assert fields["borrower_primary"] == "Komala SJ"     # correction won
    assert fields["reserve_price_num"] == 13400000
    assert fields["legal_basis"] == "SARFAESI"
    assert identifiers["khata"] == "1394"


def test_load_gold_falls_back_to_seed_without_reviewed_file():
    from evals.langextract_eval import GOLD, load_gold
    # No reviewed file present in CI -> load_gold == seed
    assert len(load_gold()) >= len(GOLD)
