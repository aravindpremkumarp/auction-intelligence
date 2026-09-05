"""One page under six file names is grouped, merged and planned as one.

The case is the corpus's own: KARNTK1781938…-1781939…, six uploads 1.6 seconds
apart holding one Karnataka Bank notice that advertises six lots, each Document
linked to a different auction. DB-free — every function under test is pure.
"""
from __future__ import annotations

from pipeline.notice_twins import (
    group_twins,
    merge_rosters,
    plan_reuse,
    source_key,
    text_key,
)


KARNTK = [f"KARNTK178193{n}.jpg" for n in
          ("83495370", "84812340", "86104510", "87395680", "89988120", "91325440")]


def _doc(fn: str, *, sha: str | None = None, md: str | None = None,
         roster: list | None = None) -> dict:
    return {"filename": fn, "file_path": f"notices/{fn}",
            "content_sha256": sha, "md": md, "roster": roster or []}


# ── keys ────────────────────────────────────────────────────────────────────

def test_source_key_is_the_stored_hash_and_never_the_filename():
    assert source_key(_doc("a.jpg", sha="abc")) == "abc"
    # The file name is exactly what the portal varies, so an unhashed document
    # is unidentified — not a group of one name.
    assert source_key(_doc("a.jpg")) is None
    assert source_key(_doc("a.jpg", sha="   ")) is None


def test_text_key_separates_markdown_that_differs_by_one_character():
    a = text_key({"md": "Sale notice"})
    b = text_key({"md": "Sale notice "})
    assert a and b and a != b
    assert text_key({"md": ""}) is None
    assert text_key({"markdown": "x"}) == text_key({"md": "x"})


# ── grouping ────────────────────────────────────────────────────────────────

def test_six_copies_of_one_page_group_as_one():
    docs = [_doc(fn, sha="deadbeef") for fn in KARNTK]
    groups = group_twins(docs, key=source_key)
    assert len(groups) == 1
    assert [d["filename"] for d in groups[0]] == KARNTK


def test_unkeyable_documents_stay_separate():
    # No hash is not a match with another no-hash: unknown never groups.
    groups = group_twins([_doc("a.jpg"), _doc("b.jpg")], key=source_key)
    assert [len(g) for g in groups] == [1, 1]


def test_group_order_follows_input():
    docs = [_doc("a.jpg", sha="1"), _doc("b.jpg", sha="2"),
            _doc("c.jpg", sha="1")]
    groups = group_twins(docs, key=source_key)
    assert [[d["filename"] for d in g] for g in groups] == \
        [["a.jpg", "c.jpg"], ["b.jpg"]]


def test_same_bytes_but_different_markdown_do_not_share_an_extraction():
    # Two byte-identical files OCR'd by different engines. Sharing on the file
    # hash would carry offsets into text they do not describe.
    docs = [_doc("a.jpg", sha="same", md="# A"),
            _doc("b.jpg", sha="same", md="# B")]
    assert len(group_twins(docs, key=source_key)) == 1
    assert len(group_twins(docs, key=text_key)) == 2


# ── roster union ────────────────────────────────────────────────────────────

def test_merge_rosters_restores_the_lots_the_page_advertises():
    group = [_doc(fn, roster=[{"aid": f"AUC{i}", "village": "Ambattur"}])
             for i, fn in enumerate(KARNTK)]
    merged = merge_rosters(group)
    assert [r["aid"] for r in merged] == [f"AUC{i}" for i in range(6)]


def test_merge_rosters_dedupes_by_aid_first_wins():
    group = [_doc("a.jpg", roster=[{"aid": "A", "village": "Avadi"}]),
             _doc("b.jpg", roster=[{"aid": "A", "village": "ignored"},
                                   {"aid": "B"}])]
    merged = merge_rosters(group)
    assert merged == [{"aid": "A", "village": "Avadi"}, {"aid": "B"}]


def test_merge_rosters_keeps_rows_without_an_aid():
    group = [_doc("a.jpg", roster=[{"reserve": 100}, {"aid": "A"}])]
    assert merge_rosters(group) == [{"reserve": 100}, {"aid": "A"}]


# ── planning ────────────────────────────────────────────────────────────────

def test_plan_reuse_runs_the_leader_and_copies_to_the_rest():
    docs = [_doc(fn, sha="deadbeef") for fn in KARNTK]
    to_run, copies = plan_reuse(docs, donors={}, key=source_key)
    assert [d["filename"] for d in to_run] == [KARNTK[0]]
    assert [c["filename"] for c in copies] == KARNTK[1:]
    assert {c["donor"] for c in copies} == {KARNTK[0]}
    assert copies[0]["file_path"] == f"notices/{KARNTK[1]}"


def test_plan_reuse_skips_the_pass_entirely_when_the_graph_has_the_page():
    docs = [_doc(fn, sha="deadbeef") for fn in KARNTK]
    to_run, copies = plan_reuse(docs, donors={"deadbeef": "OLD.jpg"},
                                key=source_key)
    assert to_run == []
    assert len(copies) == 6
    assert {c["donor"] for c in copies} == {"OLD.jpg"}


def test_plan_reuse_leaves_unhashed_documents_working_as_before():
    docs = [_doc("a.jpg"), _doc("b.jpg", sha="x"), _doc("c.jpg")]
    to_run, copies = plan_reuse(docs, donors={}, key=source_key)
    assert [d["filename"] for d in to_run] == ["a.jpg", "b.jpg", "c.jpg"]
    assert copies == []


def test_plan_reuse_mixes_donors_and_leaders_across_groups():
    docs = [_doc("a1.jpg", sha="A"), _doc("a2.jpg", sha="A"),
            _doc("b1.jpg", sha="B"), _doc("b2.jpg", sha="B")]
    to_run, copies = plan_reuse(docs, donors={"B": "OLD.jpg"}, key=source_key)
    assert [d["filename"] for d in to_run] == ["a1.jpg"]
    assert {(c["filename"], c["donor"]) for c in copies} == {
        ("a2.jpg", "a1.jpg"), ("b1.jpg", "OLD.jpg"), ("b2.jpg", "OLD.jpg")}
