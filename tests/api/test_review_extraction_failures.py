"""Failure filters on the extraction review queue (api/review/extraction.py
EXTRACTION_FAILURES + pipeline/key_entities.stamp_key_scores): the pills
reach Cypher, list / count / stats / bulk-confirm share them, unknown keys are
refused, and each row names the failures it carries. Graph stubbed, like
test_review_extraction.py."""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

import api.review.extraction as ex
import pipeline.key_entities as ke


def _capture(monkeypatch, rows=None):
    seen = []

    def fake(cypher, params, **kw):
        seen.append((cypher, params))
        return rows if rows is not None else []

    monkeypatch.setattr(ex, "run_read_query", fake)
    return seen


# ── Cypher ───────────────────────────────────────────────────────────────────

def test_no_failures_adds_no_clause(monkeypatch):
    seen = _capture(monkeypatch)
    ex.list_extraction_queue(None, 200)
    assert "extraction_issue_codes, [])" not in seen[0][0]
    assert "extraction_lot_count <" not in seen[0][0]
    assert seen[0][1]["fail_codes"] == []


def test_missing_lots_is_the_reviewer_lot_count_only(monkeypatch):
    """Not validators.py lot_under_recall: its "S.No" marker count matches
    survey numbers and flagged notices that had every lot."""
    seen = _capture(monkeypatch)
    ex.list_extraction_queue(None, 200, failures=["missing-lots"])
    cypher, params = seen[0]
    assert "d.extraction_lot_count < coalesce(d.stitched_expected_lot_count, d.expected_lot_count)" in cypher
    assert "$fail_codes" not in cypher
    assert params["fail_codes"] == []


def test_selected_failures_or_together(monkeypatch):
    seen = _capture(monkeypatch)
    ex.list_extraction_queue(None, 200, failures=["borrower", "rerun"])
    cypher, params = seen[0]
    assert " OR " in cypher and "d.extraction_stale_at > d.extraction_at" in cypher
    assert params["fail_codes"] == ["lot_missing_borrower", "missing_borrower"]


def test_rerun_alone_needs_no_codes(monkeypatch):
    seen = _capture(monkeypatch)
    ex.list_extraction_queue(None, 200, failures=["rerun"])
    assert "$fail_codes" not in seen[0][0]


def test_count_stats_and_bulk_confirm_share_the_filter(monkeypatch):
    seen = _capture(monkeypatch, rows=[{"n": 3, "total": 3}])
    monkeypatch.setattr(ex, "run_query", lambda c, p, **k: seen.append((c, p)) or [{"n": 3}])
    ex.count_extraction_queue(None, failures=["extent"])
    ex.extraction_stats(failures=["extent"])
    ex.bulk_verify_extractions("a@b.c", failures=["extent"])
    for cypher, params in seen:
        assert "c IN $fail_codes" in cypher
        assert params["fail_codes"] == ["missing_extent"]


# ── endpoints ────────────────────────────────────────────────────────────────

def test_unknown_failure_is_422():
    with pytest.raises(HTTPException) as e:
        ex._clean_failures(["missing-lots", "nope"])
    assert e.value.status_code == 422


def test_clean_failures_orders_by_bar_and_drops_empty():
    assert ex._clean_failures(["rerun", "missing-lots"]) == ["missing-lots", "rerun"]
    assert ex._clean_failures([]) is None
    assert ex._clean_failures(None) is None


def test_queue_endpoint_forwards_failures_and_rows_carry_them(monkeypatch):
    ents = [{"id": "0", "cls": "property", "text": "x", "start": 0, "end": 1,
             "attrs": {"lot_index": "1"}}]
    row = {"filename": "n.jpg", "status": "pending", "score": 50,
           "extraction_json": json.dumps(ents), "corrections_json": "{}",
           "expected_lot_count": 3, "issue_codes": ["missing_borrower", "ungrounded"],
           "extraction_at": "2026-01-02T00:00:00Z",
           "markdown_loaded_at": "2026-02-01T00:00:00Z"}
    seen = {}
    monkeypatch.setattr(ex, "list_extraction_queue",
                        lambda *a, **k: seen.update(k) or [row])
    monkeypatch.setattr(ex, "count_extraction_queue", lambda *a, **k: 1)
    out = ex.extraction_queue(status=None, limit=200, sort="recent",
                              score_min=None, score_max=None,
                              failures=["borrower", "missing-lots"], _admin=None)
    assert seen["failures"] == ["missing-lots", "borrower"]
    assert out.rows[0].failures == ["missing-lots", "rerun", "borrower", "ungrounded"]


def test_pills_follow_the_review_check_order():
    """Missing lots → extra lots → reserve → lot order → auction date →
    description → property type → location → extent → possession, then the rest."""
    keys = list(ex.EXTRACTION_FAILURES)
    review = ["missing-lots", "extra-lots", "reserve", "lot-order", "auction-date",
              "description", "property-type", "location", "extent", "possession"]
    assert [k for k in keys if k in review] == review
    assert keys[:len(review) + 3] == review[:6] + ["wrong-lot"] + review[6:8] + \
        ["not-placed", "district-only"] + review[8:]
    codes = ke.review_codes([], {}, "")
    assert ex.row_failures(sorted(codes), None, None, False) == ["auction-date", "possession"]


def test_review_codes_flag_lot_order_and_missing_date_and_possession():
    md = "SALE NOTICE\n\nProperty at Survey A\n\nProperty at Survey B\n"

    def desc(text, lot):
        s = md.index(text)
        return {"id": text[-1], "cls": "full_description", "text": text,
                "start": s, "end": s + len(text), "attrs": {"lot_index": lot}}

    terms = [{"id": f"t{li}", "cls": "auction_terms", "text": "x", "attrs": {
        "lot_index": li, "auction_start_dt": "2026-11-01T11:00"}} for li in "12"]
    props = [{"id": f"p{li}", "cls": "property", "text": "x", "attrs": {
        "lot_index": li, "possession_type": "symbolic"}} for li in "12"]
    in_order = [desc("Property at Survey A", "1"), desc("Property at Survey B", "2")]
    assert ke.review_codes(in_order + terms + props, {}, md) == set()

    reversed_ = [desc("Property at Survey A", "2"), desc("Property at Survey B", "1")]
    assert ke.review_codes(reversed_ + terms + props, {}, md) == {"lot_order_off"}

    # Lot 2 has no possession; marking it "not in notice" clears the code.
    assert ke.review_codes(in_order + terms + props[:1], {}, md) == {"lot_missing_possession"}
    marked = {ke.absent_key("2", "possession_type"): {"by": "a", "at": "t"}}
    assert ke.review_codes(in_order + terms + props[:1], marked, md) == set()

    # No date anywhere → every lot is missing it.
    assert ke.review_codes(in_order + props, {}, md) == {"lot_missing_auction_date"}


def test_row_failures_extra_lots_and_clean_row():
    assert ex.row_failures([], 4, 2, False) == ["extra-lots"]
    assert ex.row_failures(None, 2, 2, False) == []
    assert ex.row_failures(["lot_under_recall"], None, None, False) == []
    assert ex.row_failures(["lot_under_recall"], 3, 3, False) == []
    assert ex.row_failures([], 2, 3, False) == ["missing-lots"]


# ── stamping ─────────────────────────────────────────────────────────────────

def test_extracted_lot_count():
    assert ke.extracted_lot_count([]) is None
    assert ke.extracted_lot_count([{"cls": "property", "attrs": {}}]) == 1
    assert ke.extracted_lot_count([{"attrs": {"lot_index": 1}},
                                   {"attrs": {"lot_index": "2"}},
                                   {"attrs": {"lot_index": "1"}}]) == 2


def test_issue_codes_see_reviewer_added_entities():
    ej = json.dumps([{"id": "0", "cls": "property", "text": "Vacant land",
                      "start": 0, "end": 11, "attrs": {"property_type": "land"}}])
    before = ke.issue_codes_from_stored(ej, "{}")
    assert "missing_borrower" in before
    cj = json.dumps({"add:1": {"cls": "borrower", "text": "Ravi", "start": 0,
                               "end": 4, "attrs": {}, "by": "a", "at": "t"}})
    assert "missing_borrower" not in ke.issue_codes_from_stored(ej, cj)


def test_stamp_writes_codes_and_lot_count(monkeypatch):
    import api.neo4j_client as nc
    ej = json.dumps([{"id": "0", "cls": "property", "text": "x", "start": 0,
                      "end": 1, "attrs": {"lot_index": "2"}}])
    monkeypatch.setattr(nc, "run_read_query", lambda *a, **k: [
        {"filename": "n.jpg", "ej": ej, "cj": "{}", "md": "x", "elc": 3}])
    written = {}
    monkeypatch.setattr(nc, "run_query", lambda c, p, **k: written.update(c=c, p=p))
    assert ke.stamp_key_scores(["n.jpg"]) == 1
    row = written["p"]["rows"][0]
    assert row["lots"] == 1 and "missing_borrower" in row["codes"]
    assert "d.extraction_issue_codes = row.codes" in written["c"]
    assert "d.extraction_lot_count   = row.lots" in written["c"]


# ── read but not placed ──────────────────────────────────────────────────────

_UNPLACED = [{"lot": "12", "village": "Senkundram", "district": None},
             {"lot": "1", "village": "Vardharajapuram", "district": "Kancheepuram"},
             {"lot": "2", "village": None, "district": None}]


def test_place_pills_split_unplaced_lots_by_district():
    assert ex.place_pills(_UNPLACED) == {"not-placed": True, "district-only": True}
    assert ex.place_pills(_UNPLACED[1:2]) == {"not-placed": False, "district-only": True}
    assert ex.place_pills(None) == {"not-placed": False, "district-only": False}


def test_a_row_carries_the_place_pills_its_lots_raise():
    assert ex.row_failures([], None, None, False, ex.place_pills(_UNPLACED[:1])) == ["not-placed"]
    assert ex.row_failures([], None, None, False, ex.place_pills([])) == []


def test_unplaced_labels_name_the_lot_what_it_gave_and_how_far_it_got():
    assert ex.unplaced_labels(_UNPLACED) == [
        "lot 1: Vardharajapuram, Kancheepuram — district only",
        "lot 2: no village read — no district",
        "lot 12: Senkundram — no district"]


def test_the_place_pills_read_lots_and_skip_out_of_state_ones():
    for key in ("not-placed", "district-only"):
        cypher = ex.EXTRACTION_FAILURES[key]["cypher"]
        assert "(d)-[:HAS_LOT]->(_l:Lot)" in cypher
        assert "outside-tamil-nadu" in cypher
    assert "_l.district IS NULL" in ex.EXTRACTION_FAILURES["not-placed"]["cypher"]
    assert "_l.district IS NOT NULL" in ex.EXTRACTION_FAILURES["district-only"]["cypher"]
