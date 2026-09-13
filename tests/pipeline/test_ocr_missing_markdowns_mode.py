"""The OCR script picks the Datalab tier from each notice's classification.

Single notices go fast, multi notices go accurate. A page OCR'd once for a
group of same-byte copies goes accurate if any copy is multi, and a notice
with no classification is never trusted to the fast tier. DB-free.
"""
from __future__ import annotations

import scripts.ocr_missing_markdowns as M


def _doc(name: str, notice_type: str | None, sha: str | None = None) -> dict:
    return {"filename": name, "file_path": f"notices/{name}",
            "notice_type": notice_type, "content_sha256": sha}


def test_single_goes_fast_and_multi_goes_accurate():
    docs = [_doc("s.jpg", "single", "sha-s"), _doc("m.jpg", "multi", "sha-m")]
    M.assign_modes(docs, docs)
    assert [d["mode"] for d in docs] == ["fast", "accurate"]


def test_a_page_shared_with_a_multi_copy_goes_accurate():
    leader = _doc("a.jpg", "single", "same-page")
    follower = _doc("b.jpg", "multi", "same-page")
    M.assign_modes([leader], [leader, follower])
    assert leader["mode"] == "accurate"


def test_unclassified_goes_accurate():
    docs = [_doc("u.jpg", None, "sha-u"), _doc("n.jpg", None)]
    M.assign_modes(docs, docs)
    assert [d["mode"] for d in docs] == ["accurate", "accurate"]


def test_unhashed_singles_do_not_share_a_multi_tier():
    single = _doc("s.jpg", "single")            # no hash
    multi = _doc("m.jpg", "multi")              # no hash
    M.assign_modes([single, multi], [single, multi])
    assert [single["mode"], multi["mode"]] == ["fast", "accurate"]


def test_forced_mode_wins():
    docs = [_doc("s.jpg", "single", "a"), _doc("m.jpg", "multi", "b")]
    M.assign_modes(docs, docs, forced="accurate")
    assert [d["mode"] for d in docs] == ["accurate", "accurate"]


def test_each_row_records_its_own_tier(monkeypatch):
    sent: list = []
    monkeypatch.setattr(M, "run_query", lambda q, p: sent.append(p) or [])
    ok = {"ok": True, "markdown": "x", "blocks": [], "score": 90, "flags": []}
    n = M.write_datalab([
        {**ok, "file_path": "notices/s.jpg", "mode": "fast"},
        {**ok, "file_path": "notices/m.jpg", "mode": "accurate"},
    ])
    assert n == 2
    assert [r["model"] for r in sent[0]["rows"]] == ["datalab-fast",
                                                     "datalab-accurate"]
