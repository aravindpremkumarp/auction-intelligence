from __future__ import annotations

from pipeline.reader.provenance import BlockMap, page_and_block

BLOCKS = [
    {"id": "b1", "page": 1, "reading_order": 0, "label": "Title", "text": "SALE NOTICE"},
    {"id": "b2", "page": 1, "reading_order": 1, "label": "Text", "text": "Lot 1 Reserve Rs.50,00,000"},
    {"id": "pn", "page": 1, "reading_order": 2, "label": "PageNumber", "text": "1"},
    {"id": "b3", "page": 2, "reading_order": 3, "label": "Text", "text": "Lot 2 Reserve Rs.75,00,000",
     "confidence": 0.61},
]
MD = "# SALE NOTICE\n\nLot 1 Reserve Rs.50,00,000\n\nLot 2 Reserve Rs.75,00,000"


def test_offsets_map_to_page_and_block():
    bm = BlockMap(MD, BLOCKS)
    assert bm.page_and_block(MD.index("Rs.50")) == (1, "b2")
    assert bm.page_and_block(MD.index("Rs.75")) == (2, "b3")
    assert bm.at(MD.index("Rs.75")).confidence == 0.61
    assert bm.page_and_block(None) == (None, None)
    assert page_and_block(MD, BLOCKS, 0)[0] == 1


def test_missing_block_text_is_skipped_not_fatal():
    blocks = BLOCKS + [{"id": "ghost", "page": 3, "reading_order": 4, "text": "never on the page"}]
    bm = BlockMap(MD, blocks)
    assert [p.block_id for p in bm.placed] == ["b1", "b2", "b3"]
    assert BlockMap(MD, None).page_and_block(3) == (None, None)
