"""pipeline/stitch_refresh: a joined notice follows its pages' current text."""
from __future__ import annotations

import pipeline.stitch_refresh as sr
from pipeline.notice_pages import SEPARATOR


def _group(p1="page one", p2="page two", c1=3, c2=2, held=None, held_count=None):
    pages = [{"filename": "a.jpg", "markdown": p1, "expected_lot_count": c1},
             {"filename": "b.jpg", "markdown": p2, "expected_lot_count": c2}]
    return {"leader": "a.jpg", "order": ["a.jpg", "b.jpg"], "pages": pages,
            "held_markdown": held if held is not None else p1 + SEPARATOR + p2,
            "held_count": held_count if held_count is not None else c1 + c2}


def test_unchanged_group_is_left_alone():
    assert sr.plan_refresh([_group()]) == []


def test_rewritten_page_rebuilds_the_joined_text():
    # the TATA case: joined while page 1 was nearly empty, page 1 re-OCR'd later
    g = _group(p1="rows 1-27", held="26" + SEPARATOR + "page two")
    [r] = sr.plan_refresh([g])
    assert r["markdown"] == "rows 1-27" + SEPARATOR + "page two"
    assert r["offsets"] == [0, len("rows 1-27") + len(SEPARATOR)]
    assert r["followers"] == ["b.jpg"]
    assert r["held_markdown"] == "26" + SEPARATOR + "page two"


def test_changed_page_count_rebuilds_the_summed_count():
    [r] = sr.plan_refresh([_group(c2=21, held_count=5)])
    assert r["expected_lot_count"] == 24


def test_missing_page_is_never_joined_without_it():
    g = _group(held="stale")
    g["pages"] = [g["pages"][0], None]
    assert sr.plan_refresh([g]) == []


def test_page_order_is_the_stored_one():
    g = _group(held="stale")
    g["pages"] = [g["pages"][1], g["pages"][0]]   # query returns stored order
    g["order"] = ["b.jpg", "a.jpg"]
    g["leader"] = "b.jpg"
    [r] = sr.plan_refresh([g])
    assert r["markdown"].startswith("page two")


def test_refresh_writes_only_changed_groups(monkeypatch):
    reads, writes = [], []

    def fake_read(cypher, params=None, **kw):
        reads.append(params)
        if "file_path" in cypher and "UNWIND" in cypher:
            return [{"filename": "b.jpg"}]
        return [_group(), {**_group(p1="new", held="old"), "leader": "c.jpg"}]

    def fake_write(cypher, params=None):
        writes.append(params)
        return [{"n": 1}]

    monkeypatch.setattr(sr, "run_read_query", fake_read)
    monkeypatch.setattr(sr, "run_query", fake_write)
    assert sr.refresh_stitches(file_paths=["notices/1/b.jpg"]) == ["c.jpg"]
    assert reads[-1] == {"all": False, "filenames": ["b.jpg"]}
    assert [w["leader"] for w in writes] == ["c.jpg"]


def test_no_arguments_checks_every_group(monkeypatch):
    seen = {}
    monkeypatch.setattr(sr, "run_read_query",
                        lambda c, p=None, **k: seen.update(p) or [])
    assert sr.refresh_stitches() == []
    assert seen["all"] is True


def test_write_skipped_when_leader_moved_underneath(monkeypatch):
    monkeypatch.setattr(sr, "run_read_query",
                        lambda c, p=None, **k: [_group(p1="new", held="old")])
    monkeypatch.setattr(sr, "run_query", lambda c, p=None: [])
    assert sr.refresh_stitches(filenames=["a.jpg"]) == []


def test_database_error_never_reaches_the_writer(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("neo4j down")
    monkeypatch.setattr(sr, "run_read_query", boom)
    assert sr.refresh_stitches(filenames=["a.jpg"]) == []
