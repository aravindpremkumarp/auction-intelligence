"""
tests/api/test_review_empty_blocks_guard.py
--------------------------------------------
An empty block list must never be persisted as a valid parse.

Two Documents were found in production carrying ``{"blocks": []}`` over intact
raw output, from two separate paths that both treated "no blocks" as a result
rather than a failure:

  * ``reingest_notice`` did ``load_blocks_for(...) or []`` — a missing or
    unparseable content-list became an empty layer, written over the real one.
    Markdown and ``blocks_raw`` are read from different files and survived, so
    the loss looked exactly like a successful re-ingest.
  * ``_save_doc`` rebuilds ``d.markdown`` from the block list, so a delete of
    the last block (or a replace-all with ``[]``) blanked the notice's text
    too — one such Document was left with 0 characters of markdown.

DB-free: blocks.py is imported in isolation with stubbed neo4j/mineru, same
pattern as test_review_replace_blocks.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BLOCKS_PATH = Path(__file__).resolve().parents[2] / "api" / "review" / "blocks.py"
_spec = importlib.util.spec_from_file_location("_blocks_under_test_empty", _BLOCKS_PATH)
_mod = importlib.util.module_from_spec(_spec)

_STUB_KEYS = ("api.neo4j_client", "pipeline.mineru", "pipeline")
_saved = {k: sys.modules.get(k) for k in _STUB_KEYS}

_writes: list[tuple] = []

# Installed unconditionally, not "if absent": this module exercises _save_doc
# itself, so a real api.neo4j_client left in sys.modules by an earlier test
# would send the write at a live database.
_stub_neo4j = types.ModuleType("api.neo4j_client")
_stub_neo4j.run_query = lambda *a, **k: (_writes.append((a, k)), [{"rev": 1}])[1]
_stub_neo4j.run_read_query = lambda *a, **k: None
sys.modules["api.neo4j_client"] = _stub_neo4j

sys.modules.setdefault("pipeline", types.ModuleType("pipeline"))

_stub_mineru = types.ModuleType("pipeline.mineru")
_stub_mineru.DEFAULT_LABEL = "Text"
_stub_mineru.MINERU_LABEL_VALUES = ["Text", "Title", "Table"]
# The real one returns "" for an empty list — the behaviour that blanked the
# markdown. Keep it faithful so the guard is tested against reality.
_stub_mineru.assemble_markdown = lambda blocks: "\n\n".join(
    b.get("text", "") for b in blocks)
sys.modules["pipeline.mineru"] = _stub_mineru

try:
    _spec.loader.exec_module(_mod)
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v


def _doc(blocks):
    return {"schema_version": 1, "blocks": blocks}


def _blk(text="hello"):
    return {"id": "blk_a", "page": 1, "bbox": [0.1, 0.1, 0.5, 0.5],
            "label": "Text", "text": text, "reading_order": 1,
            "source": "mineru", "confidence": 0.9}


# ── _save_doc: an edit may not leave a Document with zero blocks ────────────

def test_save_doc_refuses_an_empty_block_list():
    with pytest.raises(_mod.BlocksWouldEmptyDoc):
        _mod._save_doc("UBI17835234803580.png", _doc([]), 5)


def test_save_doc_refuses_a_missing_blocks_key():
    with pytest.raises(_mod.BlocksWouldEmptyDoc):
        _mod._save_doc("n.png", {"schema_version": 1}, 0)


def test_empty_save_reaches_the_db_not_at_all():
    """The point of the guard: no write is attempted, so blocks_revision does
    not advance and the stored layer is untouched."""
    _writes.clear()
    with pytest.raises(_mod.BlocksWouldEmptyDoc):
        _mod._save_doc("n.png", _doc([]), 3)
    assert _writes == []


def test_guard_maps_to_http_400():
    """The router turns ValueError into a 400; a bare RuntimeError would be a
    500 and read to the reviewer as a server fault rather than a refused edit."""
    assert issubclass(_mod.BlocksWouldEmptyDoc, ValueError)


def test_save_doc_still_accepts_a_normal_edit():
    _writes.clear()
    rev = _mod._save_doc("n.png", _doc([_blk()]), 4)
    assert rev == 1
    assert len(_writes) == 1
    params = _writes[0][0][1]
    assert params["expected_rev"] == 4
    assert params["markdown"] == "hello", "markdown is rebuilt from the blocks"


def test_deleting_down_to_one_block_is_still_allowed():
    """The guard blocks the last deletion only — it must not make the
    annotator read-only."""
    _writes.clear()
    assert _mod._save_doc("n.png", _doc([_blk("only one left")]), 1) == 1
    assert len(_writes) == 1
