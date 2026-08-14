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

    def fake_list(status, limit, sort, score_min=None, score_max=None, **kw):
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
    # `total` is a separate count query now (see the row-cap test below), so it
    # is deliberately NOT asserted against len(rows) here.
    assert len(out.rows) == 2
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
                        lambda status, limit, sort, score_min=None, score_max=None, **kw:
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
                        lambda status, limit, sort, score_min=None, score_max=None, **kw:
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


# ── single-document LangExtract rerun ─────────────────────────────────────────

def _doc_row(fn):
    return {"filename": fn, "markdown": "x", "extraction_json": "[]",
            "corrections_json": "{}", "status": "pending",
            "verified_by": None, "verified_at": None}


def test_rerun_starts_worker_and_reports_running(monkeypatch):
    import api.review.extraction as ex
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _doc_row(fn))
    started = {}
    monkeypatch.setattr(ex.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: started.update(kw)})())
    ex._RERUNS.clear()
    out = ex.extraction_rerun("n.jpg", None)
    assert out.rerun_running is True
    assert out.rerun_error is None
    assert started["args"] == ("n.jpg",)
    ex._RERUNS.clear()


def test_rerun_twice_is_conflict(monkeypatch):
    import pytest
    from fastapi import HTTPException
    import api.review.extraction as ex
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _doc_row(fn))
    monkeypatch.setattr(ex.threading, "Thread",
                        lambda **kw: type("T", (), {"start": lambda s: None})())
    ex._RERUNS.clear()
    ex.extraction_rerun("n.jpg", None)
    with pytest.raises(HTTPException) as e:
        ex.extraction_rerun("n.jpg", None)
    assert e.value.status_code == 409
    ex._RERUNS.clear()


def test_rerun_unknown_doc_is_404(monkeypatch):
    import pytest
    from fastapi import HTTPException
    import api.review.extraction as ex
    monkeypatch.setattr(ex, "get_extraction", lambda fn: None)
    with pytest.raises(HTTPException) as e:
        ex.extraction_rerun("missing.jpg", None)
    assert e.value.status_code == 404


def test_rerun_worker_failure_surfaces_in_detail(monkeypatch):
    """A worker crash (e.g. langextract not installed on the web host) must
    land in rerun_error, not vanish or kill the app."""
    import api.review.extraction as ex
    monkeypatch.setattr(ex, "run_read_query", lambda *a, **k: [
        {"filename": "n.jpg", "md": "", "notice_type": None}])
    ex._RERUNS.clear()
    ex._RERUNS["n.jpg"] = {"status": "running"}
    ex._rerun_worker("n.jpg")           # md empty -> RuntimeError inside
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _doc_row(fn))
    out = ex.extraction_detail("n.jpg", None)
    assert out.rerun_running is False
    assert "no markdown" in out.rerun_error
    ex._RERUNS.clear()


# ── lot-count checksum (expected vs extracted) ──────────────────────────────


def test_count_extracted_lots_empty_is_none():
    from api.review.extraction import count_extracted_lots
    assert count_extracted_lots([]) is None


def test_count_extracted_lots_no_lot_index_is_one_lot():
    from api.review.extraction import count_extracted_lots
    ents = [{"cls": "property", "attrs": {}},
            {"cls": "borrower", "attrs": None},
            {"cls": "identifier"}]
    assert count_extracted_lots(ents) == 1


def test_count_extracted_lots_distinct_indices():
    from api.review.extraction import count_extracted_lots
    ents = [{"attrs": {"lot_index": 1}},
            {"attrs": {"lot_index": "1"}},   # same lot, str vs int
            {"attrs": {"lot_index": 2}},
            {"attrs": {"lot_index": 3}},
            {"attrs": {}}]                   # unindexed entity doesn't add a lot
    assert count_extracted_lots(ents) == 3


def test_queue_row_lot_count_mismatch(monkeypatch):
    """expected=2 but the extraction only has lot 1 -> the checksum fires."""
    import json as _json
    import api.review.extraction as ex
    row = {"filename": "n.jpg", "status": "pending", "score": 80,
           "extraction_at": None, "markdown_reextracted_at": None,
           "markdown_loaded_at": None, "extraction_batch": 1,
           "expected_lot_count": 2,
           "extraction_json": _json.dumps([{"attrs": {"lot_index": 1}}])}
    monkeypatch.setattr(ex, "list_extraction_queue", lambda *a, **k: [row])
    out = ex.extraction_queue(None, 200, "recent", None, None, None)
    r = out.rows[0]
    assert r.expected_lot_count == 2
    assert r.extracted_lot_count == 1
    assert r.lot_count_mismatch is True


def test_queue_row_lot_count_match_and_unknown(monkeypatch):
    import json as _json
    import api.review.extraction as ex
    rows = [
        {"filename": "match.jpg", "status": "pending", "score": None,
         "extraction_at": None, "markdown_reextracted_at": None,
         "markdown_loaded_at": None, "extraction_batch": None,
         "expected_lot_count": 2,
         "extraction_json": _json.dumps([{"attrs": {"lot_index": 1}},
                                          {"attrs": {"lot_index": 2}}])},
        {"filename": "unknown.jpg", "status": "pending", "score": None,
         "extraction_at": None, "markdown_reextracted_at": None,
         "markdown_loaded_at": None, "extraction_batch": None,
         "expected_lot_count": None,
         "extraction_json": _json.dumps([{"attrs": {"lot_index": 1}}])},
    ]
    monkeypatch.setattr(ex, "list_extraction_queue", lambda *a, **k: rows)
    out = ex.extraction_queue(None, 200, "recent", None, None, None)
    match, unknown = out.rows
    assert match.lot_count_mismatch is False
    assert match.extracted_lot_count == 2
    # no reviewer count -> no claim, never flagged
    assert unknown.lot_count_mismatch is False
    assert unknown.expected_lot_count is None


# ── stage-parity filters (notice type / auction date / search) ──────────────


def _capture_cypher(fn, *a, **kw):
    """Run a queue/stats/bulk call against a stubbed driver, returning
    (cypher, params). Both run_read_query and run_query are stubbed — the bulk
    path writes, so it goes through run_query."""
    import api.review.extraction as ex
    captured = {}

    def fake_run(cypher, params=None, **k):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"total": 0, "pending": 0, "verified": 0, "edited": 0, "n": 0}]

    orig_read, orig_write = ex.run_read_query, ex.run_query
    try:
        ex.run_read_query = fake_run
        ex.run_query = fake_run
        fn(*a, **kw)
    finally:
        ex.run_read_query = orig_read
        ex.run_query = orig_write
    return captured["cypher"], captured["params"]


def test_queue_filters_by_notice_type():
    import api.review.extraction as ex
    cy, _ = _capture_cypher(ex.list_extraction_queue, None, 200, "recent",
                            notice_type="multi")
    assert "d.notice_type = 'multi'" in cy
    cy, _ = _capture_cypher(ex.list_extraction_queue, None, 200, "recent",
                            notice_type="unclassified")
    assert "d.notice_type IS NULL" in cy
    # no filter -> no clause
    cy, _ = _capture_cypher(ex.list_extraction_queue, None, 200, "recent")
    assert "d.notice_type" not in cy.split("RETURN")[0]


def test_queue_filters_by_auction_date_window():
    import api.review.extraction as ex
    cy, params = _capture_cypher(ex.list_extraction_queue, None, 200, "recent",
                                 date_from="2026-08-01", date_to="2026-08-31")
    assert "auction_start_dt" in cy
    assert params["date_from"] == "2026-08-01"
    assert params["date_to"] == "2026-08-31"


def test_queue_search_covers_filename_title_and_borrower():
    """One search box, three places a reviewer might remember the notice from."""
    import api.review.extraction as ex
    cy, params = _capture_cypher(ex.list_extraction_queue, None, 200, "recent",
                                 q="hinduja")
    assert "d.filename" in cy
    assert "_p.title" in cy
    assert "_b.name" in cy
    assert params["q"] == "hinduja"


def test_stats_counts_by_status_and_ignores_status_filter():
    """The pills ARE the status breakdown, so stats must not filter by status."""
    import api.review.extraction as ex
    cy, _ = _capture_cypher(ex.extraction_stats, notice_type="single")
    assert "d.notice_type = 'single'" in cy
    assert "extraction_review_status,'pending') = $status" not in cy
    assert "AS pending" in cy and "AS verified" in cy and "AS edited" in cy


def test_no_all_maps_sentinel_to_none():
    """The shared filter bar sends 'all' to mean no filter."""
    from api.review.extraction import _no_all
    assert _no_all("all") is None
    assert _no_all("") is None
    assert _no_all(None) is None
    assert _no_all("multi") == "multi"


def test_stats_route_registered_before_filename_catchall():
    """/stats must resolve as its own route, not as a filename for the
    `/{filename:path}` catch-all declared after it."""
    from api.main import app
    paths = [r.path for r in app.routes]
    assert "/review/extraction/stats" in paths
    assert (paths.index("/review/extraction/stats")
            < paths.index("/review/extraction/{filename:path}"))


# ── bulk confirm ────────────────────────────────────────────────────────────


def test_bulk_confirm_pins_status_to_pending():
    """Verified rows are a no-op; 'edited' rows must NOT be swept back to
    'verified' — that would erase the fact a human changed fields."""
    import api.review.extraction as ex
    cy, params = _capture_cypher(ex.bulk_verify_extractions, "me@example.com",
                                 score_min=80)
    assert params["status"] == "pending"
    assert "extraction_review_status,'pending') = $status" in cy
    assert "SET d.extraction_review_status = 'verified'" in cy
    assert "d.extraction_verified_by   = $by" in cy


def test_bulk_confirm_honours_every_queue_filter():
    import api.review.extraction as ex
    cy, params = _capture_cypher(ex.bulk_verify_extractions, "me@example.com",
                                 score_min=50, score_max=90, notice_type="multi",
                                 date_from="2026-08-01", date_to="2026-08-31",
                                 q="hinduja")
    assert "d.extraction_score >= $score_min" in cy
    assert "d.extraction_score <= $score_max" in cy
    assert "d.notice_type = 'multi'" in cy
    assert "auction_start_dt" in cy
    assert params["q"] == "hinduja"


def test_bulk_confirm_dry_run_does_not_write():
    import api.review.extraction as ex
    cy, _ = _capture_cypher(ex.bulk_verify_extractions, "me@example.com",
                            dry_run=True)
    assert "count(d) AS n" in cy
    assert "SET" not in cy


def test_queue_total_is_a_real_count_not_the_row_cap():
    """The 'Confirm all N in range' button acts on the whole matching set, so a
    total capped by $limit would understate what it is about to verify."""
    import api.review.extraction as ex
    calls = {"n": 0}

    def fake_list(*a, **kw):
        calls["n"] += 1
        return [{"filename": f"{i}.jpg", "status": "pending", "score": 90,
                 "extraction_at": None, "extraction_batch": None,
                 "expected_lot_count": None, "extraction_json": "[]"}
                for i in range(3)]          # 3 rows returned...

    orig_count = ex.count_extraction_queue
    try:
        ex.list_extraction_queue = fake_list
        ex.count_extraction_queue = lambda *a, **kw: 1530   # ...of 1530 matching
        out = ex.extraction_queue(status="pending", limit=3, sort="recent",
                                  score_min=None, score_max=None,
                                  notice_type=None, date_from=None,
                                  date_to=None, q=None, _admin=None)
        assert len(out.rows) == 3
        assert out.total == 1530
    finally:
        ex.count_extraction_queue = orig_count


def test_bulk_confirm_route_registered_before_catchall():
    from api.main import app
    paths = [r.path for r in app.routes]
    assert "/review/extraction/bulk-confirm" in paths
    assert (paths.index("/review/extraction/bulk-confirm")
            < paths.index("/review/extraction/{filename:path}"))
