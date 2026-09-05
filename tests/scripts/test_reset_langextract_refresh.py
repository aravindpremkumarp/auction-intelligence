"""Unit tests for the `--refresh` selector and the verification reset.

Both are Cypher, so what is testable without a database is the query the script
builds and the parameters it sends. That is the part that decides WHICH notices
get spent on a model call and WHAT state they land in afterwards — a silent
change to either is exactly the regression worth catching.
"""
from __future__ import annotations

import scripts.reset_langextract_and_extract as R


class _Capture:
    """Stands in for run_read_query / run_query, recording the call."""

    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.cypher = None
        self.params = None

    def __call__(self, cypher, params=None, **kw):
        self.cypher, self.params = cypher, params
        return self.rows


# ── selection ────────────────────────────────────────────────────────────────

def test_refresh_selects_on_both_staleness_signals(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    # markdown rewritten after the extraction, OR a failed extraction
    assert "md > ex OR d.extraction_score < $min_score" in cap.cypher
    assert cap.params == {"min_ocr": 90, "min_score": 60}


def test_refresh_reads_the_newest_markdown_timestamp(monkeypatch):
    """`markdown_raw_at` is the rewrite; `markdown_loaded_at` is the fallback for
    documents predating it. Comparing against only one of them misses rewrites."""
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    assert "coalesce(d.markdown_raw_at, d.markdown_loaded_at)" in cap.cypher


def test_refresh_requires_an_existing_extraction(monkeypatch):
    """A never-extracted notice is `select_docs`'s job. Picking it up here would
    double-spend on it and report it as a refresh."""
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    assert "d.extraction_json IS NOT NULL" in cap.cypher


def test_single_lot_restricts_to_documents_with_exactly_one_lot(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=True, limit=None)
    assert "MATCH (d)-[:HAS_LOT]->(l:Lot)" in cap.cypher
    assert "WHERE lots = 1" in cap.cypher


def test_without_single_lot_no_lot_filter_is_applied(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    assert "HAS_LOT" not in cap.cypher


def test_multi_lot_restricts_to_documents_with_two_or_more_lots(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None, multi_lot=True)
    assert "MATCH (d)-[:HAS_LOT]->(l:Lot)" in cap.cypher
    assert "WHERE lots > 1" in cap.cypher


def test_single_and_multi_lot_together_is_refused(monkeypatch):
    """The two are complements; asking for both selects nothing, and silently
    returning an empty set would read as 'everything is up to date'."""
    import pytest
    monkeypatch.setattr(R, "run_read_query", _Capture())
    with pytest.raises(ValueError):
        R.select_refresh_docs(90, 60, single_lot=True, limit=None,
                              multi_lot=True)


def test_extracted_before_adds_a_third_staleness_signal(monkeypatch):
    """A prompt change leaves the markdown and the score untouched, so neither
    of the other two signals can see it."""
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None,
                          extracted_before="2026-09-05T12:00")
    assert "OR ex < $extracted_before" in cap.cypher
    assert cap.params["extracted_before"] == "2026-09-05T12:00"


def test_extracted_before_is_absent_when_not_asked_for(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    assert "extracted_before" not in cap.cypher
    assert "extracted_before" not in cap.params


def test_limit_is_inlined_as_an_integer(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=5)
    assert cap.cypher.rstrip().endswith("LIMIT 5")


def test_refresh_carries_the_portal_roster(monkeypatch):
    """The roster is what a re-extraction reads to name its listing. Selecting
    without it would silently extract these notices differently from every other
    entry point."""
    cap = _Capture()
    monkeypatch.setattr(R, "run_read_query", cap)
    R.select_refresh_docs(90, 60, single_lot=False, limit=None)
    assert "roster AS roster" in cap.cypher


# ── the write ────────────────────────────────────────────────────────────────

def _write_cypher(monkeypatch, entities=None) -> str:
    cap = _Capture(rows=[{"d.filename": "x.jpg"}])
    monkeypatch.setattr(R, "run_query", cap)
    monkeypatch.setattr(R, "_entities",
                        lambda res: [{"id": "e1"}] if entities is None
                        else entities)
    monkeypatch.setattr(R, "validate", lambda *a, **k: {"score": 80})

    class _Res:
        extractions = []

    import sys
    import types
    stub = types.ModuleType("pipeline.langextract_examples")
    stub.extract = lambda *a, **k: _Res()
    monkeypatch.setitem(sys.modules, "pipeline.langextract_examples", stub)

    R._extract_one({"filename": "x.jpg", "md": "text"}, batch=1, route=False)
    return cap.cypher


def test_reextraction_returns_the_notice_to_pending(monkeypatch):
    """The entities a reviewer verified are gone, so the verdict cannot stand
    over the ones replacing them."""
    assert "d.extraction_review_status = 'pending'" in _write_cypher(monkeypatch)


def test_reextraction_drops_the_verifier(monkeypatch):
    cypher = _write_cypher(monkeypatch)
    assert "REMOVE d.extraction_verified_by, d.extraction_verified_at" in cypher


def test_reextraction_clears_the_staleness_marker(monkeypatch):
    assert "d.extraction_stale_at = NULL" in _write_cypher(monkeypatch)


def test_an_empty_result_is_never_written(monkeypatch):
    """A model reply LangExtract could not parse yields zero entities. Writing
    that over a notice's existing entities destroys them; the run must fail the
    document and leave it alone."""
    import pytest
    with pytest.raises(ValueError):
        _write_cypher(monkeypatch, entities=[])
