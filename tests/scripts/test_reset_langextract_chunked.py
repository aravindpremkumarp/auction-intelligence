"""reset_langextract_and_extract: chunked reads and the --keep-more-lots guard."""
from __future__ import annotations

import re
import sys
import types

import pytest

import scripts.reset_langextract_and_extract as R


def _ent(li, start=0):
    return {"cls": "borrower", "text": "x", "start": start, "end": start + 1,
            "attrs": {"lot_index": str(li)}}


def _terms(li, start=0):
    return {"cls": "auction_terms", "text": "x", "start": start, "end": start + 1,
            "attrs": {"lot_index": str(li), "reserve_price_num": "100000"}}


@pytest.fixture
def fake(monkeypatch):
    """Stub the model, the writes and the key-score stamp."""
    calls = {"extract": [], "writes": []}
    lx = types.ModuleType("pipeline.langextract_examples")

    def extract(text, **kw):
        calls["extract"].append(kw.get("expected_lot_count"))
        calls.setdefault("kw", []).append(kw)
        return types.SimpleNamespace(text=text, n=kw.get("expected_lot_count") or 1)
    lx.extract = extract
    monkeypatch.setitem(sys.modules, "pipeline.langextract_examples", lx)
    # `from pipeline import X` reads the package attribute first, which is the
    # real module once any earlier test imported it.
    import pipeline
    monkeypatch.setattr(pipeline, "langextract_examples", lx, raising=False)
    def entities(res, text):
        # one borrower per "No.N" block, numbered locally like the model does;
        # plain text (no blocks) gets n lots at the top
        found = [(m.start(), k) for k, m in
                 enumerate(re.finditer(r"No\.\d+", text), 1)]
        # each lot read in full: a borrower, its full_description and reserve
        return ([e for pos, k in found
                 for e in (_ent(k, pos), {**_ent(k, pos), "cls": "full_description"},
                           _terms(k, pos))]
                if found else [_ent(i, i) for i in range(1, res.n + 1)])
    monkeypatch.setattr(R, "_entities", entities)
    monkeypatch.setattr(R, "validate_stored", lambda ents, **kw: {"score": 50})
    monkeypatch.setattr(R, "run_query",
                        lambda q, p=None, **kw: calls["writes"].append(p) or [])
    ke = types.ModuleType("pipeline.key_entities")
    ke.stamp_key_scores = lambda fns: None
    monkeypatch.setitem(sys.modules, "pipeline.key_entities", ke)
    monkeypatch.setattr(pipeline, "key_entities", ke, raising=False)
    return calls


def _notice(n):
    return "".join(f"No.{i}\nReserve price: Rs.{i},00,000/-\n" for i in range(1, n + 1))


def test_a_big_notice_is_read_in_chunks_from_the_start(fake, monkeypatch):
    monkeypatch.setattr(R, "CHUNK_FIRST_AT", 12)
    d = {"filename": "f", "md": _notice(12), "notice_type": "multi",
         "expected_lot_count": 12, "roster": []}
    R._extract_one(d, batch=1, route=False)
    assert fake["extract"] == [5, 5, 2]
    assert "It holds lots 6–10" in fake["kw"][1]["extra"]


def test_a_smaller_notice_read_in_full_stays_one_read(fake):
    d = {"filename": "f", "md": _notice(12), "notice_type": "multi",
         "expected_lot_count": 12, "roster": []}
    R._extract_one(d, batch=1, route=False)
    assert fake["extract"] == [12]


def test_a_short_whole_read_falls_back_to_chunks(fake, monkeypatch):
    whole = R._entities

    def short_when_whole(res, text):
        ents = whole(res, text)
        if res.n == 12:     # the whole read: drop the last two lots
            ents = [e for e in ents if int(e["attrs"]["lot_index"]) <= 10]
        return ents
    monkeypatch.setattr(R, "_entities", short_when_whole)
    d = {"filename": "f", "md": _notice(12), "notice_type": "multi",
         "expected_lot_count": 12, "roster": []}
    R._extract_one(d, batch=1, route=False)
    assert fake["extract"] == [12, 5, 5, 2]
    assert R._lot_count(json_written(fake)) == 12


def json_written(fake):
    import json
    return json.loads(next(p["j"] for p in fake["writes"] if p and "j" in p))


def test_an_unmatched_notice_is_read_whole(fake):
    d = {"filename": "f", "md": _notice(12), "notice_type": "multi",
         "expected_lot_count": 13, "roster": []}
    R._extract_one(d, batch=1, route=False)
    assert fake["extract"] == [13]
    assert fake["kw"][0]["extra"] is None


def _stored(monkeypatch, ents, text_changed=False):
    monkeypatch.setattr(R, "_stored", lambda fn: {"entities": ents,
                                                  "text_changed": text_changed})


def test_keep_better_refuses_a_weaker_run(fake, monkeypatch):
    _stored(monkeypatch, [_ent(i) for i in range(1, 21)])
    d = {"filename": "f", "md": "text", "notice_type": "multi",
         "expected_lot_count": 20, "roster": []}
    with pytest.raises(R.KeptExisting, match="keeping the existing one"):
        R._extract_one(d, batch=1, route=False, keep_better=True)
    assert fake["writes"] == []


def test_keep_better_lets_a_stronger_run_through(fake, monkeypatch):
    _stored(monkeypatch, [_ent(1), _ent(2)])
    d = {"filename": "f", "md": "text", "notice_type": "multi",
         "expected_lot_count": 3, "roster": []}
    R._extract_one(d, batch=1, route=False, keep_better=True)
    assert len(fake["writes"]) == 1


def test_keep_better_skips_the_comparison_when_the_text_changed(fake, monkeypatch):
    _stored(monkeypatch, [_ent(i) for i in range(1, 21)], text_changed=True)
    d = {"filename": "f", "md": "text", "notice_type": "multi",
         "expected_lot_count": 20, "roster": []}
    R._extract_one(d, batch=1, route=False, keep_better=True)
    assert len(fake["writes"]) == 1


def test_a_kept_notice_is_not_counted_as_a_failure(fake, monkeypatch, capsys):
    _stored(monkeypatch, [_ent(i) for i in range(1, 21)])
    monkeypatch.setattr(R, "_next_batch", lambda: 1)
    monkeypatch.setattr(R, "_plan_groups", lambda docs, **kw: (docs, 0))
    d = {"filename": "f", "md": "text", "notice_type": "multi",
         "expected_lot_count": 20, "roster": []}
    assert R.extract_docs([d], keep_better=True) == 0
    out = capsys.readouterr().out
    assert "[kept] f" in out and "kept 1 existing, failed 0" in out


def test_lot_count_ignores_entities_without_a_lot():
    ents = [_ent(1), _ent(1), _ent(2), {"cls": "contact", "attrs": {}}]
    assert R._lot_count(ents) == 2
