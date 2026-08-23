"""
tests/api/test_review_replace_blocks.py
---------------------------------------
Unit tests for the pure block-array normalizer behind the replace-all
endpoint (``_normalize_replacement_blocks`` in :mod:`api.review.blocks`).

Covers id preservation/assignment, de-dup, bbox clamping, label validation,
table stripping, and source/confidence fallbacks — the logic an undo/redo or
multi-delete restore depends on. DB-free: blocks.py is imported in isolation
with stubbed neo4j/mineru, same pattern as test_review_rotation.py.
"""
from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

_BLOCKS_PATH = Path(__file__).resolve().parents[2] / "api" / "review" / "blocks.py"
_spec = importlib.util.spec_from_file_location("_blocks_under_test_replace", _BLOCKS_PATH)
_mod = importlib.util.module_from_spec(_spec)

_STUB_KEYS = ("api.neo4j_client", "pipeline.mineru", "pipeline")
_saved = {k: sys.modules.get(k) for k in _STUB_KEYS}

if "api.neo4j_client" not in sys.modules:
    _stub_neo4j = types.ModuleType("api.neo4j_client")
    _stub_neo4j.run_query = lambda *a, **k: None
    _stub_neo4j.run_read_query = lambda *a, **k: None
    sys.modules["api.neo4j_client"] = _stub_neo4j
if "pipeline" not in sys.modules:
    sys.modules["pipeline"] = types.ModuleType("pipeline")
if "pipeline.mineru" not in sys.modules:
    _stub_mineru = types.ModuleType("pipeline.mineru")
    _stub_mineru.DEFAULT_LABEL = "Text"
    _stub_mineru.MINERU_LABEL_VALUES = ["Text", "Title", "Table"]
    _stub_mineru.assemble_markdown = lambda blocks: ""
    sys.modules["pipeline.mineru"] = _stub_mineru

try:
    _spec.loader.exec_module(_mod)
finally:
    for k, v in _saved.items():
        if v is None:
            sys.modules.pop(k, None)
        else:
            sys.modules[k] = v

_normalize = _mod._normalize_replacement_blocks


def _blk(**over):
    base = {"id": "blk_keepme", "page": 1, "bbox": [0.1, 0.1, 0.5, 0.5],
            "label": "Text", "text": "hi", "reading_order": 3,
            "source": "human", "confidence": 0.9}
    base.update(over)
    return base


def test_preserves_existing_id():
    out = _normalize([_blk(id="blk_abc")], "me@x.com")
    assert out[0]["id"] == "blk_abc"


def test_assigns_id_when_missing():
    out = _normalize([_blk(id=None)], "me@x.com")
    assert out[0]["id"].startswith("blk_")


def test_dedupes_duplicate_ids():
    out = _normalize([_blk(id="blk_dup"), _blk(id="blk_dup")], "me@x.com")
    assert out[0]["id"] == "blk_dup"
    assert out[1]["id"] != "blk_dup"
    assert out[1]["id"].startswith("blk_")


def test_clamps_out_of_range_bbox():
    out = _normalize([_blk(bbox=[-0.2, 0.5, 1.4, 0.9])], "me@x.com")
    assert out[0]["bbox"][0] == 0.0
    assert out[0]["bbox"][2] == 1.0


def test_invalid_label_raises():
    with pytest.raises(ValueError):
        _normalize([_blk(label="Bogus")], "me@x.com")


def test_table_cleared_for_non_table_label():
    out = _normalize([_blk(label="Text", table={"format": "html"})], "me@x.com")
    assert out[0]["table"] is None


def test_table_kept_for_table_label():
    out = _normalize([_blk(label="Table", table={"format": "html", "rows": 2})],
                     "me@x.com")
    assert out[0]["table"] is not None
    assert out[0]["table"]["rows"] == 2


def test_source_fallback_and_confidence_none():
    out = _normalize([_blk(source="weird", confidence="nan")], "me@x.com")
    assert out[0]["source"] == "human"
    assert out[0]["confidence"] is None


def test_edited_by_defaults_to_caller_when_missing():
    out = _normalize([{"bbox": [0.1, 0.1, 0.2, 0.2], "label": "Text"}], "me@x.com")
    assert out[0]["edited_by"] == "me@x.com"


def test_preserves_mineru_provenance_fields():
    # An undo/redo restore must keep the archived image URL + the previously
    # dropped content-list fields, not silently strip them.
    raw = _blk(img_path="images/aa.jpg", img_url="https://cdn/aa.jpg",
               text_level=1, sub_type="heading",
               table_caption="Schedule A", table_footnote="as on 2024")
    out = _normalize([raw], "me@x.com")
    assert out[0]["img_path"] == "images/aa.jpg"
    assert out[0]["img_url"] == "https://cdn/aa.jpg"
    assert out[0]["text_level"] == 1
    assert out[0]["sub_type"] == "heading"
    assert out[0]["table_caption"] == "Schedule A"
    assert out[0]["table_footnote"] == "as on 2024"


def test_mineru_fields_default_none_for_human_block():
    out = _normalize([{"bbox": [0.1, 0.1, 0.2, 0.2], "label": "Text"}], "me@x.com")
    assert out[0]["img_url"] is None
    assert out[0]["text_level"] is None
    assert out[0]["table_caption"] is None


def test_preserves_datalab_provenance():
    """Regression: the allowlist here must match Block.source in router.py.

    "datalab" was missing, so every Datalab-OCR'd block that round-tripped
    through replace-all (undo/redo, multi-delete restore) had its provenance
    silently rewritten to "human". The review UI renders source == 'human' as
    an "edited" pill, so machine OCR displayed as human-verified — the review
    signal inverted for 661 documents' worth of blocks.
    """
    out = _normalize([_blk(source="datalab")], "me@x.com")
    assert out[0]["source"] == "datalab"


def test_unknown_source_still_falls_back_to_human():
    """The allowlist must stay closed — widening it for datalab must not turn
    it into a passthrough for arbitrary strings."""
    out = _normalize([_blk(source="tesseract")], "me@x.com")
    assert out[0]["source"] == "human"
