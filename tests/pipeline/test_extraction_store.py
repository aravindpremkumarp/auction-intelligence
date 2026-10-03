"""Every writer keeps the previous read and carries corrections (PR4)."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pipeline.extraction_store as ES
import pipeline.load_extractions as M
from pipeline.extraction_ids import assign_ids


def _e(cls, text, start, lot="1"):
    return {"cls": cls, "text": text, "start": start, "end": start + len(text),
            "attrs": {"lot_index": lot}}


def test_carry_rows_moves_corrections_and_keeps_the_previous_read(monkeypatch):
    old = assign_ids([_e("borrower", "Komla SJ", 10)])
    new = assign_ids([_e("borrower", "Komla SJ", 14), _e("extent", "1 acre", 30)])
    monkeypatch.setattr(ES, "previous", lambda fns: {
        "a.jpg": {"j": json.dumps(old), "cj": json.dumps({old[0]["id"]: {"value": "Komala SJ", "by": "p"}}),
                  "reader": None, "at": "2026-09-01T00:00:00Z"},
        "b.jpg": {"j": None, "cj": None, "reader": None, "at": None}})
    rows, report = ES.carry_rows(["a.jpg", "b.jpg"], new)
    a, b = rows
    assert json.loads(a["cj"])[new[0]["id"]]["value"] == "Komala SJ"
    assert a["prev_j"] == json.dumps(old) and a["prev_reader"] == "langextract"
    assert b["cj"] == "{}" and b["prev_j"] is None and b["prev_reader"] is None
    assert report == {"moved": 1}


def test_previous_swallows_a_graph_that_cannot_be_read(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("no neo4j here")
    monkeypatch.setattr(ES, "run_read_query", boom)
    assert ES.previous(["x"]) == {}
    assert ES.previous([]) == {}


def test_loader_entities_carry_stable_ids(monkeypatch):
    monkeypatch.setattr(M, "ground_missing", lambda ents, src: 0)
    ext = [SimpleNamespace(extraction_class="borrower", extraction_text="Komla SJ",
                           attributes={"lot_index": "1"},
                           char_interval=SimpleNamespace(start_pos=10, end_pos=18)),
           SimpleNamespace(extraction_class="identifier", extraction_text="T.S.No 5",
                           attributes={"kind": "T.S.No", "value": "5"}, char_interval=None)]
    res = SimpleNamespace(extractions=ext)
    a = M._entities(res, "")
    b = M._entities(res, "")
    assert [e["id"] for e in a] == [e["id"] for e in b]
    assert all(len(e["id"]) >= 12 for e in a) and a[0]["id"] != a[1]["id"]
    assert a[1]["attrs"]["kind"] == "survey_old"       # normalisation still applies


def _quiet_store(monkeypatch, captured: list):
    monkeypatch.setattr(ES, "run_query", lambda cypher, params=None, **k: captured.append((cypher, params)) or [])
    monkeypatch.setattr(ES, "carry_rows", lambda targets, ents: (
        [{"fn": t, "cj": "{}", "prev_j": None, "prev_reader": None, "prev_at": None} for t in targets],
        {"orphaned": 0}))
    monkeypatch.setattr(ES, "clear_auto_marks", lambda fns: None)
    monkeypatch.setattr(ES, "stamp_key_scores", lambda fns: None)
    monkeypatch.setattr(ES, "write_marks", lambda fn, marks: 0)
    monkeypatch.setattr(ES, "validate_stored", lambda ents, source_text="": {"score": 90})


def test_loader_write_goes_through_the_switch_and_the_store(monkeypatch):
    captured: list = []
    _quiet_store(monkeypatch, captured)
    ents = [{"id": "abc", "cls": "borrower", "text": "x", "start": 0, "end": 1,
             "attrs": {"evidence": "CONTESTED"}}]
    monkeypatch.setattr(M, "read_document", lambda d, route: (ents, "m", {"reader": "langextract"}))
    ok, model, line = M._extract_one({"filename": "a.jpg", "md": "x", "twins": ["a.jpg", "b.jpg"]},
                                     batch=3, route=False, LX=None)
    assert ok and "reader=langextract" in line
    cypher, params = captured[0]
    assert "UNWIND $rows AS r" in cypher
    assert "d.extraction_prev_json = r.prev_j" in cypher
    assert "d.extraction_corrections_json = r.cj" in cypher
    assert "d.extraction_reader = $reader" in cypher and params["reader"] == "langextract"
    assert "d.extraction_review_status = 'pending'" in cypher
    assert "REMOVE d.extraction_verified_by, d.extraction_verified_at" in cypher
    assert "d.extraction_stale_at = NULL" in cypher
    assert [r["fn"] for r in params["rows"]] == ["a.jpg", "b.jpg"]
    assert params["counts"] == {"contested": 1, "fuzzy": 0, "illegible": 0, "dropped": 0}
    assert params["text_hash"] and len(params["text_hash"]) == 64
    assert any("ExtractionRun" in c for c, _ in captured)


def test_store_writes_shadow_events_and_marks(monkeypatch):
    captured: list = []
    _quiet_store(monkeypatch, captured)
    marks: list = []
    monkeypatch.setattr(ES, "write_marks", lambda fn, m: marks.append((fn, m)) or len(m))
    ents = [{"cls": "auction_terms", "text": "Rs.1", "start": 0, "end": 4, "attrs": {"lot_index": "1"}}]
    meta = {"reader": "v2", "prompt_hash": "v2-s1-abc", "schema_version": 1,
            "timeline": [{"event": "auction_start", "date": "2026-10-15", "lot_index": "1"}],
            "marks": [("1", "possession_type"), ("1", "reserve_price")],
            "dropped": [{"cls": "extent"}],
            "shadow": {"entities": ents, "score": 70, "model": "m2", "judge": {"v2_better": True}}}
    res = ES.write_extraction({"filename": "a.jpg", "md": "Rs.1"}, ents, 5, reader="v2", model="m", meta=meta)
    cyphers = [c for c, _ in captured]
    assert any("AuctionEvent" in c for c in cyphers) and any("extraction_shadow_json" in c for c in cyphers)
    assert res["counts"]["dropped"] == 1
    (fn, m), = marks
    assert "absent:1:possession_type" in m and "unfound:1:reserve_price" in m
    assert all(v["rule"] == "reader_not_stated" and v["by"] == "auto" for v in m.values())


def test_keep_better_raises_when_stored_is_at_least_as_good(monkeypatch):
    captured: list = []
    _quiet_store(monkeypatch, captured)
    stored = [{"cls": "auction_terms", "text": "Rs.1", "start": 0, "end": 4,
               "attrs": {"lot_index": "1", "reserve_price_num": "100000"}}]
    monkeypatch.setattr(ES, "stored", lambda fn: {"entities": stored, "text_changed": False})
    import pytest
    with pytest.raises(ES.KeptExisting):
        ES.write_extraction({"filename": "a.jpg", "md": "Rs.1"},
                            [{"cls": "auction_terms", "text": "Rs.1", "start": 0, "end": 4, "attrs": {"lot_index": "1"}}],
                            5, keep_better=True)
    assert captured == []
