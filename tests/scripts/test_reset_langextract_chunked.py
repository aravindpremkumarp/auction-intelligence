"""reset_langextract_and_extract: chunked reads and the --keep-more-lots guard."""
from __future__ import annotations

import sys
import types

import pytest

import scripts.reset_langextract_and_extract as R


def _ent(li, start=0):
    return {"cls": "borrower", "text": "x", "start": start, "end": start + 1,
            "attrs": {"lot_index": str(li)}}


@pytest.fixture
def fake(monkeypatch):
    """Stub the model, the writes and the key-score stamp."""
    calls = {"extract": [], "writes": []}
    lx = types.ModuleType("pipeline.langextract_examples")

    def extract(text, **kw):
        calls["extract"].append(kw.get("expected_lot_count"))
        n = kw.get("expected_lot_count") or 1
        return types.SimpleNamespace(n=n)
    lx.extract = extract
    monkeypatch.setitem(sys.modules, "pipeline.langextract_examples", lx)
    monkeypatch.setattr(R, "_entities",
                        lambda res, text: [_ent(i, i) for i in range(1, res.n + 1)])
    monkeypatch.setattr(R, "validate_stored", lambda ents, **kw: {"score": 50})
    monkeypatch.setattr(R, "run_query",
                        lambda q, p=None, **kw: calls["writes"].append(p) or [])
    ke = types.ModuleType("pipeline.key_entities")
    ke.stamp_key_scores = lambda fns: None
    monkeypatch.setitem(sys.modules, "pipeline.key_entities", ke)
    return calls


def _notice(n):
    return "".join(f"No.{i}\nReserve price: Rs.{i},00,000/-\n" for i in range(1, n + 1))


def test_a_matching_multi_lot_notice_is_read_in_chunks(fake):
    d = {"filename": "f", "md": _notice(12), "notice_type": "multi",
         "expected_lot_count": 12, "roster": []}
    R._extract_one(d, batch=1, route=False)
    assert fake["extract"] == [5, 5, 2]


def test_an_unmatched_notice_is_read_whole(fake):
    d = {"filename": "f", "md": _notice(12), "notice_type": "multi",
         "expected_lot_count": 13, "roster": []}
    R._extract_one(d, batch=1, route=False)
    assert fake["extract"] == [13]


def test_keep_more_lots_refuses_a_weaker_run(fake, monkeypatch):
    monkeypatch.setattr(R, "_stored_lot_count", lambda fn: 20)
    d = {"filename": "f", "md": "text", "notice_type": "multi",
         "expected_lot_count": 3, "roster": []}
    with pytest.raises(ValueError, match="keeping the existing one"):
        R._extract_one(d, batch=1, route=False, keep_more_lots=True)
    assert fake["writes"] == []


def test_keep_more_lots_lets_a_stronger_run_through(fake, monkeypatch):
    monkeypatch.setattr(R, "_stored_lot_count", lambda fn: 2)
    d = {"filename": "f", "md": "text", "notice_type": "multi",
         "expected_lot_count": 3, "roster": []}
    R._extract_one(d, batch=1, route=False, keep_more_lots=True)
    assert len(fake["writes"]) == 1


def test_lot_count_ignores_entities_without_a_lot():
    ents = [_ent(1), _ent(1), _ent(2), {"cls": "contact", "attrs": {}}]
    assert R._lot_count(ents) == 2
