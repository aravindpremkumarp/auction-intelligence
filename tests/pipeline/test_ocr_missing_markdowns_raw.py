"""ocr_missing_markdowns attaches durable raw fields to its write rows.

Regression guard for the gap (flagged in PR #159 review) where this script
called write_markdowns with only {file_path, markdown}, so markdown_raw/
blocks_raw stayed null for documents it freshly OCR'd. DB-free: the cache
reader read_raw_artifacts is monkeypatched.
See docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.
"""
from __future__ import annotations

import scripts.ocr_missing_markdowns as M


def test_build_write_rows_attaches_raw(monkeypatch):
    # markdown_raw is the markdown we're writing (which IS the raw full.md);
    # only blocks_raw comes from the on-disk content_list cache.
    monkeypatch.setattr(M, "read_raw_artifacts",
                        lambda fp: ("ignored-md", "[raw blocks]"))
    rows = M.build_write_rows({"notices/x.jpg": "MD"})
    assert len(rows) == 1
    r = rows[0]
    assert r["file_path"] == "notices/x.jpg"
    assert r["markdown"] == "MD"
    assert r["markdown_raw"] == "MD"
    assert r["blocks_raw"] == "[raw blocks]"


def test_build_write_rows_skips_empty_markdown(monkeypatch):
    monkeypatch.setattr(M, "read_raw_artifacts", lambda fp: ("X", None))
    rows = M.build_write_rows({"a.jpg": "", "b.jpg": "  ", "c.jpg": "ok"})
    assert [r["file_path"] for r in rows] == ["c.jpg"]


def test_build_write_rows_blocks_raw_none_ok(monkeypatch):
    monkeypatch.setattr(M, "read_raw_artifacts", lambda fp: ("MD", None))
    rows = M.build_write_rows({"a.jpg": "MD"})
    assert rows[0]["blocks_raw"] is None
