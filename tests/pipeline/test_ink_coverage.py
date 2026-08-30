"""pipeline.ink_coverage: unread-ink detection on synthetic pages.

Network-free and image-only — pages are drawn with Pillow so each test states
exactly where the ink is and which blocks claim it.
"""
from __future__ import annotations

import io

import pytest

from pipeline.ink_coverage import (
    MISSING_REGION_MIN_RATIO,
    PDF_MAX_EDGE_PX,
    PDF_RENDER_SCALE,
    TILE_PX,
    _is_pdf,
    _render_pdf_page,
    coverage_map,
    score_ink_coverage,
)
from pipeline.ocr_health import PENALTY, score_ocr_health


W = H = 400


# Word-like marks: wide enough not to read as a rule, short enough not to read
# as one, and thin enough vertically not to read as a solid graphic. The module
# distinguishes text ink from rules and from graphics, so a test page has to
# lay down ink shaped like text — a solid rectangle is, correctly, a picture.
WORD_W, WORD_H, GAP_X, GAP_Y = 16, 7, 6, 5


def _page(ink_boxes: list[tuple[float, float, float, float]]) -> bytes:
    """A white page whose given 0..1 boxes are filled with lines of "words"."""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in ink_boxes:
        px0, py0, px1, py1 = x0 * W, y0 * H, x1 * W, y1 * H
        y = py0
        while y + WORD_H <= py1:
            x = px0
            while x + WORD_W <= px1:
                d.rectangle([x, y, x + WORD_W, y + WORD_H], fill="black")
                x += WORD_W + GAP_X
            y += WORD_H + GAP_Y
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid_page(ink_boxes: list[tuple[float, float, float, float]]) -> bytes:
    """A white page with SOLID black rectangles — graphics, not text."""
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


def test_scattered_fringe_does_not_flag_even_when_it_sums_high():
    """The distinction the patch measure exists for. Ten separate strips of ink
    that no block claims add up to a large share of the page, but none of them
    is a dropped region — this is the shape bbox fringe takes on a dense notice,
    and measuring the total instead of the largest patch flagged 60% of the
    corpus on it."""
    strips = [(0.05, 0.05 + i * 0.09, 0.95, 0.10 + i * 0.09) for i in range(10)]
    page = _page(strips)
    # One block per strip, each stopping short of the strip's right end, so
    # every strip leaves a separate unread tail.
    blocks = [_block(0.04, 0.04 + i * 0.09, 0.70, 0.11 + i * 0.09)
              for i in range(10)]
    r = score_ink_coverage(page, blocks)
    assert r["uncovered_ratio"] > r["patch_ratio"]
    assert r["patch_ratio"] < MISSING_REGION_MIN_RATIO
    assert r["flag"] is False


def test_patch_bbox_points_at_the_missing_band():
    # The crop + re-ingest path needs to know *where* to look, not just how much.
    page = _page([(0.1, 0.05, 0.9, 0.35), (0.1, 0.7, 0.9, 0.95)])
    r = score_ink_coverage(page, [_block(0.05, 0.02, 0.95, 0.4)])
    assert r["flag"] is True
    x0, y0, x1, y1 = r["details"]["patch_bbox"]
    assert y0 > 0.5 and y1 > 0.9      # the unread band is the bottom one
    assert x0 < 0.2 and x1 > 0.8      # spanning the page width


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


def _ruled_page(cells: list[tuple[float, float, float, float]],
                rules_h: list[float], rules_v: list[float], *,
                thickness: int = 2) -> bytes:
    """A page whose cells hold text and whose grid is drawn as thin rules."""
    from PIL import Image, ImageDraw
    img = Image.open(io.BytesIO(_page(cells))).convert("RGB")
    d = ImageDraw.Draw(img)
    for y in rules_h:
        d.rectangle([0, y * H, W - 1, y * H + thickness], fill="black")
    for x in rules_v:
        d.rectangle([x * W, 0, x * W + thickness, H - 1], fill="black")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_table_rules_are_not_unread_content():
    """The false positive this guards: block bboxes hug their text, so a fully
    bordered notice's grid always falls outside every box — and because every
    rule touches every other, the skeleton reads as ONE huge connected patch and
    flags a notice whose cells were all read."""
    page = _ruled_page(
        cells=[(0.08, 0.12, 0.45, 0.38), (0.55, 0.12, 0.92, 0.38),
               (0.08, 0.62, 0.45, 0.88), (0.55, 0.62, 0.92, 0.88)],
        rules_h=[0.05, 0.5, 0.95], rules_v=[0.02, 0.5, 0.97],
    )
    blocks = [_block(0.07, 0.11, 0.46, 0.39), _block(0.54, 0.11, 0.93, 0.39),
              _block(0.07, 0.61, 0.46, 0.89), _block(0.54, 0.61, 0.93, 0.89)]
    r = score_ink_coverage(page, blocks)
    assert r["flag"] is False
    assert r["uncovered_ratio"] < MISSING_REGION_MIN_RATIO


def test_a_missing_cell_still_flags_on_a_ruled_page():
    """Stripping rules must not blind the detector: the same grid with one
    column unread is still a real missing region."""
    page = _ruled_page(
        cells=[(0.08, 0.12, 0.45, 0.38), (0.55, 0.12, 0.92, 0.38),
               (0.08, 0.62, 0.45, 0.88), (0.55, 0.62, 0.92, 0.88)],
        rules_h=[0.05, 0.5, 0.95], rules_v=[0.02, 0.5, 0.97],
    )
    blocks = [_block(0.07, 0.11, 0.46, 0.39), _block(0.07, 0.61, 0.46, 0.89)]
    r = score_ink_coverage(page, blocks)
    assert r["flag"] is True
    assert r["details"]["worst_column"]["where"] == "right"


def test_solid_graphics_are_not_unread_text():
    """A logo, seal or reversed-out banner is ink, but not text we failed to
    read. It survives the rule strip (it is far too thick to be a rule) and is
    then dropped as a solid blob. Nothing is lost by that: a reversed banner's
    glyphs are white, so the only dark ink there was ever the bar behind them."""
    page = _solid_page([(0.0, 0.02, 1.0, 0.22)])     # solid full-width bar
    r = score_ink_coverage(page, [_block(0.0, 0.5, 1.0, 0.9)])
    assert r["flag"] is False


def test_newspaper_chrome_at_the_page_edge_is_ignored():
    """These scans are clippings: the epaper's date line and URL sit past the
    notice behind a wide white gutter, and no block will ever cover them."""
    page = _page([(0.05, 0.10, 0.95, 0.80),          # the notice itself
                  (0.05, 0.93, 0.95, 0.99)])         # the epaper footer strip
    r = score_ink_coverage(page, [_block(0.03, 0.08, 0.97, 0.82)])
    assert r["flag"] is False


def test_an_unread_footer_is_not_mistaken_for_chrome():
    """The guard on the rule above: a footer that belongs to the notice butts up
    against the text that was read, so it must still be measured."""
    page = _page([(0.05, 0.10, 0.95, 0.60),
                  (0.05, 0.62, 0.95, 0.95)])         # unread, no gutter above
    r = score_ink_coverage(page, [_block(0.03, 0.08, 0.97, 0.61)])
    assert r["flag"] is True
    assert r["details"]["worst_band"]["where"] == "bottom"


# ── coverage_map: the same measurement, plus the grids the UI paints ─────────

def test_coverage_map_carries_the_grids_the_verdict_was_read_off():
    """The annotator's Ink tab renders these tiles, so they must agree with the
    verdict exactly — a UI that disagreed with the flag would be worse than no
    UI at all."""
    page = _page([(0.05, 0.10, 0.95, 0.60),
                  (0.05, 0.62, 0.95, 0.95)])         # second band unread
    blocks = [_block(0.03, 0.08, 0.97, 0.61)]
    verdict = score_ink_coverage(page, blocks)
    m = coverage_map(page, blocks)

    # Same numbers, whichever entry point the caller used.
    for k in ("uncovered_ratio", "patch_ratio", "flag"):
        assert m[k] == verdict[k]
    assert len(m["ink"]) == len(m["covered"]) == m["tile_w"] * m["tile_h"]
    assert m["tile_px"] == TILE_PX

    inked = [i for i, v in enumerate(m["ink"]) if v >= m["ink_min"] * 255]
    unread = [i for i in inked if not m["covered"][i]]
    assert inked and unread, "a flagged page has both covered and unread ink"
    # The unread tiles are the bottom band the block does not reach.
    assert min(i // m["tile_w"] for i in unread) > m["tile_h"] * 0.5


def test_coverage_map_omits_the_grids_when_the_page_is_unscorable():
    """Unscorable is a real answer (the UI says why), not an error — and it
    must not ship half a map the caller would paint as "all missing"."""
    page = _page([(0.1, 0.1, 0.9, 0.9)])
    for m in (coverage_map(page, []),
              coverage_map(None, [_block(0, 0, 1, 1)]),
              coverage_map(page, [_block(0, 0, 1, 1, page=2)]),
              coverage_map(b"not-an-image", [_block(0, 0, 1, 1)])):
        assert m["flag"] is False
        assert m["uncovered_ratio"] is None
        assert "ink" not in m and "covered" not in m
        assert m["details"].get("skipped")


# ── PDF sources ─────────────────────────────────────────────────────────────
# A notice arrives as a scan or as a PDF, and the measure has to answer the
# same question either way. Before PDFs were rendered here, every one of them
# came back "unreadable-image" — the annotator's Ink tab said the source was
# unsupported and the corpus scorer left ink_uncovered_ratio null.

def _pdf(pages: list[bytes]) -> bytes:
    """A PDF whose pages are the given rasters, one image per page.

    Each page box is sized so ``PDF_RENDER_SCALE`` renders it back to exactly
    the raster's own pixel dimensions. That makes the PDF and raster readings
    directly comparable: any difference is the PDF path, not a resolution
    change (the tile constants carry absolute-pixel floors, so a page rendered
    at another scale is honestly a different measurement).
    """
    fitz = pytest.importorskip("fitz", reason="PDF coverage needs PyMuPDF")
    doc = fitz.open()
    try:
        for raster in pages:
            pg = doc.new_page(width=W / PDF_RENDER_SCALE,
                              height=H / PDF_RENDER_SCALE)
            pg.insert_image(pg.rect, stream=raster)
        return doc.tobytes()
    finally:
        doc.close()


def test_a_pdf_reads_the_same_as_the_raster_of_the_same_page():
    """The bug this covers: a PDF notice was never measured at all."""
    ink = [(0.05, 0.10, 0.95, 0.55), (0.05, 0.60, 0.95, 0.95)]
    raster = _page(ink)
    blocks = [_block(0.03, 0.08, 0.97, 0.57)]      # lower band unread

    from_raster = score_ink_coverage(raster, blocks)
    from_pdf = score_ink_coverage(_pdf([raster]), blocks)

    assert from_pdf["details"].get("skipped") is None
    assert from_pdf["flag"] is from_raster["flag"] is True
    assert from_pdf["uncovered_ratio"] == from_raster["uncovered_ratio"]
    assert from_pdf["patch_ratio"] == from_raster["patch_ratio"]
    assert from_pdf["details"]["worst_band"]["where"] == "bottom"


def test_a_pdf_page_ships_the_same_grids_a_raster_does():
    """The Ink tab paints these, so the map has to survive the PDF path."""
    m = coverage_map(_pdf([_page([(0.05, 0.10, 0.95, 0.95)])]),
                     [_block(0.03, 0.08, 0.97, 0.52)])

    assert m["tile_px"] == TILE_PX
    assert len(m["ink"]) == len(m["covered"]) == m["tile_w"] * m["tile_h"]
    unread = [i for i, v in enumerate(m["ink"])
              if v >= m["ink_min"] * 255 and not m["covered"][i]]
    assert unread
    assert min(i // m["tile_w"] for i in unread) > m["tile_h"] * 0.5


def test_the_page_argument_selects_which_pdf_page_is_rendered():
    """Page 2's blocks must be measured against page 2's ink, not page 1's."""
    doc = _pdf([_page([(0.05, 0.05, 0.95, 0.95)]),    # p1: ink everywhere
                _page([(0.05, 0.60, 0.95, 0.95)])])   # p2: ink only low down
    covers_p2 = [_block(0.03, 0.58, 0.97, 0.97, page=2)]

    assert score_ink_coverage(doc, covers_p2, page=2)["uncovered_ratio"] == 0.0
    # The same box against page 1 leaves that page's upper two thirds unread,
    # which is only true if page 1 is what actually got rendered.
    on_p1 = score_ink_coverage(
        doc, [_block(0.03, 0.58, 0.97, 0.97, page=1)], page=1)
    assert on_p1["flag"] is True
    assert on_p1["details"]["worst_band"]["where"] == "top"


def test_a_page_the_pdf_does_not_have_says_so():
    """A distinct reason, not a generic decode failure: the reviewer needs to
    know the page is absent rather than the file broken."""
    out = score_ink_coverage(_pdf([_page([(0.1, 0.1, 0.9, 0.9)])]),
                             [_block(0.1, 0.1, 0.9, 0.9, page=4)], page=4)

    assert out["flag"] is False
    assert out["uncovered_ratio"] is None
    assert out["details"]["skipped"] == "page-out-of-range"


def test_a_corrupt_pdf_is_reported_not_raised():
    pytest.importorskip("fitz", reason="PDF coverage needs PyMuPDF")
    out = score_ink_coverage(b"%PDF-1.7\nshredded", [_block(0, 0, 1, 1)])

    assert out["uncovered_ratio"] is None
    assert out["details"]["skipped"].startswith("unreadable-pdf")


def test_a_pdf_is_recognised_by_its_header_not_its_name():
    """Bytes come from R2, where the extension is a convention. A PDF stored
    under an image name still has to measure."""
    raster = _page([(0.05, 0.10, 0.95, 0.95)])
    blocks = [_block(0.03, 0.08, 0.97, 0.97)]

    assert _is_pdf(_pdf([raster]))
    assert not _is_pdf(raster)
    assert score_ink_coverage(_pdf([raster]), blocks)["uncovered_ratio"] == 0.0


def test_an_oversized_page_box_is_rendered_within_the_pixel_cap():
    """One page is held in memory as pixels, so a broadsheet-sized page box
    scales down rather than rendering to whatever 2x happens to be."""
    fitz = pytest.importorskip("fitz", reason="PDF coverage needs PyMuPDF")
    doc = fitz.open()
    try:
        doc.new_page(width=6000, height=4000)       # 2x would be 12000px wide
        huge = doc.tobytes()
    finally:
        doc.close()

    png = _render_pdf_page(huge, 1)
    from PIL import Image
    with Image.open(io.BytesIO(png)) as im:
        assert max(im.width, im.height) <= PDF_MAX_EDGE_PX
        # Still rendered at the cap, not shrunk past it.
        assert max(im.width, im.height) > PDF_MAX_EDGE_PX * 0.9
