"""Multi-region crop: validation (`_clean_crop_regions`) and the pure
region→document merge (`_merge_region_blocks`) in api/review/blocks.py.

DB-free — the remap math and validation rules are what need pinning:
region-local MinerU bboxes must land at the right full-image coordinates,
in the reviewer's region order, on the right page.
"""
from __future__ import annotations

import pytest

from api.review.blocks import (
    MAX_CROP_REGIONS,
    _clean_crop_regions,
    _merge_region_blocks,
)


# ── _clean_crop_regions ─────────────────────────────────────────────────────

def test_none_and_empty_clear():
    assert _clean_crop_regions(None) is None
    assert _clean_crop_regions([]) is None


def test_valid_regions_are_sorted_top_to_bottom():
    raw = [
        {"bbox": [0.0, 0.5, 1.0, 0.9], "page": 1},   # lower band first
        {"bbox": [0.0, 0.0, 1.0, 0.1], "page": 1},   # header second
    ]
    out = _clean_crop_regions(raw)
    assert [r["bbox"][1] for r in out] == [0.0, 0.5]
    assert all(r["page"] == 1 for r in out)


def test_cross_page_regions_rejected():
    raw = [
        {"bbox": [0.0, 0.0, 1.0, 0.4], "page": 1},
        {"bbox": [0.0, 0.5, 1.0, 0.9], "page": 2},
    ]
    with pytest.raises(ValueError, match="same page"):
        _clean_crop_regions(raw)


def test_region_count_cap():
    raw = [{"bbox": [0.0, i / 20, 1.0, i / 20 + 0.04], "page": 1}
           for i in range(MAX_CROP_REGIONS + 1)]
    with pytest.raises(ValueError, match="at most"):
        _clean_crop_regions(raw)


def test_tiny_region_rejected():
    with pytest.raises(ValueError):
        _clean_crop_regions([{"bbox": [0.0, 0.0, 0.01, 0.01], "page": 1}])


def test_non_dict_and_missing_bbox_rejected():
    with pytest.raises(ValueError):
        _clean_crop_regions(["not-a-dict"])
    with pytest.raises(ValueError):
        _clean_crop_regions([{"page": 1}])
    with pytest.raises(ValueError):
        _clean_crop_regions("nope")


# ── _merge_region_blocks ────────────────────────────────────────────────────

def _blk(bbox, label="Text", text="x"):
    return {"id": "", "page": 1, "bbox": list(bbox), "label": label,
            "text": text, "reading_order": 0, "source": "mineru"}


def test_remap_into_full_image_coords():
    # Region occupies the middle band y∈[0.4, 0.8] of the page.
    region = {"bbox": [0.0, 0.4, 1.0, 0.8], "page": 1}
    # A block filling the region's top half.
    merged = _merge_region_blocks(
        [(region, [_blk([0.0, 0.0, 1.0, 0.5])])], page=1)
    x0, y0, x1, y1 = merged[0]["bbox"]
    assert (x0, y0, x1) == (0.0, 0.4, 1.0)
    assert y1 == pytest.approx(0.6)


def test_remap_clamps_to_region():
    region = {"bbox": [0.1, 0.1, 0.5, 0.5], "page": 1}
    # Degenerate MinerU bbox nudging past 1.0 must clamp to the region edge.
    merged = _merge_region_blocks(
        [(region, [_blk([0.0, 0.0, 1.2, 1.2])])], page=1)
    assert merged[0]["bbox"] == [0.1, 0.1, 0.5, 0.5]


def test_reading_order_is_region_major():
    header = {"bbox": [0.0, 0.0, 1.0, 0.2], "page": 1}
    table = {"bbox": [0.0, 0.2, 1.0, 0.9], "page": 1}
    merged = _merge_region_blocks(
        [(header, [_blk([0, 0, 1, 0.5], text="title"),
                   _blk([0, 0.5, 1, 1], text="intro")]),
         (table, [_blk([0, 0, 1, 1], label="Table", text="<table/>")])],
        page=1)
    assert [b["reading_order"] for b in merged] == [0, 1, 1000]
    assert [b["text"] for b in merged] == ["title", "intro", "<table/>"]


def test_page_stamped_on_every_block():
    region = {"bbox": [0.0, 0.0, 1.0, 1.0], "page": 3}
    merged = _merge_region_blocks(
        [(region, [_blk([0, 0, 1, 1]), _blk([0, 0, 0.5, 0.5])])], page=3)
    assert all(b["page"] == 3 for b in merged)


def test_empty_regions_merge_to_empty():
    assert _merge_region_blocks([], page=1) == []
    region = {"bbox": [0.0, 0.0, 1.0, 0.5], "page": 1}
    assert _merge_region_blocks([(region, [])], page=1) == []
