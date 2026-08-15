"""pipeline.ink_coverage: unread-ink detection on synthetic pages.

Network-free and image-only — pages are drawn with Pillow so each test states
exactly where the ink is and which blocks claim it.
"""
from __future__ import annotations

import io

import pytest

from pipeline.ink_coverage import MISSING_REGION_MIN_RATIO, score_ink_coverage
from pipeline.ocr_health import PENALTY, score_ocr_health


W = H = 400


def _page(ink_boxes: list[tuple[float, float, float, float]]) -> bytes:
    """A white page with solid black rectangles at the given 0..1 boxes."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in ink_boxes:
        d.rectangle([x0 * W, y0 * H, x1 * W, y1 * H], fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _block(x0, y0, x1, y1, page=1):
    return {"page": page, "bbox": [x0, y0, x1, y1], "label": "Text", "text": "x"}


def test_fully_covered_page_has_no_unread_ink():
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    r = score_ink_coverage(page, [_block(0.05, 0.05, 0.95, 0.95)])
    assert r["uncovered_ratio"] == 0.0
    assert r["flag"] is False


def test_missing_column_is_caught_and_located():
    """The regression that motivated tiles: a two-column page where only the
    left column was read. Row-wise coverage sees every row claimed by the left
    block and reports ~0% missing; 2D coverage sees the right column's ink."""
    page = _page([(0.05, 0.1, 0.45, 0.9),      # left column of text
                  (0.55, 0.1, 0.95, 0.9)])     # right column of text
    r = score_ink_coverage(page, [_block(0.02, 0.05, 0.48, 0.95)])
    assert r["flag"] is True
    assert r["uncovered_ratio"] == pytest.approx(0.5, abs=0.05)
    assert r["details"]["worst_column"]["where"] == "right"
    assert r["details"]["worst_column"]["ratio"] > 0.9


def test_missing_footer_band_is_located():
    page = _page([(0.1, 0.05, 0.9, 0.45), (0.1, 0.7, 0.9, 0.95)])
    r = score_ink_coverage(page, [_block(0.05, 0.02, 0.95, 0.5)])
    assert r["flag"] is True
    assert r["details"]["worst_band"]["where"] == "bottom"


def test_small_bbox_slop_stays_under_the_threshold():
    # Boxes that clip a few pixels off the text must not flag — that slop is
    # ordinary, and flagging it would bury the real cases.
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    r = score_ink_coverage(page, [_block(0.11, 0.11, 0.89, 0.89)])
    assert r["uncovered_ratio"] < MISSING_REGION_MIN_RATIO
    assert r["flag"] is False


def test_page_sized_empty_block_does_not_mask_a_failed_parse():
    """The corpus turned up five notices where Datalab returned one page-sized
    block with empty text. Counting area alone, they scored 0% unread — a
    perfect result on a page where nothing at all was read."""
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    empty_full_page = {"page": 1, "bbox": [0.0, 0.0, 1.0, 1.0],
                       "label": "Text", "text": ""}
    r = score_ink_coverage(page, [empty_full_page])
    assert r["flag"] is True
    assert r["uncovered_ratio"] == pytest.approx(1.0, abs=0.01)


def test_figures_count_as_read_but_a_page_sized_one_does_not():
    # A logo is ink we are not expected to transcribe, so a real figure block
    # counts as handled...
    page = _page([(0.1, 0.1, 0.4, 0.3)])
    fig = {"page": 1, "bbox": [0.05, 0.05, 0.45, 0.35], "label": "Image", "text": ""}
    assert score_ink_coverage(page, [fig])["flag"] is False
    # ...but "the whole page is a picture" is the parser giving up.
    whole = {"page": 1, "bbox": [0.0, 0.0, 1.0, 1.0], "label": "Image", "text": ""}
    assert score_ink_coverage(_page([(0.1, 0.1, 0.9, 0.9)]), [whole])["flag"] is True


def test_unscorable_inputs_never_flag():
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    # No blocks is a different failure (nothing parsed at all) and must not be
    # reported as 100% missing.
    assert score_ink_coverage(page, [])["uncovered_ratio"] is None
    assert score_ink_coverage(page, None)["flag"] is False
    assert score_ink_coverage(None, [_block(0, 0, 1, 1)])["uncovered_ratio"] is None
    # Blocks that all sit on another page can't judge page 1.
    assert score_ink_coverage(page, [_block(0, 0, 1, 1, page=2)])["uncovered_ratio"] is None
    # A blank page has too little ink to judge.
    assert score_ink_coverage(_page([]), [_block(0, 0, 1, 1)])["uncovered_ratio"] is None
    # A corrupt image is skipped, not raised.
    assert score_ink_coverage(b"not-an-image", [_block(0, 0, 1, 1)])["flag"] is False


def test_health_gains_missing_region_flag_and_penalty():
    md = "Clean notice text that trips no other flag."
    base = score_ocr_health(md)
    assert base["score"] == 100 and base["flags"] == []

    region = {"flag": True, "uncovered_ratio": 0.38,
              "details": {"uncovered_ratio": 0.38,
                          "worst_column": {"where": "right", "ratio": 1.0}}}
    scored = score_ocr_health(md, region=region)
    assert scored["flags"] == ["missing-region"]
    assert scored["score"] == 100 - PENALTY["missing-region"]
    assert scored["details"]["missing_region"]["worst_column"]["where"] == "right"


def test_health_unchanged_when_region_is_absent_or_clean():
    md = "Clean notice text that trips no other flag."
    assert score_ocr_health(md, region=None)["flags"] == []
    assert score_ocr_health(md, region={"flag": False,
                                        "uncovered_ratio": 0.01})["flags"] == []
