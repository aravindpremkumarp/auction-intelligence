"""Durable raw-MinerU capture: read_raw_artifacts + loader write path.

Reads raw full.md + content_list.json off the on-disk cache, and verifies
write_markdowns persists them into the new Document properties. DB-free:
run_query is monkeypatched to capture; cache dirs redirected to tmp_path.
See docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.
"""
from __future__ import annotations

import pipeline.load_markdowns_to_neo4j as L


def _redirect(tmp_path, monkeypatch):
    md_dir = tmp_path / "md"
    bl_dir = tmp_path / "blocks"
    md_dir.mkdir()
    bl_dir.mkdir()
    monkeypatch.setattr(L, "MD_DIR", md_dir)
    monkeypatch.setattr(L, "BLOCKS_DIR", bl_dir)
    return md_dir, bl_dir


def test_read_raw_artifacts_none_when_absent(tmp_path, monkeypatch):
    _redirect(tmp_path, monkeypatch)
    md_raw, bl_raw = L.read_raw_artifacts("notices/x.jpg")
    assert md_raw is None
    assert bl_raw is None


def test_read_raw_artifacts_reads_both_verbatim(tmp_path, monkeypatch):
    md_dir, bl_dir = _redirect(tmp_path, monkeypatch)
    fp = "notices/x.jpg"
    safe = L.safe_name(fp)
    (md_dir / f"{safe}.md").write_text("# RAW\n![](images/abc.jpg)", encoding="utf-8")
    (bl_dir / f"{safe}.json").write_text('[{"type":"image"}]', encoding="utf-8")
    md_raw, bl_raw = L.read_raw_artifacts(fp)
    assert md_raw == "# RAW\n![](images/abc.jpg)"
    assert bl_raw == '[{"type":"image"}]'


def test_read_raw_artifacts_blocks_none_when_only_md(tmp_path, monkeypatch):
    md_dir, _ = _redirect(tmp_path, monkeypatch)
    fp = "notices/y.jpg"
    (md_dir / f"{L.safe_name(fp)}.md").write_text("only md", encoding="utf-8")
    md_raw, bl_raw = L.read_raw_artifacts(fp)
    assert md_raw == "only md"
    assert bl_raw is None


def test_write_markdowns_sets_raw_fields(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(L, "run_query", _capture)
    row = {"file_path": "notices/x.jpg", "markdown": "MD",
           "markdown_raw": "MD", "blocks_raw": "[1]",
           "blocks_json": None, "model": None}
    L.write_markdowns([row], "mineru", "mineru-vlm")
    assert "d.markdown_raw" in captured["cypher"]
    assert "d.blocks_raw" in captured["cypher"]
    assert "d.markdown_raw_at" in captured["cypher"]
    assert captured["params"]["rows"][0]["markdown_raw"] == "MD"
    assert captured["params"]["rows"][0]["blocks_raw"] == "[1]"
