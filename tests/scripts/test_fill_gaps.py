"""scripts/fill_gaps: a gap two reads cannot fill is marked, not re-read forever."""
from __future__ import annotations

import scripts.fill_gaps as F

MD = ("SALE NOTICE. Lot 1: land at Sy No 12/1, Village V, 1200 sq.ft. "
      "Possession: symbolic. Auction on 01.10.2026.\n")


def _e(cls, text, md=MD, lot="1", **attrs):
    s = md.index(text)
    return {"cls": cls, "text": text, "start": s, "end": s + len(text),
            "attrs": {"lot_index": lot, **attrs}}


def _setup(monkeypatch, corrections=None, finds=()):
    stored = [_e("full_description", "land at Sy No 12/1, Village V, 1200 sq.ft."),
              _e("location", "Village V"), _e("extent", "1200 sq.ft."),
              _e("property", "land", property_type="land")]
    calls, out = [], {}
    monkeypatch.setattr(F, "_stored", lambda fn: {"entities": stored,
                                                  "text_changed": False})
    monkeypatch.setattr(F, "_corrections", lambda fn: corrections or {})

    def make_reader(nt, strong=False):
        def read(text, keys, hint):
            calls.append(("strong" if strong else "lean", sorted(keys)))
            return [f(text) for f in finds]
        return read
    monkeypatch.setattr(F, "make_reader", make_reader)
    monkeypatch.setattr(F, "write_extraction",
                        lambda d, ents, b, **k: out.setdefault("ents", ents))
    monkeypatch.setattr(F, "write_marks",
                        lambda fn, m: out.setdefault("marks", m))
    return calls, out


D = {"filename": "f", "md": MD, "expected_lot_count": 1}


def test_a_fact_two_reads_miss_is_marked(monkeypatch):
    calls, out = _setup(monkeypatch)
    msg = F.fill_one(D, set(F.KEYS), 1, dry_run=False, max_lots=5)
    # possession has clues ("symbolic"), so it is read — lean, then strong
    assert calls == [("lean", ["auction_date", "possession_type", "reserve_price"]),
                     ("strong", ["auction_date", "possession_type", "reserve_price"])]
    assert set(out["marks"]) == {"absent:1:possession_type",
                                 "unfound:1:reserve_price",
                                 "unfound:1:auction_date"}
    assert "1 marked not in notice, 2 left for a person" in msg


def test_a_fact_the_lean_read_finds_skips_the_strong_read(monkeypatch):
    finds = [lambda t: _e("property", "symbolic", t, possession_type="symbolic"),
             lambda t: _e("auction_terms", "Auction on 01.10.2026", t,
                          auction_start_dt="2026-10-01", reserve_price_num="950000")]
    calls, out = _setup(monkeypatch, finds=finds)
    msg = F.fill_one(D, set(F.KEYS), 1, dry_run=False, max_lots=5)
    assert [c[0] for c in calls] == ["lean"]
    assert out["marks"] == {} and "saved" in msg


def test_marked_facts_are_not_read_again(monkeypatch):
    corr = {"unfound:1:reserve_price": {"by": "auto"},
            "unfound:1:auction_date": {"by": "auto"},
            "absent:1:possession_type": {"by": "auto"}}
    calls, _ = _setup(monkeypatch, corrections=corr)
    assert F.fill_one(D, set(F.KEYS), 1, dry_run=False, max_lots=5) == "no gaps"
    assert calls == []


def test_a_failed_read_marks_nothing(monkeypatch):
    calls, out = _setup(monkeypatch)

    def make_reader(nt, strong=False):
        def read(text, keys, hint):
            raise RuntimeError("provider down")
        return read
    monkeypatch.setattr(F, "make_reader", make_reader)
    F.fill_one(D, set(F.KEYS), 1, dry_run=False, max_lots=5)
    assert out["marks"] == {}
