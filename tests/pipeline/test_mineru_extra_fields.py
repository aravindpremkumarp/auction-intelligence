"""parse_mineru_content_list now carries MinerU content-list fields that were
previously dropped (img_path, text_level, sub_type, table_caption,
table_footnote) and resolves each block's img_url from the archive image map.

Pure-function, DB-free. See
docs/superpowers/specs/2026-06-20-keep-full-mineru-output-design.md.
"""
from __future__ import annotations

from pipeline.mineru import parse_mineru_content_list


def test_captures_text_level_and_subtype():
    raw = [{"type": "title", "page_idx": 0, "bbox": [10, 10, 900, 50],
            "text": "SALE NOTICE", "text_level": 1, "sub_type": "heading"}]
    blk = parse_mineru_content_list(raw)[0]
    assert blk["text_level"] == 1
    assert blk["sub_type"] == "heading"


def test_text_level_ignores_bool_and_nonnumeric():
    raw = [{"type": "text", "page_idx": 0, "bbox": [0, 0, 100, 100],
            "text": "x", "text_level": True}]
    assert parse_mineru_content_list(raw)[0]["text_level"] is None


def test_table_caption_and_footnote_list_joined():
    raw = [{"type": "table", "page_idx": 0, "bbox": [0, 0, 1000, 500],
            "table_body": "<table></table>",
            "table_caption": ["Schedule A", "(immovable)"],
            "table_footnote": ["as on 2024"]}]
    blk = parse_mineru_content_list(raw)[0]
    assert blk["table_caption"] == "Schedule A\n(immovable)"
    assert blk["table_footnote"] == "as on 2024"


def test_img_path_captured_and_url_resolved_by_basename():
    raw = [{"type": "image", "page_idx": 0, "bbox": [100, 100, 400, 400],
            "img_path": "images/deadbeef.jpg"}]
    img_map = {"deadbeef.jpg": "https://cdn.example/mineru/images/x/deadbeef.jpg"}
    blk = parse_mineru_content_list(raw, img_map=img_map)[0]
    assert blk["img_path"] == "images/deadbeef.jpg"
    assert blk["img_url"] == "https://cdn.example/mineru/images/x/deadbeef.jpg"


def test_img_url_resolves_by_full_path_key_too():
    raw = [{"type": "image", "page_idx": 0, "bbox": [1, 1, 2, 2],
            "img_path": "images/deadbeef.jpg"}]
    img_map = {"images/deadbeef.jpg": "https://cdn/full.jpg"}
    assert parse_mineru_content_list(raw, img_map=img_map)[0]["img_url"] == \
        "https://cdn/full.jpg"


def test_img_url_none_without_map():
    raw = [{"type": "image", "page_idx": 0, "bbox": [100, 100, 400, 400],
            "img_path": "images/deadbeef.jpg"}]
    blk = parse_mineru_content_list(raw)[0]
    assert blk["img_path"] == "images/deadbeef.jpg"
    assert blk["img_url"] is None


def test_absent_fields_are_none():
    raw = [{"type": "text", "page_idx": 0, "bbox": [0, 0, 100, 100], "text": "hi"}]
    blk = parse_mineru_content_list(raw)[0]
    assert blk["img_path"] is None
    assert blk["img_url"] is None
    assert blk["text_level"] is None
    assert blk["sub_type"] is None
    assert blk["table_caption"] is None
    assert blk["table_footnote"] is None
