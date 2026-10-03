"""The reader switch: which reader runs, and what shadow mode returns."""
from __future__ import annotations

import pipeline.extract_entry as X


def _doc():
    return {"filename": "a.jpg", "md": "Reserve price Rs.1,00,000/- EMD Rs.10,000/-",
            "notice_type": "single", "expected_lot_count": 1}


def test_langextract_is_the_default_and_v1_runs(monkeypatch):
    monkeypatch.delenv("EXTRACT_READER", raising=False)
    monkeypatch.setattr(X.config, "EXTRACT_READER", "langextract")
    monkeypatch.setattr(X, "_v1", lambda d, route: ([{"cls": "x", "attrs": {}}], "m1"))
    monkeypatch.setattr(X, "_v2", lambda d, route, st: (_ for _ in ()).throw(AssertionError("v2 must not run")))
    ents, model, meta = X.read_document(_doc())
    assert ents and model == "m1" and meta == {"reader": "langextract"}


def test_v2_runs_when_selected(monkeypatch):
    monkeypatch.setenv("EXTRACT_READER", "v2")
    monkeypatch.setattr(X, "_v1", lambda d, route: (_ for _ in ()).throw(AssertionError("v1 must not run")))
    monkeypatch.setattr(X, "_v2", lambda d, route, st: ([{"cls": "y", "attrs": {"evidence": "EXPLICIT"}}], "m2",
                                                        {"reader": "v2", "prompt_hash": "v2-s1-x"}))
    ents, model, meta = X.read_document(_doc())
    assert ents[0]["cls"] == "y" and model == "m2" and meta["reader"] == "v2"


def test_shadow_writes_v1_and_carries_v2_beside_it(monkeypatch):
    monkeypatch.setenv("EXTRACT_READER", "shadow")
    v1 = [{"cls": "auction_terms", "text": "Rs.1,00,000/-", "start": 14, "end": 27,
           "attrs": {"lot_index": "1", "reserve_price_num": "100000"}}]
    v2 = v1 + [{"cls": "extent", "text": "x", "start": 0, "end": 1, "attrs": {"lot_index": "1"}}]
    monkeypatch.setattr(X, "_v1", lambda d, route: (v1, "m1"))
    monkeypatch.setattr(X, "_v2", lambda d, route, st: (v2, "m2", {"reader": "v2", "telemetry": {"a": 1},
                                                                   "dropped": [], "segmentation": {"strategy": "whole"}}))
    ents, model, meta = X.read_document(_doc())
    assert ents is v1 and model == "m1" and meta["reader"] == "langextract"
    sh = meta["shadow"]
    assert sh["entities"] is v2 and sh["model"] == "m2" and "score" in sh
    assert "judge" in sh and "v2_better" in sh["judge"]


def test_shadow_failure_never_fails_the_write(monkeypatch):
    monkeypatch.setenv("EXTRACT_READER", "shadow")
    monkeypatch.setattr(X, "_v1", lambda d, route: ([{"cls": "x", "attrs": {}}], "m1"))

    def boom(d, route, st):
        raise RuntimeError("no key")
    monkeypatch.setattr(X, "_v2", boom)
    ents, model, meta = X.read_document(_doc())
    assert ents and meta["shadow"]["error"].startswith("RuntimeError")


def test_unknown_reader_is_refused(monkeypatch):
    monkeypatch.setenv("EXTRACT_READER", "v3")
    import pytest
    with pytest.raises(ValueError):
        X.read_document(_doc())
