"""Cohort selection and the write split for the Datalab shadow pass.

The `legacy-mineru` cohort runs over notices that already carry a block layer,
~90 of which hold human re-extractions. The measurement must never cost those
edits, so `write_back` writes blocks only where there are none — that gate is
what these tests pin down.
"""
from __future__ import annotations

import scripts.backfill_blocks_datalab as bd


def _capture(monkeypatch):
    """Swap nq for a recorder; returns the list of (cypher, params) calls."""
    calls: list[tuple[str, dict]] = []

    def fake_nq(statement, parameters=None):
        calls.append((statement, parameters or {}))
        return []

    monkeypatch.setattr(bd, "nq", fake_nq)
    return calls


def _result(file_path, *, had_blocks, ok=True):
    return {"file_path": file_path, "filename": file_path, "notice_type": "single",
            "mode": "fast", "blocks": [{"id": "blk_1"}], "pq": 4.0, "ratio": 0.1,
            "gain": 1.05, "chars": 100, "stored_chars": 95,
            "had_blocks": had_blocks, "human_edited": had_blocks,
            "ok_to_write": ok}


# ── write_back: the block-preservation gate ─────────────────────────────────

def test_document_with_blocks_is_measured_but_keeps_its_blocks(monkeypatch):
    calls = _capture(monkeypatch)
    measured, blocks = bd.write_back([_result("a.jpg", had_blocks=True)])

    assert (measured, blocks) == (1, 0)
    assert len(calls) == 1, "a second statement would be the block write"
    cypher = calls[0][0]
    assert "d.shadow_char_gain" in cypher
    assert "d.blocks" not in cypher


def test_blockless_document_gets_its_block_layer(monkeypatch):
    calls = _capture(monkeypatch)
    measured, blocks = bd.write_back([_result("b.jpg", had_blocks=False)])

    assert (measured, blocks) == (1, 1)
    assert len(calls) == 2
    block_cypher = calls[1][0]
    assert "SET d.blocks" in block_cypher
    # Belt and braces: the write re-checks emptiness server-side, so a block
    # layer added between selection and write-back still survives.
    assert "WHERE d.blocks IS NULL OR d.blocks = ''" in block_cypher


def test_mixed_batch_measures_all_and_writes_only_the_blockless(monkeypatch):
    _capture(monkeypatch)
    measured, blocks = bd.write_back([
        _result("a.jpg", had_blocks=True),
        _result("b.jpg", had_blocks=False),
        _result("c.jpg", had_blocks=True),
    ])
    assert (measured, blocks) == (3, 1)


def test_failed_documents_are_not_written(monkeypatch):
    calls = _capture(monkeypatch)
    assert bd.write_back([_result("a.jpg", had_blocks=True, ok=False)]) == (0, 0)
    assert calls == []


# ── select_targets: cohort predicates ───────────────────────────────────────

def _cypher_for(monkeypatch, **kwargs):
    calls = _capture(monkeypatch)
    bd.select_targets("all", None, **kwargs)
    return calls[0]


def test_legacy_cohort_selects_unmeasured_pre_cutoff_mineru(monkeypatch):
    cypher, params = _cypher_for(monkeypatch, cohort="legacy-mineru")
    assert "d.markdown_source = 'mineru'" in cypher
    assert "date(d.markdown_loaded_at) < date($before)" in cypher
    assert "d.shadow_char_gain IS NULL" in cypher, "would re-measure and re-bill"
    assert params["before"] == bd.LEGACY_CUTOFF
    # It must NOT inherit the blockless predicate — every legacy notice has blocks.
    assert "d.blocks IS NULL" not in cypher


def test_blockless_cohort_is_unchanged_and_is_the_default(monkeypatch):
    default_cypher, _ = _cypher_for(monkeypatch, )
    explicit_cypher, _ = _cypher_for(monkeypatch, cohort="blockless")
    assert default_cypher == explicit_cypher
    assert "(d.blocks IS NULL OR d.blocks = '')" in default_cypher
    assert "markdown_source" not in default_cypher


def test_cutoff_is_overridable(monkeypatch):
    _, params = _cypher_for(monkeypatch, cohort="legacy-mineru", before="2026-01-01")
    assert params["before"] == "2026-01-01"


def test_both_cohorts_require_a_raster_source_and_stored_text(monkeypatch):
    for kwargs in ({"cohort": "blockless"}, {"cohort": "legacy-mineru"}):
        cypher, _ = _cypher_for(monkeypatch, **kwargs)
        assert "png|jpg|jpeg|webp" in cypher, "ink coverage needs an image"
        assert "d.markdown <> ''" in cypher, "char gain needs a baseline"
