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


def test_build_reports_unchanged_when_the_leader_already_holds_this_text_and_count():
    docs = dict(DOCS)
    docs["p1.jpg"] = dict(_doc("p1.jpg", "PAGE ONE", 14,
                               stitched_markdown="PAGE ONE" + S.SEPARATOR + "PAGE TWO"),
                          stitched_expected_lot_count=24)
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


def test_only_rejects_follower_name_and_keeps_group_ambiguous():
    # Regression: naming a follower instead of the leader should not silently drop the group.
    rows = _rows() + [{"listing": "L3", "filename": "p1.jpg", "content_key": "s1", "position": 0}]
    groups, ambiguous = S.plan(rows, only={"p2.jpg"}, skip=set())
    assert groups == []  # no groups (p2.jpg is not a leader)
    # The p1.jpg/p2.jpg entry should remain in ambiguous, not vanish
    assert any("p1.jpg" in a["filenames"] and "p2.jpg" in a["filenames"] for a in ambiguous)


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
    assert "REMOVE x.stitched_into" in c
    assert "x.extraction_stale_at = CASE" in c
    assert "l.extraction_stale_at = CASE" in c
    assert cap.params == {"leader": "p1.jpg"}


def test_dry_run_writes_nothing(monkeypatch, capsys):
    reads = _Capture(rows=[])
    writes = _Capture()
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    assert S.main(["--dry-run"]) == 0
    assert writes.calls == []


# ── detection reads (a) candidates once and (b) every edge of them ──────────

import pipeline.promote_extractions as P  # noqa: E402


def _cand(fn, md, elc=None, sha=None, stitched_markdown=None, stitched_elc=None,
          has_extraction=False):
    return {"filename": fn, "markdown": md, "content_sha256": sha,
            "expected_lot_count": elc, "stitched_markdown": stitched_markdown,
            "stitched_expected_lot_count": stitched_elc,
            "has_extraction": has_extraction}


def _edge(listing, fn, position):
    return {"listing": listing, "filename": fn, "position": position}


class _Reads:
    """Answers each read the script sends by which constant it is."""

    def __init__(self, cands, edges, lots=None, rejections=None):
        self.cands, self.edges, self.lots = cands, edges, lots or {}
        self.rejections = rejections or []
        self.calls = []

    def __call__(self, cypher, params=None, **kw):
        self.calls.append((cypher, params))
        if cypher == S.CANDIDATES_CYPHER:
            return [dict(c) for c in self.cands]
        if cypher == S.EDGES_CYPHER:
            return [dict(e) for e in self.edges if e["filename"] in params["filenames"]]
        if cypher == S.FOLLOWER_LOTS_CYPHER:
            return [{"filename": fn, "lots": self.lots.get(fn, 0)}
                    for fn in params["filenames"]]
        if cypher == S.REJECTIONS_CYPHER:
            return [dict(r) for r in self.rejections]
        raise AssertionError(f"unexpected read: {cypher[:60]}")


def test_candidate_query_returns_each_document_once_with_both_counts():
    c = S.CANDIDATES_CYPHER
    assert "size(docs) >= 2" in c
    assert "WITH DISTINCT d" in c
    assert "d.markdown AS markdown" in c
    assert "d.stitched_expected_lot_count AS stitched_expected_lot_count" in c


def test_edge_query_keeps_single_document_listings_and_carries_no_markdown():
    e = S.EDGES_CYPHER
    assert "UNWIND $filenames AS fn" in e
    assert "size(docs)" not in e
    assert "markdown" not in e
    assert "AS position" in e and "AS listing" in e


def test_fetch_rows_builds_detector_rows_from_edges_and_keys_from_candidates(monkeypatch):
    reads = _Reads([_cand("p1.jpg", "ONE"), _cand("p2.jpg", "TWO")],
                   [_edge("L1", "p1.jpg", 0), _edge("L1", "p2.jpg", 1),
                    _edge("L3", "p2.jpg", 0)])
    monkeypatch.setattr(S, "run_read_query", reads)
    rows, docs = S.fetch_rows()
    assert reads.calls[1][1] == {"filenames": ["p1.jpg", "p2.jpg"]}
    assert sorted(docs) == ["p1.jpg", "p2.jpg"]
    assert all(set(r) == {"listing", "filename", "content_key", "position"} for r in rows)
    assert len(rows) == 3


def test_a_listing_holding_only_page_two_blocks_the_stitch(monkeypatch, capsys):
    reads = _Reads([_cand("AXIS-1.jpg", "ONE"), _cand("AXIS-2.jpg", "TWO")],
                   [_edge("L1", "AXIS-1.jpg", 0), _edge("L1", "AXIS-2.jpg", 1),
                    _edge("L3", "AXIS-2.jpg", 0)])
    writes = _Capture()
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    assert S.main(["--apply"]) == 0
    out = capsys.readouterr().out
    assert "[ambiguous] AXIS-1.jpg | AXIS-2.jpg: listing sets differ" in out
    assert writes.calls == []


def test_content_key_is_the_markdown_hash_whatever_the_byte_hash_says():
    assert (S._content_key({"markdown": "SAME", "content_sha256": "abc"})
            == S._content_key({"markdown": "SAME"}))


def test_same_markdown_with_and_without_a_sha_are_twins_not_pages(monkeypatch):
    reads = _Reads([_cand("a.jpg", "SAME", sha="abc"), _cand("a-copy.jpg", "SAME")],
                   [_edge("L1", "a.jpg", 0), _edge("L1", "a-copy.jpg", 1)])
    monkeypatch.setattr(S, "run_read_query", reads)
    rows, _ = S.fetch_rows()
    groups, ambiguous = S.plan(rows, only=None, skip=set())
    assert groups == [] and ambiguous == []


def test_only_never_forces_a_twin_exclusion_into_a_group():
    rows = [{"listing": "L1", "filename": "p1.jpg", "content_key": "s1", "position": 0},
            {"listing": "L1", "filename": "p1-copy.jpg", "content_key": "s1", "position": 1},
            {"listing": "L1", "filename": "p2.jpg", "content_key": "s2", "position": 2},
            {"listing": "L9", "filename": "p1-copy.jpg", "content_key": "s1", "position": 0}]
    groups, ambiguous = S.plan(rows, only={"p1.jpg"}, skip=set())
    assert groups == [{"pages": ["p1.jpg", "p2.jpg"], "twins": []}]
    assert [a["filenames"] for a in ambiguous] == [["p1-copy.jpg", "p1.jpg"]]


def test_twin_exclusion_prints_like_any_ambiguous_entry(capsys):
    S._print_plan([], [{"filenames": ["p1-copy.jpg", "p1.jpg"],
                        "reason": "twin outside group: p1-copy.jpg is on 2 listings, p1.jpg is on 1"}])
    assert ("[ambiguous] p1-copy.jpg | p1.jpg: twin outside group: "
            "p1-copy.jpg is on 2 listings, p1.jpg is on 1") in capsys.readouterr().out


# ── changed: text OR lot count ──────────────────────────────────────────────

def test_build_reports_changed_when_only_the_lot_count_differs():
    docs = dict(DOCS)
    docs["p1.jpg"] = dict(_doc("p1.jpg", "PAGE ONE", 14,
                               stitched_markdown="PAGE ONE" + S.SEPARATOR + "PAGE TWO"),
                          stitched_expected_lot_count=20)
    assert S.build(GROUP, docs)["changed"] is True


# ── minors on the write Cypher ──────────────────────────────────────────────

def test_apply_clears_a_leaders_own_follower_pointer():
    assert "REMOVE l.stitched_into" in S.APPLY_CYPHER


def test_apply_clears_a_followers_stale_stamp():
    assert "REMOVE f.extraction_stale_at" in S.APPLY_CYPHER


def test_unstitch_guards_follower_writes_against_null():
    assert "FOREACH (x IN CASE WHEN f IS NULL THEN [] ELSE [f] END |" in S.UNSTITCH_CYPHER


# ── follower lots are cleared on apply ──────────────────────────────────────

def test_clearing_follower_lots_reuses_promotes_per_document_rebuild(monkeypatch):
    cap = _Capture(rows=[{"lots": 3}])
    monkeypatch.setattr(P, "run_query", cap)
    assert S.clear_follower_lots(["p2.jpg", "p1-copy.jpg"]) == {"p2.jpg": 3, "p1-copy.jpg": 3}
    assert cap.calls == [(P._REBUILD_DOC_LOTS, {"filename": "p2.jpg"}),
                         (P._REBUILD_DOC_LOTS, {"filename": "p1-copy.jpg"})]


def _pair_world(lots):
    """p1/p2 (+ twin p1-copy) to be written; q1/q2 already stitched, unchanged."""
    cands = [_cand("p1.jpg", "PAGE ONE", 14), _cand("p2.jpg", "PAGE TWO", 10),
             _cand("p1-copy.jpg", "PAGE ONE", 14),
             _cand("q1.jpg", "Q ONE", 1, stitched_markdown="Q ONE" + S.SEPARATOR + "Q TWO",
                   stitched_elc=2),
             _cand("q2.jpg", "Q TWO", 1)]
    edges = []
    for a in ("L1", "L2"):
        edges += [_edge(a, "p1.jpg", 0), _edge(a, "p1-copy.jpg", 1), _edge(a, "p2.jpg", 2)]
    edges += [_edge("L5", "q1.jpg", 0), _edge("L5", "q2.jpg", 1)]
    return _Reads(cands, edges, lots)


def test_dry_run_prints_follower_lot_counts_and_deletes_nothing(monkeypatch, capsys):
    reads = _pair_world({"p2.jpg": 6, "p1-copy.jpg": 0})
    writes, promote_writes = _Capture(), _Capture()
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    monkeypatch.setattr(P, "run_query", promote_writes)
    assert S.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "[lots to clear] p2.jpg: 6" in out
    assert "[lots to clear] p1-copy.jpg: 0" in out
    assert "q2.jpg:" not in out  # an unchanged group is not written, so not cleared
    lot_reads = [p for c, p in reads.calls if c == S.FOLLOWER_LOTS_CYPHER]
    assert lot_reads == [{"filenames": ["p2.jpg", "p1-copy.jpg"]}]
    assert "HAS_LOT" in S.FOLLOWER_LOTS_CYPHER
    assert writes.calls == [] and promote_writes.calls == []


def test_apply_writes_the_stitch_and_clears_lots_for_followers_only(monkeypatch, capsys):
    reads = _pair_world({"p2.jpg": 6})
    writes = _Capture(rows=[{"followers": 2}])
    promote_writes = _Capture(rows=[{"lots": 6}])
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    monkeypatch.setattr(P, "run_query", promote_writes)
    assert S.main(["--apply"]) == 0
    assert [(c == S.APPLY_CYPHER, p["leader"]) for c, p in writes.calls] == [(True, "p1.jpg")]
    assert promote_writes.calls == [(P._REBUILD_DOC_LOTS, {"filename": "p2.jpg"}),
                                    (P._REBUILD_DOC_LOTS, {"filename": "p1-copy.jpg"})]


def test_keep_follower_lots_skips_the_deletion(monkeypatch, capsys):
    reads = _pair_world({"p2.jpg": 6})
    writes = _Capture(rows=[{"followers": 2}])
    promote_writes = _Capture(rows=[{"lots": 6}])
    monkeypatch.setattr(S, "run_read_query", reads)
    monkeypatch.setattr(S, "run_query", writes)
    monkeypatch.setattr(P, "run_query", promote_writes)
    assert S.main(["--apply", "--keep-follower-lots"]) == 0
    assert len(writes.calls) == 1
    assert promote_writes.calls == []
    assert all(c != S.FOLLOWER_LOTS_CYPHER for c, _ in reads.calls)


# ── stored rejections and the page cues ─────────────────────────────────────

CONT_FIRST = "SALE NOTICE\n1. Borrower\n...Continued to the next page..."
CONT_SECOND = "... Previous page Continuation...\n5. Borrower"


def test_a_rejected_pairing_is_dropped_from_the_plan():
    groups, _ = S.plan(_rows(), only=None, skip=set(),
                       rejected={frozenset(["q1.jpg", "q2.jpg"])})
    assert [g["pages"] for g in groups] == [["p1.jpg", "p2.jpg"]]


def test_a_rejected_pairing_stops_being_reported_as_ambiguous():
    rows = _rows() + [{"listing": "L3", "filename": "p1.jpg",
                       "content_key": "s1", "position": 0}]
    groups, ambiguous = S.plan(rows, only=None, skip=set(),
                               rejected={frozenset(["p1.jpg", "p2.jpg"])})
    assert [g["pages"] for g in groups] == [["q1.jpg", "q2.jpg"]]
    assert ambiguous == []


def test_the_pages_own_cues_correct_a_reversed_portal_order():
    texts = {"p1.jpg": CONT_SECOND, "p2.jpg": CONT_FIRST,
             "q1.jpg": "", "q2.jpg": ""}
    groups, _ = S.plan(_rows(), only=None, skip=set(), texts=texts)
    by_leader = {g["pages"][0]: g for g in groups}
    assert by_leader["p2.jpg"]["pages"] == ["p2.jpg", "p1.jpg"]
    assert by_leader["p2.jpg"]["order_corrected"] is True
    assert by_leader["q1.jpg"]["order_corrected"] is False


def test_cues_no_order_satisfies_move_the_group_to_ambiguous():
    both = CONT_FIRST + CONT_SECOND
    texts = {"p1.jpg": both, "p2.jpg": both, "q1.jpg": "", "q2.jpg": ""}
    groups, ambiguous = S.plan(_rows(), only=None, skip=set(), texts=texts)
    assert [g["pages"] for g in groups] == [["q1.jpg", "q2.jpg"]]
    assert ambiguous[0]["filenames"] == ["p1.jpg", "p2.jpg"]


def test_a_forced_group_is_cue_checked_like_any_other():
    rows = _rows() + [{"listing": "L3", "filename": "p1.jpg",
                       "content_key": "s1", "position": 0}]
    texts = {"p1.jpg": CONT_SECOND, "p2.jpg": CONT_FIRST}
    groups, _ = S.plan(rows, only={"p1.jpg"}, skip=set(), texts=texts)
    assert [g["pages"] for g in groups] == [["p2.jpg", "p1.jpg"]]


def test_the_build_carries_the_corrected_order_into_the_write():
    group = {"pages": ["p2.jpg", "p1.jpg"], "twins": [], "order_corrected": True}
    built = S.build(group, DOCS | {"p2.jpg": _doc("p2.jpg", "PAGE TWO", 10)})
    assert built["leader"] == "p2.jpg" and built["followers"] == ["p1.jpg"]
    assert built["order_corrected"] is True


def test_apply_clears_the_stitch_fields_of_a_demoted_leader():
    # correcting the order makes yesterday's leader a follower; its joined
    # text must not survive on it
    assert "REMOVE f.stitched_markdown" in S.APPLY_CYPHER
    assert "f.stitched_expected_lot_count" in S.APPLY_CYPHER


def test_reject_writes_the_other_members_on_every_document():
    assert "d.stitch_rejected_with = [x IN $members WHERE x <> fn]" in S.REJECT_CYPHER
    assert "d.stitch_rejected_at" in S.REJECT_CYPHER
    assert "REMOVE d.stitch_rejected_with, d.stitch_rejected_at" in S.UNREJECT_CYPHER


def test_fetch_rejections_reads_each_record_as_one_member_set(monkeypatch):
    monkeypatch.setattr(S, "run_read_query",
                        lambda *a, **k: [{"filename": "a.jpg", "others": ["b.pdf"]},
                                         {"filename": "b.pdf", "others": ["a.jpg"]}])
    assert S.fetch_rejections() == {frozenset(["a.jpg", "b.pdf"])}
