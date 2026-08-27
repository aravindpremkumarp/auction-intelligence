"""pipeline.block_order: column detection and reading-order scoring.

Network-free and image-only, like tests/pipeline/test_ink_coverage.py — pages
are drawn with Pillow so each test states exactly where the ink and the columns
are, and the block sequence is written out in the order the parser would emit
it.
"""
from __future__ import annotations

import io

import pytest

from pipeline.block_order import (ORDER_INVERSION_MIN_RATIO, reading_order,
                                  score_block_order)
from pipeline.ink_coverage import page_columns
from pipeline.ocr_health import PENALTY, score_ocr_health


W = H = 400
# TILE_PX is 8, so the page is a 50×50 tile grid and one tile is 0.02 of it.
# The full-width rules below are drawn 2px tall on exact tile boundaries so
# each inks one tile row and the gutter stays under GUTTER_MAX_OCCUPANCY.
RULE_PX = 2


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


def _rule(y: float) -> tuple[float, float, float, float]:
    """A full-width horizontal rule — the thing that crosses a gutter."""
    return (0.02, y, 0.98, y + RULE_PX / H)


def _block(x0, y0, x1, y1, page=1, text="x"):
    return {"page": page, "bbox": [x0, y0, x1, y1], "label": "Text", "text": text}


def _bands(n: int) -> list[tuple[float, float]]:
    """``n`` evenly spaced text bands down the page, with clear gaps."""
    step = 0.88 / n
    return [(0.06 + i * step, 0.06 + i * step + step * 0.7) for i in range(n)]


# Four y-bands of text, used by most of the ordering tests below.
BANDS = _bands(4)
LEFT_X, RIGHT_X = (0.05, 0.45), (0.55, 0.95)


def _two_column_page(bands: list[tuple[float, float]] | None = None) -> bytes:
    bands = bands or BANDS
    return _page([(LEFT_X[0], y0, LEFT_X[1], y1) for y0, y1 in bands]
                 + [(RIGHT_X[0], y0, RIGHT_X[1], y1) for y0, y1 in bands])


def _col_blocks(x: tuple[float, float],
                bands: list[tuple[float, float]] | None = None) -> list[dict]:
    return [_block(x[0] - 0.01, y0 - 0.01, x[1] + 0.01, y1 + 0.01)
            for y0, y1 in (bands or BANDS)]


# ── column detection ────────────────────────────────────────────────────────

def test_columns_are_read_off_the_ink():
    cols = page_columns(_two_column_page())["columns"]
    assert len(cols) == 2
    left, right = cols
    assert left[0] == pytest.approx(0.05, abs=0.03)
    assert left[1] == pytest.approx(0.45, abs=0.03)
    assert right[0] == pytest.approx(0.55, abs=0.03)
    assert right[1] == pytest.approx(0.95, abs=0.03)


def test_single_column_page_reports_one_column_not_zero():
    cols = page_columns(_page([(0.1, 0.1, 0.9, 0.9)]))["columns"]
    assert len(cols) == 1


def test_a_few_full_width_rules_do_not_fill_the_gutter():
    """Notices are ruled. A header and footer rule cross the gutter and must not
    erase it — that tolerance is what GUTTER_MAX_OCCUPANCY buys."""
    page = _page([(LEFT_X[0], y0, LEFT_X[1], y1) for y0, y1 in BANDS]
                 + [(RIGHT_X[0], y0, RIGHT_X[1], y1) for y0, y1 in BANDS]
                 + [_rule(0.08), _rule(0.92)])
    assert len(page_columns(page)["columns"]) == 2


def test_narrow_gaps_inside_a_grid_are_not_gutters():
    """Cell padding lines up down a table but is far too narrow to separate
    columns — splitting on it would turn every grid into five 'columns'."""
    cells = [(0.05 + c * 0.18, y0, 0.20 + c * 0.18, y1)
             for c in range(5) for y0, y1 in BANDS]     # 3px gaps between cells
    assert len(page_columns(_page(cells))["columns"]) == 1


def test_a_marginal_strip_is_folded_into_its_neighbour():
    # A vertical rule near the edge is not a column of its own.
    page = _page([(0.02, 0.05, 0.035, 0.95), (0.12, 0.1, 0.95, 0.9)])
    assert len(page_columns(page)["columns"]) == 1


def test_unreadable_or_blank_pages_report_why():
    assert page_columns(None)["skipped"] == "no-image"
    assert page_columns(b"not-an-image")["skipped"].startswith("unreadable-image")
    assert page_columns(_page([]))["skipped"] == "no-inked-columns"


# ── order scoring ───────────────────────────────────────────────────────────

def test_column_major_order_is_clean():
    blocks = _col_blocks(LEFT_X) + _col_blocks(RIGHT_X)
    r = score_block_order(_two_column_page(), blocks)
    assert r["inversion_ratio"] == 0.0
    assert r["flag"] is False
    assert r["details"]["comparable_pairs"] == 28   # 16 cross + 12 within


def test_interleaved_columns_are_caught_and_named():
    """The failure this exists for: the parser walked the page row by row, so
    every lot in the left column is followed by the next lot's details from the
    right. Coverage sees 100% of the ink read and every text check passes."""
    left, right = _col_blocks(LEFT_X), _col_blocks(RIGHT_X)
    interleaved = [b for pair in zip(left, right) for b in pair]
    r = score_block_order(_two_column_page(), interleaved)
    assert r["flag"] is True
    assert r["inversion_ratio"] >= ORDER_INVERSION_MIN_RATIO
    # It is the columns that are scrambled, not the vertical run within one.
    assert r["details"]["cross_column"]["inversions"] == 6
    assert r["details"]["within_column"]["inversions"] == 0
    assert r["details"]["first_inversion"]["kind"] == "cross-column"


def test_an_adjacent_swap_does_not_flag():
    """Two neighbours out of sequence contradict one pair out of the whole
    triangle. Flagging that would fire on most notices and bury the systematic
    failures."""
    blocks = _col_blocks(LEFT_X) + _col_blocks(RIGHT_X)
    blocks[1], blocks[2] = blocks[2], blocks[1]
    r = score_block_order(_two_column_page(), blocks)
    assert r["details"]["inversions"] == 1
    assert 0 < r["inversion_ratio"] < ORDER_INVERSION_MIN_RATIO
    assert r["flag"] is False


def test_one_far_misplaced_block_scales_with_the_page():
    """A block emitted far from its place contradicts every other block — ~2/N
    of the pairs. On a dense notice that is well under the threshold; on a short
    one it is not, and should not be."""
    dense = _bands(7)
    blocks = _col_blocks(LEFT_X, dense) + _col_blocks(RIGHT_X, dense)
    blocks.insert(0, blocks.pop())          # last right-hand block emitted first
    r = score_block_order(_two_column_page(dense), blocks)
    assert r["inversion_ratio"] == pytest.approx(2 / len(blocks), abs=0.01)
    assert r["flag"] is False

    short = _col_blocks(LEFT_X) + _col_blocks(RIGHT_X)      # eight blocks
    short.insert(0, short.pop())
    assert score_block_order(_two_column_page(), short)["flag"] is True


def test_a_scrambled_single_column_is_caught_without_any_gutter():
    page = _page([(0.1, y0, 0.9, y1) for y0, y1 in BANDS]
                 + [(0.1, 0.88, 0.9, 0.95)])
    blocks = [_block(0.08, y0 - 0.01, 0.92, y1 + 0.01)
              for y0, y1 in reversed(BANDS + [(0.88, 0.95)])]
    r = score_block_order(page, blocks)
    assert len(r["details"]["columns"]) == 1
    assert r["flag"] is True
    assert r["details"]["within_column"]["ratio"] == 1.0


def test_full_width_blocks_span_the_gutter_and_are_not_sequenced():
    """A page-wide header and footer have no column, so their position relative
    to the columns is not something the layout can adjudicate."""
    page = _page([_rule(0.04)] + [(LEFT_X[0], y0, LEFT_X[1], y1) for y0, y1 in BANDS]
                 + [(RIGHT_X[0], y0, RIGHT_X[1], y1) for y0, y1 in BANDS]
                 + [_rule(0.96)])
    header, footer = _block(0.02, 0.02, 0.98, 0.07), _block(0.02, 0.93, 0.98, 0.98)
    # Footer emitted first, header last — pure noise if spanning blocks counted.
    blocks = [footer] + _col_blocks(LEFT_X) + _col_blocks(RIGHT_X) + [header]
    r = score_block_order(page, blocks)
    assert r["details"]["spanning_blocks"] == 2
    assert r["inversion_ratio"] == 0.0
    assert r["flag"] is False


def test_side_by_side_blocks_in_one_column_have_no_order_to_violate():
    """A label and its value sit at the same height. Either emission order is
    correct, so neither may count as an inversion."""
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    pairs = []
    for y0, y1 in BANDS:
        pairs += [_block(0.55, y0, 0.88, y1), _block(0.12, y0, 0.45, y1)]
    r = score_block_order(page, pairs)
    assert r["details"]["within_column"]["inversions"] == 0
    assert r["flag"] is False


def test_unscorable_inputs_never_flag():
    page = _two_column_page()
    blocks = _col_blocks(LEFT_X) + _col_blocks(RIGHT_X)
    assert score_block_order(None, blocks)["inversion_ratio"] is None
    assert score_block_order(page, [])["inversion_ratio"] is None
    assert score_block_order(page, None)["flag"] is False
    assert score_block_order(b"not-an-image", blocks)["flag"] is False
    # Blocks on another page can't judge page 1.
    other = [{**b, "page": 2} for b in blocks]
    assert score_block_order(page, other)["inversion_ratio"] is None
    # Empty text carries no reading position; two blocks are too few to judge.
    assert score_block_order(page, [{**b, "text": ""} for b in blocks])["flag"] is False
    assert score_block_order(page, blocks[:2])["inversion_ratio"] is None


def test_blank_page_is_unscorable_rather_than_perfect():
    r = score_block_order(_page([]), _col_blocks(LEFT_X))
    assert r["inversion_ratio"] is None
    assert r["details"]["skipped"] == "no-inked-columns"


# ── reading_order ───────────────────────────────────────────────────────────

def test_reading_order_resequences_column_major():
    left, right = _col_blocks(LEFT_X), _col_blocks(RIGHT_X)
    interleaved = [b for pair in zip(left, right) for b in pair]
    cols = page_columns(_two_column_page())["columns"]
    assert reading_order(interleaved, cols) == [0, 2, 4, 6, 1, 3, 5, 7]
    assert reading_order(interleaved, []) == []


# ── health integration ──────────────────────────────────────────────────────

def test_health_gains_block_order_flag_and_penalty():
    md = "Clean notice text that trips no other flag."
    assert score_ocr_health(md)["flags"] == []

    order = {"flag": True, "inversion_ratio": 0.24,
             "details": {"inversion_ratio": 0.24,
                         "cross_column": {"pairs": 16, "inversions": 6,
                                          "ratio": 0.375}}}
    scored = score_ocr_health(md, order=order)
    assert scored["flags"] == ["block-order"]
    assert scored["score"] == 100 - PENALTY["block-order"]
    assert scored["details"]["block_order"]["cross_column"]["inversions"] == 6


def test_health_unchanged_when_order_is_absent_or_clean():
    md = "Clean notice text that trips no other flag."
    assert score_ocr_health(md, order=None)["flags"] == []
    assert score_ocr_health(md, order={"flag": False,
                                       "inversion_ratio": 0.02})["flags"] == []


def test_a_notice_can_carry_both_image_derived_flags():
    md = "Clean notice text that trips no other flag."
    scored = score_ocr_health(
        md,
        region={"flag": True, "uncovered_ratio": 0.38, "details": {}},
        order={"flag": True, "inversion_ratio": 0.3, "details": {}})
    assert scored["flags"] == ["missing-region", "block-order"]
    assert scored["score"] == 100 - PENALTY["missing-region"] - PENALTY["block-order"]
