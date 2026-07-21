"""pipeline.datalab.parse_datalab_blocks flattens Datalab's json block-tree
into the same canonical shape as pipeline.mineru.parse_mineru_content_list.

Pure-function, DB-free / network-free. Covers: block-type mapping, pixel→0..1
bbox normalization by page dims, polygon fallback, group descent + caption
disambiguation (table vs image), table HTML passthrough, image map resolution,
and that assemble_markdown consumes the output unchanged.
"""
from __future__ import annotations

import pytest

from pipeline.datalab import parse_datalab_blocks
from pipeline.mineru import assemble_markdown


# A one-page Document mirroring a SARFAESI notice: heading, body text, a table
# (with caption) and a site photo (with caption). Page is 1000×1400 px.
DOC = {
    "block_type": "Document",
    "children": [
        {
            "block_type": "Page",
            "bbox": [0, 0, 1000, 1400],
            "children": [
                {"block_type": "SectionHeader", "html": "<h1>SALE NOTICE</h1>",
                 "bbox": [100, 140, 900, 210]},
                {"block_type": "Text",
                 "html": "<p>Public notice is hereby given.</p>",
                 "polygon": [[100, 300], [900, 300], [900, 360], [100, 360]]},
                {"block_type": "TableGroup", "children": [
                    {"block_type": "Table",
                     "html": "<table><tr><td>A</td></tr></table>",
                     "bbox": [100, 400, 900, 700]},
                    {"block_type": "Caption", "html": "<p>Schedule A</p>",
                     "bbox": [100, 710, 900, 740]},
                ]},
                {"block_type": "PictureGroup", "children": [
                    {"block_type": "Picture", "html": "",
                     "bbox": [100, 800, 400, 1100],
                     "images": {"pic_0.jpg": "QkFTRTY0"}},
                    {"block_type": "Caption", "html": "<p>Site photo</p>",
                     "bbox": [100, 1110, 400, 1140]},
                ]},
            ],
        },
    ],
}


def test_labels_in_reading_order():
    blocks = parse_datalab_blocks(DOC)
    assert [b["label"] for b in blocks] == [
        "Title", "Text", "Table", "TableCaption", "Image", "ImageCaption",
    ]
    # reading_order is strictly increasing across the flattened tree.
    orders = [b["reading_order"] for b in blocks]
    assert orders == sorted(orders)
    assert len(set(orders)) == len(orders)
    assert all(b["source"] == "datalab" for b in blocks)


def test_bbox_normalized_by_page_dimensions():
    hdr = parse_datalab_blocks(DOC)[0]
    # [100,140,900,210] / [1000,1400] → [0.1, 0.1, 0.9, 0.15]
    assert hdr["bbox"] == pytest.approx([0.1, 0.1, 0.9, 0.15])


def test_polygon_used_when_bbox_absent():
    text = parse_datalab_blocks(DOC)[1]
    assert text["bbox"] == pytest.approx([0.1, 300 / 1400, 0.9, 360 / 1400])
    assert text["text"] == "Public notice is hereby given."


def test_table_html_passthrough():
    tbl = parse_datalab_blocks(DOC)[2]
    assert tbl["text"] == "<table><tr><td>A</td></tr></table>"
    assert tbl["table"] == {"format": "html", "rows": None, "cols": None,
                            "row_positions": None, "col_positions": None}


def test_caption_disambiguated_by_parent_group():
    blocks = parse_datalab_blocks(DOC)
    assert blocks[3]["label"] == "TableCaption"   # inside TableGroup
    assert blocks[3]["text"] == "Schedule A"
    assert blocks[5]["label"] == "ImageCaption"   # inside PictureGroup


def test_image_block_carries_img_path_and_resolves_url():
    img_map = {"pic_0.jpg": "https://cdn.example/datalab/pic_0.jpg"}
    pic = parse_datalab_blocks(DOC, img_map=img_map)[4]
    assert pic["label"] == "Image"
    assert pic["img_path"] == "pic_0.jpg"
    assert pic["img_url"] == "https://cdn.example/datalab/pic_0.jpg"


def test_img_url_none_without_map():
    pic = parse_datalab_blocks(DOC)[4]
    assert pic["img_path"] == "pic_0.jpg"
    assert pic["img_url"] is None


def test_assemble_markdown_consumes_output():
    md = assemble_markdown(parse_datalab_blocks(DOC))
    assert "# SALE NOTICE" in md                         # Title gets its marker
    assert "Public notice is hereby given." in md
    assert "<table><tr><td>A</td></tr></table>" in md    # HTML table passes through
    assert "Schedule A" in md


def test_canonical_keys_match_mineru_shape():
    from pipeline.mineru import parse_mineru_content_list
    mineru_keys = set(parse_mineru_content_list(
        [{"type": "text", "page_idx": 0, "bbox": [0, 0, 10, 10], "text": "x"}])[0])
    datalab_keys = set(parse_datalab_blocks(DOC)[0])
    assert datalab_keys == mineru_keys


def test_iter_pages_accepts_bare_list_and_single_page():
    page = DOC["children"][0]
    assert len(parse_datalab_blocks([page])) == 6      # bare list of pages
    assert len(parse_datalab_blocks(page)) == 6        # single Page node


def test_garbage_input_returns_empty():
    assert parse_datalab_blocks(None) == []
    assert parse_datalab_blocks({}) == []
    assert parse_datalab_blocks({"block_type": "Document", "children": []}) == []
