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
