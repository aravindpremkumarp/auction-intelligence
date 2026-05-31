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
