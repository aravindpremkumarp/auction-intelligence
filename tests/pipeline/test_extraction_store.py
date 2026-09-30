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


def test_loader_write_carries_rows(monkeypatch):
    captured = {}
    monkeypatch.setattr(M, "run_query", lambda cypher, params=None, **k: captured.update(cypher=cypher, params=params) or [])
    monkeypatch.setattr(M, "carry_rows", lambda targets, ents: (
        [{"fn": t, "cj": "{}", "prev_j": None, "prev_reader": None, "prev_at": None} for t in targets],
        {"orphaned": 0}))
    monkeypatch.setattr(M, "clear_auto_marks", lambda fns: None)
    monkeypatch.setattr(M, "stamp_key_scores", lambda fns: None)
    monkeypatch.setattr(M, "validate_stored", lambda ents, source_text="": {"score": 90})
    monkeypatch.setattr(M, "_entities", lambda res, src="": [{"id": "abc", "cls": "borrower", "text": "x",
                                                              "start": 0, "end": 1, "attrs": {}}])
    LX = SimpleNamespace(extract=lambda *a, **k: SimpleNamespace(extractions=[1]))
    ok, model, line = M._extract_one({"filename": "a.jpg", "md": "x", "twins": ["a.jpg", "b.jpg"]},
                                     batch=3, route=False, LX=LX)
    assert ok
    assert "UNWIND $rows AS r" in captured["cypher"]
    assert "d.extraction_prev_json = r.prev_j" in captured["cypher"]
    assert "d.extraction_corrections_json = r.cj" in captured["cypher"]
    assert [r["fn"] for r in captured["params"]["rows"]] == ["a.jpg", "b.jpg"]
