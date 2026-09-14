"""What the stitch script writes, and when it refuses to.

The decisions (which files are pages) are tested in
tests/pipeline/test_notice_pages.py. Here the subject is the script's own job:
building the write from stored rows and sending the right Cypher. Neo4j is
stood in for by a recorder, as in test_reset_langextract_refresh.py.
"""
from __future__ import annotations

import scripts.stitch_sibling_pages as S


class _Capture:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls = []

    def __call__(self, cypher, params=None, **kw):
        self.calls.append((cypher, params))
        return self.rows

    @property
    def cypher(self):
        return self.calls[-1][0]

    @property
    def params(self):
        return self.calls[-1][1]


def _doc(fn, md, elc=None, stitched_markdown=None, has_extraction=False):
    return {"filename": fn, "markdown": md, "expected_lot_count": elc,
            "stitched_markdown": stitched_markdown,
            "has_extraction": has_extraction}


GROUP = {"pages": ["p1.jpg", "p2.jpg"], "twins": ["p1-copy.jpg"]}
DOCS = {"p1.jpg": _doc("p1.jpg", "PAGE ONE", 14),
        "p2.jpg": _doc("p2.jpg", "PAGE TWO", 10),
        "p1-copy.jpg": _doc("p1-copy.jpg", "PAGE ONE", 14)}


# ── build ───────────────────────────────────────────────────────────────────

def test_build_joins_pages_and_points_twins_at_the_leader():
    b = S.build(GROUP, DOCS)
    assert b["leader"] == "p1.jpg"
    assert b["followers"] == ["p2.jpg", "p1-copy.jpg"]
    assert b["markdown"] == "PAGE ONE" + S.SEPARATOR + "PAGE TWO"
    assert b["offsets"] == [0, len("PAGE ONE") + len(S.SEPARATOR)]
    assert b["pages"] == ["p1.jpg", "p2.jpg"]
    assert b["expected_lot_count"] == 24
    assert b["changed"] is True


def test_build_reports_unchanged_when_the_leader_already_holds_this_text():
    docs = dict(DOCS)
    docs["p1.jpg"] = _doc("p1.jpg", "PAGE ONE", 14,
                          stitched_markdown="PAGE ONE" + S.SEPARATOR + "PAGE TWO")
    assert S.build(GROUP, docs)["changed"] is False


# ── plan (--only / --skip) ──────────────────────────────────────────────────

def _rows():
    return [{"listing": "L1", "filename": "p1.jpg", "content_key": "s1", "position": 0},
            {"listing": "L1", "filename": "p2.jpg", "content_key": "s2", "position": 1},
            {"listing": "L2", "filename": "q1.jpg", "content_key": "s3", "position": 0},
            {"listing": "L2", "filename": "q2.jpg", "content_key": "s4", "position": 1}]


def test_skip_drops_a_group_that_names_any_member():
    groups, _ = S.plan(_rows(), only=None, skip={"q2.jpg"})
    assert [g["pages"] for g in groups] == [["p1.jpg", "p2.jpg"]]


def test_only_keeps_just_the_named_leaders():
    groups, _ = S.plan(_rows(), only={"q1.jpg"}, skip=set())
    assert [g["pages"] for g in groups] == [["q1.jpg", "q2.jpg"]]


def test_only_forces_an_ambiguous_group_by_its_leader():
    rows = _rows() + [{"listing": "L3", "filename": "p1.jpg", "content_key": "s1", "position": 0}]
    groups, ambiguous = S.plan(rows, only={"p1.jpg"}, skip=set())
    assert [g["pages"] for g in groups] == [["p1.jpg", "p2.jpg"]]
    assert ambiguous == []


# ── writes ──────────────────────────────────────────────────────────────────

def test_apply_writes_leader_fields_and_follower_pointer(monkeypatch):
    cap = _Capture(rows=[{"followers": 2}])
    monkeypatch.setattr(S, "run_query", cap)
    n = S.apply_group(S.build(GROUP, DOCS))
    assert n == 2
    c = cap.cypher
    assert "l.stitched_markdown = $md" in c
    assert "l.stitched_pages = $pages" in c
    assert "l.stitched_page_offsets = $offsets" in c
    assert "l.stitched_at = datetime()" in c
    assert "l.stitched_expected_lot_count = $elc" in c
    assert "f.stitched_into = $leader" in c
    assert cap.params["leader"] == "p1.jpg"
    assert cap.params["followers"] == ["p2.jpg", "p1-copy.jpg"]
    assert cap.params["elc"] == 24


def test_apply_stamps_stale_only_where_an_extraction_exists(monkeypatch):
    cap = _Capture(rows=[{"followers": 2}])
    monkeypatch.setattr(S, "run_query", cap)
    S.apply_group(S.build(GROUP, DOCS))
    assert ("l.extraction_stale_at = CASE WHEN l.extraction_json IS NOT NULL "
            "THEN datetime() ELSE l.extraction_stale_at END") in cap.cypher


def test_unstitch_removes_every_stitch_property_and_stamps_stale(monkeypatch):
    cap = _Capture(rows=[{"followers": 1}])
    monkeypatch.setattr(S, "run_query", cap)
    assert S.unstitch("p1.jpg") == 1
    c = cap.cypher
    for prop in ("stitched_markdown", "stitched_pages", "stitched_page_offsets",
                 "stitched_at", "stitched_expected_lot_count"):
        assert f"l.{prop}" in c
    assert "REMOVE f.stitched_into" in c
    assert "f.extraction_stale_at = CASE" in c
    assert "l.extraction_stale_at = CASE" in c
    assert cap.params == {"leader": "p1.jpg"}


def test_dry_run_writes_nothing(monkeypatch, capsys):
    reads = _Capture(rows=[])
    writes = _Capture()
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    assert S.main(["--dry-run"]) == 0
    assert writes.calls == []
