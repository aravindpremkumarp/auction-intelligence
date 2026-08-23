"""Datalab as the bulk-pipeline OCR engine: notice_type tier routing and the
run_and_cache path that writes loader-compatible markdown + blocks.

Network-free — datalab_api.run_file is monkeypatched. Proves the cached blocks
are read back by the real Neo4j loader without changes.
"""
from __future__ import annotations

import json

import pipeline.datalab_api as DLA
import pipeline.mineru as M
from pipeline.config import DATALAB_MODE_MULTI, DATALAB_MODE_SINGLE, datalab_mode_for


DOC = {
    "block_type": "Document",
    "children": [
        {"block_type": "Page", "bbox": [0, 0, 1000, 1400], "children": [
            {"block_type": "SectionHeader", "html": "<h1>SALE NOTICE</h1>",
             "bbox": [100, 140, 900, 210]},
            {"block_type": "Text", "html": "<p>Public notice is hereby given.</p>",
             "bbox": [100, 300, 900, 360]},
        ]},
    ],
}


def test_datalab_mode_routing():
    assert datalab_mode_for("multi") == DATALAB_MODE_MULTI
    assert datalab_mode_for("MULTI") == DATALAB_MODE_MULTI
    assert datalab_mode_for("single") == DATALAB_MODE_SINGLE
    assert datalab_mode_for("unknown") == DATALAB_MODE_SINGLE
    assert datalab_mode_for(None) == DATALAB_MODE_SINGLE


def test_run_and_cache_writes_loader_compatible_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "MINERU_MARKDOWN_DIR", tmp_path / "md")
    monkeypatch.setattr(M, "MINERU_BLOCKS_DIR", tmp_path / "bl")
    monkeypatch.setattr(DLA, "run_file", lambda *a, **k: {"json": DOC, "markdown": None})

    md_path, bl_path = DLA.run_and_cache("notices/x.jpg", tmp_path / "src.jpg", mode="fast")

    assert md_path.exists() and bl_path.exists()
    data = json.loads(bl_path.read_text(encoding="utf-8"))
    # Wrapped dict shape is what the loader treats as "already normalized".
    assert isinstance(data, dict) and isinstance(data["blocks"], list) and data["blocks"]
    assert data["engine"] == "datalab"
    assert all(b["source"] == "datalab" for b in data["blocks"])
    # Markdown was assembled from blocks (native markdown was None).
    assert "SALE NOTICE" in md_path.read_text(encoding="utf-8")


def test_parse_quality_coerces_and_rejects_junk():
    assert DLA.parse_quality({"parse_quality_score": 3}) == 3.0
    assert DLA.parse_quality({"parse_quality_score": "4.5"}) == 4.5
    # A cache-hit replay carries no score; so does a payload predating the field.
    assert DLA.parse_quality({"parse_quality_score": None}) is None
    assert DLA.parse_quality({}) is None
    assert DLA.parse_quality({"parse_quality_score": "n/a"}) is None
    # bool is an int subclass — must not read True as quality 1.0.
    assert DLA.parse_quality({"parse_quality_score": True}) is None


def test_run_and_cache_records_parse_quality(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "MINERU_MARKDOWN_DIR", tmp_path / "md")
    monkeypatch.setattr(M, "MINERU_BLOCKS_DIR", tmp_path / "bl")
    monkeypatch.setattr(DLA, "run_file",
                        lambda *a, **k: {"json": DOC, "parse_quality_score": 3.0})

    _md, bl_path = DLA.run_and_cache("notices/x.jpg", tmp_path / "src.jpg", mode="fast")

    data = json.loads(bl_path.read_text(encoding="utf-8"))
    assert data["parse_quality_score"] == 3.0

    import pipeline.load_markdowns_to_neo4j as L
    monkeypatch.setattr(L, "BLOCKS_DIR", tmp_path / "bl")
    assert L.read_parse_quality("notices/x.jpg") == 3.0


def test_read_parse_quality_none_when_absent(tmp_path, monkeypatch):
    """MinerU sidecars are a bare list, and older Datalab ones lack the field —
    both must read as None so the loader's coalesce leaves any stored score."""
    import pipeline.load_markdowns_to_neo4j as L
    bl = tmp_path / "bl"
    bl.mkdir()
    monkeypatch.setattr(L, "BLOCKS_DIR", bl)
    (bl / "notices_mineru.jpg.json").write_text('[{"type": "text"}]', encoding="utf-8")
    (bl / "notices_old.jpg.json").write_text('{"blocks": []}', encoding="utf-8")

    assert L.read_parse_quality("notices/mineru.jpg") is None
    assert L.read_parse_quality("notices/old.jpg") is None
    assert L.read_parse_quality("notices/missing.jpg") is None


def test_loader_reads_datalab_cache_unchanged(tmp_path, monkeypatch):
    monkeypatch.setattr(M, "MINERU_MARKDOWN_DIR", tmp_path / "md")
    monkeypatch.setattr(M, "MINERU_BLOCKS_DIR", tmp_path / "bl")
    monkeypatch.setattr(DLA, "run_file", lambda *a, **k: {"json": DOC})
    fp = "notices/x.jpg"
    DLA.run_and_cache(fp, tmp_path / "src.jpg", mode="fast")

    import pipeline.load_markdowns_to_neo4j as L
    monkeypatch.setattr(L, "BLOCKS_DIR", tmp_path / "bl")
    blocks = L.load_blocks_for(fp)

    assert blocks is not None and blocks
    assert all(b["id"] for b in blocks)          # loader stamped stable ids
    assert blocks[0]["source"] == "datalab"      # canonical shape preserved
    assert [b["label"] for b in blocks] == ["Title", "Text"]
