"""Regression tests for the per-block re-extract OCR-prep step.

A thin one-line crop (the real failing case was the ~356x18 "RESERVE PRICE ..."
banner on a 377x562 low-res scan) used to reach MinerU at its native extreme
aspect ratio and come back with no text — the block stayed empty and the UI
reported success. :func:`pipeline.reextract._pad_and_upscale_for_ocr`
letterbox-pads the crop to a sane aspect ratio and upscales it so the layout
model can read it.
"""
from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from pipeline.reextract import (  # noqa: E402
    OCR_MAX_ASPECT,
    OCR_TARGET_SHORT_EDGE,
    _draw_table_guides_on_png,
    _pad_and_upscale_for_ocr,
)


def _png(w: int, h: int) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


def _dims(png: bytes) -> tuple[int, int]:
    im = Image.open(io.BytesIO(png))
    im.load()
    return im.size


def test_thin_wide_strip_is_padded_and_upscaled():
    # The real annotator crop that returned no text.
    out_w, out_h = _dims(_pad_and_upscale_for_ocr(_png(356, 18)))
    assert out_w / out_h <= OCR_MAX_ASPECT + 0.01      # no longer extreme
    assert min(out_w, out_h) >= OCR_TARGET_SHORT_EDGE - 1   # upscaled, not tiny


def test_thin_tall_strip_is_padded_and_upscaled():
    out_w, out_h = _dims(_pad_and_upscale_for_ocr(_png(18, 356)))
    assert out_h / out_w <= OCR_MAX_ASPECT + 0.01
    assert min(out_w, out_h) >= OCR_TARGET_SHORT_EDGE - 1


def test_balanced_small_crop_is_upscaled_without_padding():
    out_w, out_h = _dims(_pad_and_upscale_for_ocr(_png(300, 200)))
    # 3:2 aspect is well under the cap, so only upscaling happens.
    assert abs((out_w / out_h) - 1.5) < 0.05
    assert min(out_w, out_h) >= OCR_TARGET_SHORT_EDGE - 1


def test_large_balanced_crop_is_left_alone():
    # Already big and balanced -> returned unchanged (no needless upscale).
    out_w, out_h = _dims(_pad_and_upscale_for_ocr(_png(1200, 800)))
    assert (out_w, out_h) == (1200, 800)


def test_garbage_bytes_return_input_unchanged():
    junk = b"not a png"
    assert _pad_and_upscale_for_ocr(junk) == junk


# ── Image table guides ───────────────────────────────────────────────────────
# Before this, image-sourced Table re-extracts dropped the reviewer's row/col
# guides entirely (the guide path was PDF-only), so re-running on the same crop
# just re-OCR'd to the same blob. We now burn the guides onto the crop as black
# rules so MinerU's table model follows them.

def _has_dark_column(png: bytes, x_frac: float, tol: int = 2) -> bool:
    """True if a (near-)vertical dark line exists around x = x_frac * width."""
    im = Image.open(io.BytesIO(png)).convert("RGB")
    im.load()
    w, h = im.size
    x = int(round(x_frac * w))
    for xx in range(max(0, x - tol), min(w, x + tol + 1)):
        col_dark = sum(1 for yy in range(h) if sum(im.getpixel((xx, yy))) < 200)
        if col_dark >= h * 0.6:   # most of the column is dark -> a drawn rule
            return True
    return False


def test_no_guides_returns_input_unchanged():
    raw = _png(200, 120)
    assert _draw_table_guides_on_png(raw, None, None) is raw
    assert _draw_table_guides_on_png(raw, [], []) is raw


def test_garbage_bytes_with_guides_return_input_unchanged():
    junk = b"not a png"
    assert _draw_table_guides_on_png(junk, [0.5], None) == junk


def test_column_guide_is_drawn_on_crop():
    raw = _png(400, 200)            # all white -> no dark column anywhere yet
    assert not _has_dark_column(raw, 0.5)
    out = _draw_table_guides_on_png(raw, None, [0.5])
    assert out[:8] == b"\x89PNG\r\n\x1a\n"          # still a valid PNG
    assert _has_dark_column(out, 0.5)               # the column rule landed
    assert not _has_dark_column(out, 0.25)          # and only where asked


def test_outer_border_is_stroked():
    # Even a single row guide closes the table box (left/right borders present).
    out = _draw_table_guides_on_png(_png(300, 150), [0.5], None)
    assert _has_dark_column(out, 0.0)               # left border
    assert _has_dark_column(out, 1.0)               # right border
