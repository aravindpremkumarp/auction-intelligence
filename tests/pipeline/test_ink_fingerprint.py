"""pipeline.ink_fingerprint: recognising one page under two names.

Network-free and image-only — pages are drawn with Pillow, so each test states
exactly what changed between the two files it compares.
"""
from __future__ import annotations

import io

import pytest

from pipeline.ink_fingerprint import (
    GRID,
    SAME_PAGE_MAX_DISTANCE,
    content_hash,
    ink_signature,
    is_same_page,
    signature_distance,
)


W = H = 480
# Word-like marks: short enough not to read as a ruling line, and drawn as
# separate strokes rather than filled blocks so they stay text at every
# resolution these tests render. The module strips rules and solid graphics
# before it measures anything, and a filled block *is* a graphic — the more so
# once a test doubles the page, which doubles the block along with it.
WORD_W, WORD_H, GAP_X, GAP_Y = 16, 7, 6, 5
STROKE_W, STROKE_GAP = 2, 5


def _page(ink_boxes, *, size=(W, H), fmt="PNG", quality=90) -> bytes:
    """A white page whose given 0..1 boxes are filled with lines of "words"."""
    from PIL import Image, ImageDraw
    w, h = size
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    for x0, y0, x1, y1 in ink_boxes:
        px0, py0, px1, py1 = x0 * w, y0 * h, x1 * w, y1 * h
        y = py0
        while y + WORD_H <= py1:
            x = px0
            while x + WORD_W <= px1:
                for s in range(0, WORD_W, STROKE_GAP):
                    d.rectangle([x + s, y, x + s + STROKE_W, y + WORD_H],
                                fill="black")
                x += WORD_W + GAP_X
            y += WORD_H + GAP_Y
    buf = io.BytesIO()
    img.save(buf, format=fmt, quality=quality)
    return buf.getvalue()


def _rescaled(page: bytes, factor: float) -> bytes:
    """``page`` re-rendered at another resolution, as a portal would serve it.

    Note this rescales the finished image rather than redrawing at a new word
    size: the same page at another resolution is one where the glyphs grow with
    the paper, not one whose text got relatively smaller.
    """
    from PIL import Image
    with Image.open(io.BytesIO(page)) as im:
        out = im.resize((int(im.width * factor), int(im.height * factor)),
                        Image.LANCZOS)
        buf = io.BytesIO()
        out.save(buf, format="PNG")
        return buf.getvalue()


TWO_COLUMN = [(0.06, 0.08, 0.46, 0.92), (0.54, 0.08, 0.94, 0.92)]
ONE_COLUMN = [(0.06, 0.08, 0.94, 0.46)]


def test_content_hash_is_the_bytes_not_the_name():
    page = _page(TWO_COLUMN)
    assert content_hash(page) == content_hash(bytes(page))
    assert content_hash(page) != content_hash(_page(ONE_COLUMN))


def test_content_hash_of_nothing_is_none():
    assert content_hash(None) is None
    assert content_hash(b"") is None


def test_signature_is_stable_across_a_re_encode():
    """The portal re-serving one page as a lossy JPEG must not change it."""
    a = ink_signature(_page(TWO_COLUMN))
    b = ink_signature(_page(TWO_COLUMN, fmt="JPEG", quality=45))
    assert signature_distance(a["signature"], b["signature"]) <= SAME_PAGE_MAX_DISTANCE
    assert is_same_page(a["signature"], b["signature"])


@pytest.mark.parametrize("factor", [1.5, 2.0, 3.0])
def test_signature_is_stable_across_a_resolution_change(factor):
    """Fixed cell count, floating cell size — the point of meshing this way."""
    page = _page(TWO_COLUMN)
    a = ink_signature(page)
    b = ink_signature(_rescaled(page, factor))
    assert is_same_page(a["signature"], b["signature"])


def test_a_different_layout_is_not_the_same_page():
    a = ink_signature(_page(TWO_COLUMN))
    b = ink_signature(_page(ONE_COLUMN))
    assert signature_distance(a["signature"], b["signature"]) > SAME_PAGE_MAX_DISTANCE
    assert not is_same_page(a["signature"], b["signature"])


def test_signature_shape():
    sig = ink_signature(_page(TWO_COLUMN))
    assert len(sig["signature"]) == GRID * GRID // 4
    assert sig["skipped"] is None
    assert sig["aspect"] == pytest.approx(1.0)
    int(sig["signature"], 16)                    # it is hex


@pytest.mark.parametrize("page, why", [
    (None, "no-image"),
    (b"not an image at all", "unreadable-image"),
    (_page([]), "too-little-ink"),
])
def test_unscorable_pages_get_no_signature(page, why):
    """A blank or broken page must not become everyone's duplicate."""
    sig = ink_signature(page)
    assert sig["signature"] is None
    assert sig["skipped"].startswith(why)


def test_two_blank_pages_are_not_each_others_duplicate():
    a = ink_signature(_page([]))
    b = ink_signature(_page([], size=(W, H + 40)))
    assert not is_same_page(a["signature"], b["signature"])


@pytest.mark.parametrize("a, b", [
    (None, "ff" * 128),
    ("ff" * 128, None),
    ("ff", "ff" * 128),                          # different widths
    ("zz" * 128, "ff" * 128),                    # not hex
])
def test_missing_comparison_is_none_not_far(a, b):
    assert signature_distance(a, b) is None
    assert is_same_page(a, b) is False


def test_distance_is_zero_against_itself_and_one_against_its_inverse():
    sig = ink_signature(_page(TWO_COLUMN))["signature"]
    assert signature_distance(sig, sig) == 0.0
    inverse = f"{int(sig, 16) ^ ((1 << (GRID * GRID)) - 1):0{len(sig)}x}"
    assert signature_distance(sig, inverse) == 1.0
