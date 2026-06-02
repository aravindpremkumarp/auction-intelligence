"""Backfill script: fetch filter + write params for durable raw capture."""
from __future__ import annotations

import scripts.backfill_markdown_raw as BF


def test_fetch_pending_filters_null_when_not_force(monkeypatch):
    captured = {}

    def _cap(cypher, **kwargs):
        captured["cypher"] = cypher
        return []

    monkeypatch.setattr(BF, "run_read_query", _cap)
    BF.fetch_pending(None, force=False)
    assert "d.markdown_raw IS NULL" in captured["cypher"]


def test_fetch_pending_no_filter_when_force(monkeypatch):
    captured = {}

    def _cap(cypher, **kwargs):
        captured["cypher"] = cypher
        return []

    monkeypatch.setattr(BF, "run_read_query", _cap)
    BF.fetch_pending(None, force=True)
    assert "d.markdown_raw IS NULL" not in captured["cypher"]


def test_write_raw_passes_all_fields(monkeypatch):
    captured = {}

    def _capture(cypher, params=None):
        captured["cypher"] = cypher
        captured["params"] = params
        return []

    monkeypatch.setattr(BF, "run_query", _capture)
    BF.write_raw("notices/x.jpg", "MD", "[1]")
    assert "d.markdown_raw" in captured["cypher"]
    assert "d.markdown_raw_at" in captured["cypher"]
    assert captured["params"]["markdown_raw"] == "MD"
    assert captured["params"]["blocks_raw"] == "[1]"
