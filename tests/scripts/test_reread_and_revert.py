"""The targeted re-read's selection and forecast, and the revert, DB-free."""
from __future__ import annotations

import json

import scripts.reread_failing as RF
import scripts.revert_extraction as RV


def test_select_builds_the_failing_predicate_and_ocr_gate(monkeypatch):
    seen = {}
    monkeypatch.setattr(RF, "run_read_query", lambda q, p=None, **k: seen.update(q=q, p=p) or [])
    RF.select(50, None, 90)
    q = seen["q"]
    assert "d.extraction_stale_at IS NOT NULL" in q
    assert "d.extraction_score < 70" in q
    assert "any(c IN coalesce(d.extraction_issue_codes, []) WHERE c IN $codes)" in q
    assert "d.extraction_key_missing > 0" in q
    assert "d.ocr_health_score >= $min_ocr" in q and seen["p"]["min_ocr"] == 90
    assert "LIMIT 50" in q and "lot_under_recall" in seen["p"]["codes"]
    RF.select(None, ["a.jpg"], 90)
    assert "d.filename IN $only" in seen["q"] and seen["p"]["only"] == ["a.jpg"]


def test_forecast_counts_reasons_corrections_and_cost():
    docs = [{"reason": "score", "has_corrections": True}, {"reason": "score", "has_corrections": False},
            {"reason": "lot_count", "has_corrections": False}]
    fc = RF.forecast(docs, 0.02)
    assert fc == {"documents": 3, "by_reason": {"score": 2, "lot_count": 1}, "with_corrections": 1,
                  "unit_cost_usd": 0.02, "cost_usd": 0.06}
    assert RF.forecast([], None)["cost_usd"] is None


def test_revert_dry_run_carries_corrections_back(monkeypatch):
    writes = []
    monkeypatch.setattr(RV, "run_query", lambda q, p=None, **k: writes.append(p) or [])
    monkeypatch.setattr(RV, "stamp_key_scores", lambda fns: None)
    prev = [{"id": "p1", "cls": "borrower", "text": "Komla SJ", "start": 10, "end": 18, "attrs": {"lot_index": "1"}}]
    cur = [{"id": "c1", "cls": "borrower", "text": "Komla SJ", "start": 12, "end": 20, "attrs": {"lot_index": "1"}}]
    row = {"fn": "a.jpg", "md": "x" * 30, "prev": json.dumps(prev), "cur": json.dumps(cur),
           "prev_reader": "langextract", "reader": "v2",
           "cj": json.dumps({"c1": {"value": "Komala SJ", "by": "p"}})}
    out = RV.revert(row, dry_run=True)
    assert out["status"] == "would revert" and out["corrections"] == {"moved": 1} and writes == []
    out = RV.revert(row, dry_run=False)
    assert out["status"] == "reverted"
    (p,) = writes
    assert json.loads(p["cj"])["p1"]["value"] == "Komala SJ"
    assert p["prev"] == row["prev"] and p["cur"] == row["cur"] and p["reader"] == "v2"
