"""
tests/api/test_markdown_stale_warning.py
----------------------------------------
The annotator's "the blocks are newer than the text" warning.

``scripts/backfill_blocks_datalab.py`` rewrites ``d.blocks`` from a fresh
Datalab parse and deliberately never writes ``d.markdown`` — langextract reads
``d.markdown`` and nothing else, and the review UI's highlight spans are
character offsets into that exact string. The cost is that the annotator then
shows a complete block layer beside an older, thinner engine's text, with
nothing on screen saying so: ``pipeline/ocr_health.py`` skips
``missing-region`` on precisely this cohort, so health still reads 100.

These tests pin the signal that drives the warning, and — the half that is
easy to forget — that it CLEARS. A warning that cannot go away once the
condition is fixed is worse than no warning: both write paths that rewrite
markdown and blocks together must drop ``d.blocks_source``.

Network- and DB-free: ``run_query`` / ``_load_doc`` are stubbed.
"""
from __future__ import annotations

import api.review.blocks as B
from api.review.router import BlocksDoc


# ── the signal ──────────────────────────────────────────────────────────────

def test_backfill_source_is_stale():
    assert B.markdown_is_stale("datalab-backfill") is True
    assert B.markdown_is_stale("datalab-backfill", "mineru-vlm") is True


def test_a_datalab_text_under_a_backfill_stamp_is_not_stale():
    """The stamp can outlive its condition. The backfill only ran on notices
    whose markdown was MinerU's, so Datalab-produced text under that stamp
    means a re-ingest rewrote both halves before re-ingests learned to clear
    it — 2 notices in the corpus. Without this the warning is permanent and
    wrong on every one of them."""
    assert B.markdown_is_stale("datalab-backfill", "datalab-fast") is False
    assert B.markdown_is_stale("datalab-backfill", "datalab-accurate") is False


def test_whitespace_around_the_source_still_counts():
    """The value is read straight off a Neo4j property; don't let padding
    silently turn the warning off."""
    assert B.markdown_is_stale("  datalab-backfill  ") is True


def test_every_other_provenance_is_not_stale():
    """These all mean markdown and blocks were written together."""
    for src in (None, "", "datalab", "mineru", "human", "datalab-patchfix"):
        assert B.markdown_is_stale(src) is False, src


def test_stale_sources_are_a_subset_of_what_code_writes():
    """Tripwire: the only writer of a stale source is the backfill script. If
    a value is added here it must be a real ``d.blocks_source`` value, or the
    warning is keyed on a string nothing ever sets."""
    assert B.MARKDOWN_STALE_BLOCK_SOURCES == ("datalab-backfill",)


# ── the payload the annotator reads ─────────────────────────────────────────

def _stub_load(monkeypatch, blocks_source, markdown_model="mineru-vlm"):
    meta = {"filename": "n1.jpg", "markdown": "# short", "markdown_length": 620,
            "blocks_source": blocks_source, "markdown_model": markdown_model}
    monkeypatch.setattr(
        B, "_load_doc",
        lambda f: ({"schema_version": 1, "blocks": [{"id": "b1"}]}, 1, meta))


def test_payload_flags_a_backfilled_notice(monkeypatch):
    _stub_load(monkeypatch, "datalab-backfill")
    doc = B.get_blocks("n1.jpg")
    assert doc["markdown_stale"] is True
    assert doc["blocks_source"] == "datalab-backfill"


def test_payload_does_not_flag_a_normal_notice(monkeypatch):
    _stub_load(monkeypatch, None)
    doc = B.get_blocks("n1.jpg")
    assert doc["markdown_stale"] is False


def test_payload_does_not_flag_a_reingested_notice(monkeypatch):
    """get_blocks must pass the model through, or the guard never runs."""
    _stub_load(monkeypatch, "datalab-backfill", markdown_model="datalab-fast")
    doc = B.get_blocks("n1.jpg")
    assert doc["markdown_stale"] is False
    assert doc["blocks_source"] == "datalab-backfill"


def test_load_doc_actually_selects_the_property():
    """The verdict is derived from ``blocks_source``; if the read query stops
    returning it every notice silently reads as not-stale."""
    src = B._load_doc.__doc__ and True  # keep linters quiet about the import
    assert src
    import inspect
    cypher = inspect.getsource(B._load_doc)
    assert "d.blocks_source" in cypher


def test_response_model_carries_both_fields():
    """A field missing from BlocksDoc is dropped on serialization, so the UI
    would never see it however well the read path works."""
    doc = BlocksDoc(blocks_source="datalab-backfill", markdown_stale=True)
    dumped = doc.model_dump()
    assert dumped["blocks_source"] == "datalab-backfill"
    assert dumped["markdown_stale"] is True


def test_response_model_defaults_to_not_stale():
    assert BlocksDoc().markdown_stale is False


# ── the warning clears ──────────────────────────────────────────────────────

def _capture_run_query(monkeypatch):
    captured: dict = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return [{"rev": 2}]

    monkeypatch.setattr(B, "run_query", _capture)
    return captured


def test_block_edit_clears_the_stale_source(monkeypatch):
    """``_save_doc`` rebuilds the markdown from the blocks, so afterwards the
    two agree and the warning must not survive."""
    captured = _capture_run_query(monkeypatch)
    monkeypatch.setattr(B, "assemble_markdown", lambda blocks: "rebuilt")
    B._save_doc("n1.jpg", {"blocks": [{"id": "b1", "text": "x"}]}, 1)
    assert "d.blocks_source        = NULL" in captured["cypher"]


def test_reingest_clears_the_stale_source(monkeypatch):
    """A re-ingest writes markdown and blocks from one engine in one query."""
    captured = _capture_run_query(monkeypatch)
    B._persist_reingest_result(
        "n1.jpg", markdown="full text", blocks_json="{}", markdown_raw=None,
        blocks_raw=None, mineru_zip_url=None)
    assert "d.blocks_source       = NULL" in captured["cypher"]


def test_a_cleared_source_reads_as_not_stale():
    """End of the loop: what those writes leave behind must clear the flag."""
    assert B.markdown_is_stale(None) is False
