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


# ── stitched pages (pipeline/notice_pages) ──────────────────────────────────

class _Capture:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.cypher = None

    def __call__(self, cypher, params=None, **kw):
        self.cypher = cypher
        return self.rows


def test_fetch_reads_the_stitched_text_and_skips_followers(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(M, "run_read_query", cap)
    M._fetch(None, False, None)
    assert "d.stitched_into IS NULL" in cap.cypher
    assert "coalesce(d.stitched_markdown, d.markdown) AS md" in cap.cypher
    assert ("coalesce(d.stitched_expected_lot_count, d.expected_lot_count) "
            "AS expected_lot_count") in cap.cypher


def _donor_length_known(monkeypatch, *lengths):
    """Let the lookup past its length prefilter, so the equality query runs."""
    monkeypatch.setattr(M, "_DONOR_LENGTHS", set(lengths))


def test_find_donor_never_copies_a_stale_followers_extraction(monkeypatch):
    cap = _Capture()
    monkeypatch.setattr(M, "run_read_query", cap)
    _donor_length_known(monkeypatch, 3)
    M._find_donor("abc")
    assert "d.stitched_into IS NULL" in cap.cypher


def test_find_donor_matches_on_the_stitched_text(monkeypatch):
    """A stitched leader's `markdown` is still plain page 1, but its extraction
    indexes the joined text. Matching on `markdown` would hand that extraction
    to a plain page-1 twin whose offsets it does not fit."""
    cap = _Capture()
    monkeypatch.setattr(M, "run_read_query", cap)
    _donor_length_known(monkeypatch, 3)
    M._find_donor("abc")
    assert "size(coalesce(d.stitched_markdown, d.markdown)) = $len" in cap.cypher
    assert "coalesce(d.stitched_markdown, d.markdown) = $md" in cap.cypher
    assert "d.markdown = $md" not in cap.cypher
    assert "size(d.markdown)" not in cap.cypher


# ── the donor lookup never scans for a length nothing has ───────────────────

class _Reads:
    """Records every read the donor lookup sends."""

    def __init__(self, lengths, donor=None):
        self.lengths, self.donor = lengths, donor
        self.calls = []

    def __call__(self, cypher, params=None, **kw):
        self.calls.append((cypher, params))
        if cypher == M.DONOR_LENGTHS_CYPHER:
            return [{"len": n} for n in self.lengths]
        return [self.donor] if self.donor else []


def _reads(monkeypatch, lengths, donor=None):
    r = _Reads(lengths, donor)
    monkeypatch.setattr(M, "run_read_query", r)
    monkeypatch.setattr(M, "_DONOR_LENGTHS", None)
    return r


def test_a_length_no_extraction_has_costs_no_query(monkeypatch):
    """The scan this skips is the one that cost 40 minutes on a full run."""
    r = _reads(monkeypatch, lengths=[10, 999])
    assert M._find_donor("x" * 50) is None
    assert [c[0] for c in r.calls] == [M.DONOR_LENGTHS_CYPHER]


def test_the_lengths_are_read_once_for_the_whole_run(monkeypatch):
    r = _reads(monkeypatch, lengths=[10])
    for _ in range(5):
        M._find_donor("x" * 50)
    assert sum(c[0] == M.DONOR_LENGTHS_CYPHER for c in r.calls) == 1


def test_a_matching_length_still_compares_the_whole_text(monkeypatch):
    donor = {"filename": "twin.jpg", "j": "[]", "score": 90, "model": "m"}
    r = _reads(monkeypatch, lengths=[len(MD)], donor=donor)
    assert M._find_donor(MD) == donor
    equality = [c for c in r.calls if c[0] != M.DONOR_LENGTHS_CYPHER]
    assert len(equality) == 1
    assert equality[0][1] == {"md": MD, "len": len(MD)}
    assert "coalesce(d.stitched_markdown, d.markdown) = $md" in equality[0][0]


def test_the_length_query_carries_no_markdown(monkeypatch):
    assert "markdown AS" not in M.DONOR_LENGTHS_CYPHER
    assert "DISTINCT size(" in M.DONOR_LENGTHS_CYPHER


# ── an empty result is a failure, not a done document ───────────────────────

class _Res:
    def __init__(self, extractions): self.extractions = extractions


def _extract_returning(monkeypatch, extractions, writes):
    class _LX:
        @staticmethod
        def extract(md, **kw): return _Res(extractions)
    monkeypatch.setattr(M, "run_query", lambda *a, **k: writes.append(a) or [])
    monkeypatch.setattr(M, "validate_stored", lambda *a, **k: {"score": 0})
    monkeypatch.setattr(M, "_entities",
                        lambda res, source="": list(res.extractions))
    return _LX


def test_a_model_that_returns_nothing_leaves_the_page_untouched(monkeypatch):
    """The page must stay pending: skipping is keyed on extraction_json, so a
    stored empty result is a page nothing ever looks at again."""
    writes = []
    LX = _extract_returning(monkeypatch, [], writes)
    ok, _model, line = M._extract_one({"filename": "a.jpg", "md": "TEXT"},
                                      batch=1, route=False, LX=LX)
    assert ok is False
    assert writes == []
    assert "no entities" in line and line.startswith("[fail]")


def test_a_page_with_entities_is_still_written(monkeypatch):
    writes = []
    LX = _extract_returning(monkeypatch, ["one"], writes)
    ok, _model, line = M._extract_one({"filename": "a.jpg", "md": "TEXT"},
                                      batch=1, route=False, LX=LX)
    assert ok is True and len(writes) == 1
    assert "1 fields" in line


# ── the routed pass count reaches the model call ────────────────────────────

def test_a_single_lot_page_is_read_once_and_a_multi_lot_page_twice(monkeypatch):
    seen = {}

    class _Ent:
        extraction_class = "bank_name"
        extraction_text = "Indian Bank"
        attributes: dict = {}
        char_interval = None

    class _Res:
        extractions = [_Ent()]

    class _LX:
        @staticmethod
        def extract(md, **kw):
            # keyed on the text, not the model: both notice types route to the
            # same model today, so a model-keyed dict would collide
            seen[md] = kw.get("passes")
            return _Res()

    monkeypatch.setattr(M, "run_query", lambda *a, **k: [])
    monkeypatch.setattr(M, "validate_stored", lambda *a, **k: {"score": 90})
    for ntype in ("single", "multi"):
        ok, _model, _line = M._extract_one({"filename": f"{ntype}.jpg",
                                            "md": f"TEXT {ntype}",
                                            "notice_type": ntype},
                                           batch=1, route=True, LX=_LX)
        assert ok
    assert seen["TEXT single"] == 1
    assert seen["TEXT multi"] == 2


def test_an_unrouted_call_leaves_the_pass_count_to_the_env(monkeypatch):
    """The gemini-direct path does not route, so it must not be handed a count
    routing chose — LX.extract falls back to LANGEXTRACT_PASSES there."""
    seen = {}

    class _Res:
        extractions = ["one"]

    class _LX:
        @staticmethod
        def extract(md, **kw):
            seen["passes"] = kw.get("passes")
            return _Res()

    monkeypatch.setattr(M, "run_query", lambda *a, **k: [])
    monkeypatch.setattr(M, "validate_stored", lambda *a, **k: {"score": 90})
    monkeypatch.setattr(M, "_entities",
                        lambda res, source="": list(res.extractions))
    M._extract_one({"filename": "a.jpg", "md": "T", "notice_type": "multi"},
                   batch=1, route=False, LX=_LX)
    assert seen["passes"] is None
