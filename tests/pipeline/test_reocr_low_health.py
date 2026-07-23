"""scripts.reocr_low_health_datalab: pilot mixing and the health-gate that
decides whether a Datalab re-OCR overwrites the existing text.

Network-free — fetch_source and datalab_api.run_file are monkeypatched;
score_ocr_health runs for real so the gate is exercised end to end.
"""
from __future__ import annotations

import scripts.reocr_low_health_datalab as M


DOC = {"block_type": "Document", "children": [
    {"block_type": "Page", "bbox": [0, 0, 1000, 1400], "children": [
        {"block_type": "Text", "html": "<p>clean notice text</p>", "bbox": [10, 10, 90, 40]},
    ]},
]}
EMPTY_DOC = {"block_type": "Document", "children": []}


def _t(nt="single", old=60):
    return {"file_path": "notices/x.jpg", "filename": "x.jpg",
            "notice_type": nt, "public_url": "http://x", "old_score": old}


def test_pick_pilot_mixes_single_and_multi():
    targets = [{"notice_type": "single"}] * 5 + [{"notice_type": "multi"}] * 4
    p = M.pick_pilot(targets)
    assert sum(1 for x in p if x["notice_type"] == "single") == 3
    assert sum(1 for x in p if x["notice_type"] == "multi") == 2


def test_writes_when_health_improves(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "fetch_source", lambda url: tmp_path / "x.jpg")
    monkeypatch.setattr(M.datalab_api, "run_file",
                        lambda *a, **k: {"json": DOC, "markdown": "clean notice text"})
    r = M.reocr_one(_t(old=60))
    assert r.get("ok_to_write") is True
    assert r["new_score"] == 100


def test_skips_when_result_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "fetch_source", lambda url: tmp_path / "x.jpg")
    monkeypatch.setattr(M.datalab_api, "run_file",
                        lambda *a, **k: {"json": EMPTY_DOC, "markdown": ""})
    r = M.reocr_one(_t(old=60))
    assert not r.get("ok_to_write")
    assert "empty" in r["note"]


def test_skips_when_health_would_worsen(monkeypatch, tmp_path):
    monkeypatch.setattr(M, "fetch_source", lambda url: tmp_path / "x.jpg")
    # Hallucinated foreign script → health penalty → below the old score.
    monkeypatch.setattr(M.datalab_api, "run_file",
                        lambda *a, **k: {"json": DOC, "markdown": "中国银行 leaked text here"})
    r = M.reocr_one(_t(old=100))
    assert not r.get("ok_to_write")
    assert "no gain" in r["note"]


def test_skips_when_no_health_gain(monkeypatch, tmp_path):
    # Equal score (100 vs 100) must be skipped — strict improvement only, so a
    # re-run never rewrites a doc Datalab can't actually improve.
    monkeypatch.setattr(M, "fetch_source", lambda url: tmp_path / "x.jpg")
    monkeypatch.setattr(M.datalab_api, "run_file",
                        lambda *a, **k: {"json": DOC, "markdown": "clean notice text"})
    r = M.reocr_one(_t(old=100))
    assert not r.get("ok_to_write")
    assert "no gain" in r["note"]
