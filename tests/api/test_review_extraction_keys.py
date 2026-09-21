"""The key-entity checklist on the extraction review API
(api/review/extraction.py + pipeline/key_entities.py): what the queue and
detail carry, the reviewer's add / absent / delete writes, and the guards on
them. Pure-logic tests with the graph stubbed, like test_review_extraction.py."""
from __future__ import annotations

import inspect
import json

import pytest
from fastapi import HTTPException

import api.review.extraction as ex


def _ents():
    return [
        {"id": "0", "cls": "property", "text": "Vacant land", "start": 0, "end": 11,
         "attrs": {"lot_index": "1", "property_type": "vacant land"}},
        {"id": "1", "cls": "auction_terms", "text": "Reserve Rs.9,50,000",
         "start": 20, "end": 39,
         "attrs": {"lot_index": "1", "reserve_price_num": "950000",
                   "auction_start_dt": "2026-05-11T11:00"}},
    ]


def _row(fn="n.jpg", **over):
    row = {"filename": fn, "markdown": "Vacant land at X.  Reserve Rs.9,50,000",
           "extraction_json": json.dumps(_ents()), "corrections_json": "{}",
           "status": "pending", "verified_by": None, "verified_at": None,
           "expected_lot_count": 2}
    row.update(over)
    return row


# ── detail ───────────────────────────────────────────────────────────────────

def test_detail_carries_checklist_and_expected_lots(monkeypatch):
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row())
    out = ex.extraction_detail("n.jpg", None)
    assert out.expected_lot_count == 2
    k = out.keys
    assert [lot.lot_index for lot in k.lots] == ["1", "2"]
    assert k.lots[1].extracted is False
    c = k.lots[0].cells
    assert c["property_type"].status == "filled" and c["property_type"].field_id == "0"
    assert c["reserve_price"].value == "₹9,50,000"
    assert c["extent"].status == "missing"
    assert k.total == 14 and k.filled == 3
    assert "lot 2: full description" in k.missing_labels


def test_detail_shows_added_entity_as_a_field_and_fills_its_cell(monkeypatch):
    cj = json.dumps({"add:ab12": {"cls": "extent", "text": "land at X",
                                  "start": 7, "end": 16,
                                  "attrs": {"lot_index": "1", "total_area": "2 acres"},
                                  "by": "a@b.c", "at": "t"},
                     "absent:1:possession_type": {"by": "a@b.c", "at": "t"}})
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row(corrections_json=cj,
                                                              expected_lot_count=1))
    out = ex.extraction_detail("n.jpg", None)
    added = [f for f in out.fields if f.added]
    assert len(added) == 1 and added[0].id == "add:ab12"
    assert added[0].cls == "extent" and added[0].grounded is True
    assert added[0].start == 7 and added[0].end == 16       # re-anchor kept the span
    assert added[0].lot_index == "1" and added[0].attrs == {"total_area": "2 acres"}
    c = out.keys.lots[0].cells
    assert c["extent"].status == "filled" and c["extent"].field_id == "add:ab12"
    assert c["possession_type"].status == "absent"


def test_stitched_follower_has_no_checklist(monkeypatch):
    monkeypatch.setattr(ex, "get_extraction", lambda fn: {"stitched_into": "lead.pdf"})
    out = ex.extraction_detail("p2.pdf", None)
    assert out.stitched_into == "lead.pdf" and out.keys is None


# ── queue ────────────────────────────────────────────────────────────────────

def test_queue_rows_carry_key_summary(monkeypatch):
    monkeypatch.setattr(ex, "list_extraction_queue", lambda *a, **kw: [
        {"filename": "n.jpg", "status": "pending", "score": 70,
         "extraction_at": None, "extraction_batch": None,
         "extraction_json": json.dumps(_ents()), "corrections_json": "{}",
         "expected_lot_count": 1}])
    monkeypatch.setattr(ex, "count_extraction_queue", lambda *a, **kw: 1)
    out = ex.extraction_queue(status=None, limit=10, sort="recent",
                              score_min=None, score_max=None, _admin=None)
    (r,) = out.rows
    assert r.key_score == round(100 * 3 / 7)
    assert r.key_missing == 4
    assert r.key_missing_labels[0] == "lot 1: location"


def test_queue_forwards_keys_sort_and_missing_filter(monkeypatch):
    seen = {}
    monkeypatch.setattr(ex, "list_extraction_queue",
                        lambda status, limit, sort, **kw: seen.update(sort=sort, **kw) or [])
    monkeypatch.setattr(ex, "count_extraction_queue",
                        lambda *a, **kw: seen.update(count_kw=kw) or 0)
    ex.extraction_queue(status=None, limit=10, sort="keys", score_min=None,
                        score_max=None, missing_keys=True, _admin=None)
    assert seen["sort"] == "keys" and seen["missing_keys"] is True
    assert seen["count_kw"]["missing_keys"] is True


def test_keys_order_clause_and_missing_filter_reach_cypher(monkeypatch):
    captured = {}
    monkeypatch.setattr(ex, "run_read_query",
                        lambda cypher, params=None, **kw: captured.update(c=cypher, p=params) or [])
    ex.list_extraction_queue(None, 10, "keys", missing_keys=True)
    c = captured["c"]
    assert "coalesce(d.extraction_key_score, -1) ASC" in c
    assert "coalesce(d.extraction_key_missing, 1) > 0" in c
    assert "corrections_json" in c
    # and stays out of the ordinary sort
    ex.list_extraction_queue(None, 10, "recent")
    assert "extraction_key_missing" not in captured["c"]


# ── writes ───────────────────────────────────────────────────────────────────

def _stub_writes(monkeypatch, store: dict):
    monkeypatch.setattr(ex, "_load_corrections", lambda fn: store.get(fn))
    monkeypatch.setattr(ex, "_write_corrections",
                        lambda fn, corr: store.__setitem__(fn, corr) or True)
    monkeypatch.setattr(ex, "get_stitch_pointer", lambda fn: None)
    monkeypatch.setattr(ex, "extraction_detail", lambda fn, admin: fn)


class _Admin:
    email = "rev@example.com"


def test_add_field_stores_a_grounded_entity(monkeypatch):
    store = {"n.jpg": {}}
    _stub_writes(monkeypatch, store)
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row())
    body = ex.AddFieldBody(cls="extent", text="land at X", start=7, end=16,
                           lot_index="1", attrs={"total_area": "2 acres", "x": ""})
    ex.extraction_add_field("n.jpg", body, _Admin())
    (k, v), = store["n.jpg"].items()
    assert k.startswith("add:")
    assert v["cls"] == "extent" and v["start"] == 7 and v["end"] == 16
    assert v["attrs"] == {"total_area": "2 acres", "lot_index": "1"}   # blanks dropped
    assert v["by"] == "rev@example.com"


def test_add_field_typed_by_hand_is_ungrounded(monkeypatch):
    store = {"n.jpg": {}}
    _stub_writes(monkeypatch, store)
    body = ex.AddFieldBody(cls="property", text="flat", lot_index="2",
                           attrs={"possession_type": "physical"})
    ex.extraction_add_field("n.jpg", body, _Admin())
    (v,) = store["n.jpg"].values()
    assert v["start"] is None and v["end"] is None


def test_add_field_rejects_bad_class_and_span(monkeypatch):
    store = {"n.jpg": {}}
    _stub_writes(monkeypatch, store)
    monkeypatch.setattr(ex, "get_extraction", lambda fn: _row())
    with pytest.raises(HTTPException) as e:
        ex.extraction_add_field("n.jpg", ex.AddFieldBody(cls="nope", text="x"), _Admin())
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:                     # start without end
        ex.extraction_add_field("n.jpg", ex.AddFieldBody(cls="extent", text="x", start=1), _Admin())
    assert e.value.status_code == 422
    with pytest.raises(HTTPException) as e:                     # text != markdown[start:end]
        ex.extraction_add_field("n.jpg", ex.AddFieldBody(cls="extent", text="wrong",
                                                          start=7, end=16), _Admin())
    assert e.value.status_code == 422
    assert store["n.jpg"] == {}


def test_delete_only_removes_added_fields(monkeypatch):
    store = {"n.jpg": {"add:1": {"cls": "extent", "text": "x"}, "3": {"value": "v"}}}
    _stub_writes(monkeypatch, store)
    with pytest.raises(HTTPException) as e:
        ex.extraction_delete_added_field("n.jpg", "3", _Admin())
    assert e.value.status_code == 422
    ex.extraction_delete_added_field("n.jpg", "add:1", _Admin())
    assert store["n.jpg"] == {"3": {"value": "v"}}
    with pytest.raises(HTTPException) as e:
        ex.extraction_delete_added_field("n.jpg", "add:1", _Admin())
    assert e.value.status_code == 404


def test_key_absent_sets_and_clears(monkeypatch):
    store = {"n.jpg": {}}
    _stub_writes(monkeypatch, store)
    ex.extraction_key_absent("n.jpg", ex.KeyAbsentBody(lot_index="1", key="possession_type"), _Admin())
    assert "absent:1:possession_type" in store["n.jpg"]
    ex.extraction_key_absent("n.jpg", ex.KeyAbsentBody(lot_index="1", key="possession_type",
                                                       absent=False), _Admin())
    assert store["n.jpg"] == {}
    with pytest.raises(HTTPException) as e:
        ex.extraction_key_absent("n.jpg", ex.KeyAbsentBody(lot_index="1", key="colour"), _Admin())
    assert e.value.status_code == 422


def test_correcting_an_added_field_edits_it_in_place(monkeypatch):
    store = {"n.jpg": {"add:1": {"cls": "extent", "text": "old", "start": 1, "end": 4,
                                 "attrs": {}, "by": "x", "at": "t"}}}
    _stub_writes(monkeypatch, store)
    assert ex.save_field_correction("n.jpg", "add:1", "new text", "rev@example.com", None)
    v = store["n.jpg"]["add:1"]
    assert v["text"] == "new text" and v["by"] == "rev@example.com"
    assert v["start"] is None and v["end"] is None       # typed text: span dropped
    assert "value" not in v                               # not a model-field correction
    assert not ex.save_field_correction("n.jpg", "add:9", "x", "r", None)


def test_verify_persists_notes():
    src = inspect.getsource(ex.verify_extraction)
    assert "d.extraction_review_notes" in src
    assert "ELSE $notes END" in src


def test_write_corrections_restamps_key_score(monkeypatch):
    calls = {}
    monkeypatch.setattr(ex, "run_query", lambda c, p: [{"filename": p["fn"]}])
    monkeypatch.setattr(ex, "stamp_key_scores", lambda fns: calls.setdefault("fns", fns))
    assert ex._write_corrections("n.jpg", {"3": {"value": "v"}})
    assert calls["fns"] == ["n.jpg"]
