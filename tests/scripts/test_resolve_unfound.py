"""scripts/resolve_unfound: not-found facts worked without a person."""
from __future__ import annotations

import scripts.resolve_unfound as R


def _setup(monkeypatch, *, image_fixes, still):
    calls, marks = [], {}
    monkeypatch.setattr(R, "image_check", lambda pages: (calls.append(("image", pages))
                                                          or (image_fixes, "ok")))
    monkeypatch.setattr(R, "reread", lambda fn, b: calls.append(("reread", fn)) or "saved")
    monkeypatch.setattr(R, "still_missing", lambda fn, asked: set(still) & set(asked))
    monkeypatch.setattr(R, "write_marks", lambda fn, m: marks.update(m))
    import scripts.fill_gaps as F
    import scripts.reset_langextract_and_extract as X
    monkeypatch.setattr(F, "fill_one", lambda *a, **k: calls.append(("gaps",)) or "no gaps")
    monkeypatch.setattr(X, "select_only_docs", lambda fns: [{"filename": fns[0]}])
    return calls, marks


def test_lost_from_text_goes_to_the_image_and_rereads_only_if_it_changed(monkeypatch):
    t = {"filename": "f", "pages": ["f", "f2"],
         "marks": {("1", "reserve_price"): R.RULE_LOST_FROM_TEXT}}
    calls, marks = _setup(monkeypatch, image_fixes=True, still=[])
    msg = R.resolve(t, 1, dry_run=False)
    assert calls == [("image", ["f", "f2"]), ("reread", "f"), ("gaps",)]
    assert marks == {} and "1 found, 0 need a person" in msg


def test_a_clean_image_with_nothing_read_missed_goes_straight_to_a_person(monkeypatch):
    t = {"filename": "f", "pages": ["f"],
         "marks": {("1", "reserve_price"): R.RULE_LOST_FROM_TEXT}}
    calls, marks = _setup(monkeypatch, image_fixes=False, still=[("1", "reserve_price")])
    R.resolve(t, 1, dry_run=False)
    assert [c[0] for c in calls] == ["image"]           # no costly re-read
    assert marks["unfound:1:reserve_price"]["rule"] == R.RULE_NEEDS_PERSON


def test_read_missed_is_reread_even_when_the_image_is_clean(monkeypatch):
    t = {"filename": "f", "pages": ["f"],
         "marks": {("2", "auction_date"): R.RULE_READ_MISSED}}
    calls, marks = _setup(monkeypatch, image_fixes=False, still=[])
    R.resolve(t, 1, dry_run=False)
    assert [c[0] for c in calls] == ["image", "reread", "gaps"]
    assert marks == {}


def test_only_open_automatic_marks_are_worked():
    corr = {"unfound:1:reserve_price": {"by": "auto", "rule": "lost_from_text"},
            "unfound:2:reserve_price": {"by": "auto", "rule": "needs_person"},
            "unfound:3:auction_date": {"by": "rev@x", "rule": "reviewer_undo"},
            "absent:4:extent": {"by": "auto", "rule": "no_clue"}}
    assert R.open_marks(corr) == {("1", "reserve_price"): "lost_from_text"}
