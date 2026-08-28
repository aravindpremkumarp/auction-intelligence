"""Durable raw-MinerU capture on the review side.

Asserts the full-OCR reingest persist writes markdown_raw/blocks_raw, and the
crucial invariant that an edit (_save_doc) NEVER writes the raw fields, and that
the hot read query (_load_doc) never selects them. Imports api.review.blocks
under the conftest neo4j stub; run_query is monkeypatched to capture.
See docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.
"""
from __future__ import annotations

import inspect

import api.review.blocks as B


def test_persist_reingest_writes_raw(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"rev": 1}]

    monkeypatch.setattr(B, "run_query", _capture)
    B._persist_reingest_result(
        "n.jpg", markdown="MD", blocks_json="{}",
        markdown_raw="RAWMD", blocks_raw="[RAW]",
    )
    assert "d.markdown_raw" in captured["cypher"]
    assert "d.blocks_raw" in captured["cypher"]
    assert "d.markdown_raw_at" in captured["cypher"]
    assert captured["params"]["markdown_raw"] == "RAWMD"
    assert captured["params"]["blocks_raw"] == "[RAW]"


def test_save_doc_never_writes_raw(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"rev": 2}]

    monkeypatch.setattr(B, "run_query", _capture)
    # A non-empty block list: _save_doc refuses to persist an empty one (it
    # would blank the markdown too — see test_review_empty_blocks_guard.py).
    # This test is about which fields the cypher touches, so any block does.
    B._save_doc("n.jpg", {"schema_version": 1, "blocks": [
        {"id": "blk_a", "page": 1, "bbox": [0.1, 0.1, 0.5, 0.5],
         "label": "Text", "text": "hi", "reading_order": 1},
    ]}, 0)
    assert "markdown_raw" not in captured["cypher"]
    assert "blocks_raw" not in captured["cypher"]


def test_load_doc_query_excludes_raw_fields():
    src = inspect.getsource(B._load_doc)
    assert "markdown_raw" not in src
    assert "blocks_raw" not in src


def test_re_extract_block_never_writes_raw():
    src = inspect.getsource(B.re_extract_block)
    assert "markdown_raw" not in src
    assert "blocks_raw" not in src
