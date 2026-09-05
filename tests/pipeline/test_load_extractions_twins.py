"""Extraction runs once per page, not once per file name.

Six Documents holding one Karnataka Bank notice used to be six multi-minute
model calls, each told — by its own one-row portal roster — to find one lot on a
page that advertises six. These guard both halves of the fix: one call for the
group, and the group's rosters merged into it.

DB-free: `_find_donor` and `_copy_extraction` are the only Neo4j touchpoints in
the planner and are monkeypatched.
"""
from __future__ import annotations

import pipeline.load_extractions as M


MD = "SALE NOTICE OF IMMOVABLE PROPERTIES (E-AUCTION)\n1. Chennai - Ambattur"


def _doc(fn: str, md: str = MD, aid: str = "AUC1") -> dict:
    return {"filename": fn, "md": md, "notice_type": "multi",
            "expected_lot_count": 6, "roster": [{"aid": aid, "village": "X"}]}


def _no_donor(monkeypatch):
    monkeypatch.setattr(M, "_find_donor", lambda md: None)


def test_six_copies_become_one_extraction(monkeypatch):
    _no_donor(monkeypatch)
    docs = [_doc(f"KARNTK{n}.jpg", aid=f"AUC{n}") for n in range(6)]
    leaders, reused = M._plan_groups(docs, force=False, batch=7)
    assert reused == 0
    assert len(leaders) == 1
    assert leaders[0]["filename"] == "KARNTK0.jpg"
    assert leaders[0]["twins"] == [f"KARNTK{n}.jpg" for n in range(6)]


def test_the_leader_sees_every_lot_the_page_advertises(monkeypatch):
    _no_donor(monkeypatch)
    docs = [_doc(f"KARNTK{n}.jpg", aid=f"AUC{n}") for n in range(6)]
    leaders, _ = M._plan_groups(docs, force=False, batch=7)
    assert [r["aid"] for r in leaders[0]["roster"]] == [f"AUC{n}" for n in range(6)]


def test_documents_with_different_markdown_stay_separate(monkeypatch):
    _no_donor(monkeypatch)
    leaders, _ = M._plan_groups([_doc("a.jpg"), _doc("b.jpg", md=MD + " ")],
                                force=False, batch=1)
    assert [d["filename"] for d in leaders] == ["a.jpg", "b.jpg"]


def test_a_page_already_extracted_elsewhere_is_copied_not_re_run(monkeypatch):
    donor = {"filename": "OLD.jpg", "j": "[]", "score": 88, "model": "m"}
    monkeypatch.setattr(M, "_find_donor", lambda md: donor)
    seen: dict = {}

    def fake_copy(d, targets, batch):
        seen.update(donor=d["filename"], targets=targets, batch=batch)
        return len(targets)

    monkeypatch.setattr(M, "_copy_extraction", fake_copy)
    leaders, reused = M._plan_groups([_doc("a.jpg"), _doc("b.jpg")],
                                     force=False, batch=9)
    assert leaders == []
    assert reused == 2
    assert seen == {"donor": "OLD.jpg", "targets": ["a.jpg", "b.jpg"], "batch": 9}


def test_a_donor_inside_the_group_is_not_treated_as_a_donor(monkeypatch):
    # _fetch only returns Documents without an extraction, but a donor lookup
    # racing a sibling write must not make the group copy from itself.
    monkeypatch.setattr(
        M, "_find_donor",
        lambda md: {"filename": "a.jpg", "j": "[]", "score": 1, "model": "m"})
    monkeypatch.setattr(M, "_copy_extraction",
                        lambda d, t, b: (_ for _ in ()).throw(AssertionError))
    leaders, reused = M._plan_groups([_doc("a.jpg"), _doc("b.jpg")],
                                     force=False, batch=1)
    assert [d["filename"] for d in leaders] == ["a.jpg"]
    assert reused == 0


def test_force_re_runs_the_model_but_still_only_once_per_page(monkeypatch):
    monkeypatch.setattr(M, "_find_donor",
                        lambda md: (_ for _ in ()).throw(AssertionError))
    docs = [_doc(f"KARNTK{n}.jpg", aid=f"AUC{n}") for n in range(6)]
    leaders, reused = M._plan_groups(docs, force=True, batch=2)
    assert reused == 0
    assert len(leaders) == 1
    assert len(leaders[0]["twins"]) == 6


def test_the_reextract_script_never_serves_a_copy_when_re_running_is_the_point(
        monkeypatch):
    """--no-resume and --stale exist to run the model again; a cached copy of an
    earlier extraction is the one thing they must not be given. Grouping still
    applies, so a six-copy notice is still one call."""
    import scripts.reset_langextract_and_extract as S

    seen: list = []
    monkeypatch.setattr(S, "_plan_groups",
                        lambda docs, *, force, batch: (seen.append(force), ([], 0))[1])
    monkeypatch.setattr(S, "_next_batch", lambda: 1)
    for reuse in (True, False):
        S.extract_docs([_doc("a.jpg")], concurrency=1, reuse=reuse)
    assert seen == [False, True]   # reuse=True -> force=False, and vice versa


def test_planning_does_not_mutate_the_fetched_rows(monkeypatch):
    _no_donor(monkeypatch)
    docs = [_doc("a.jpg", aid="A"), _doc("b.jpg", aid="B")]
    M._plan_groups(docs, force=False, batch=1)
    assert docs[0]["roster"] == [{"aid": "A", "village": "X"}]
    assert "twins" not in docs[0]
