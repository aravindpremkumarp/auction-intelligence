"""Region detection (pipeline/region_detect.py) on synthetic notice images.

Pure Pillow — each test draws a minimal page (white ground, near-black
ruling lines, filled banner bars) and asserts the detected bands. The
real-image validation lives in the auto-region batch run; these pin the
geometric contracts: prose/grid split point, bar + border suppression,
and the no-confident-split fallbacks.
"""
from __future__ import annotations

import io

import pytest

pytest.importorskip("PIL")
from PIL import Image, ImageDraw  # noqa: E402

from pipeline.region_detect import detect_regions  # noqa: E402

W, H = 800, 2000


def _page() -> Image.Image:
    return Image.new("L", (W, H), 255)


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _rule(draw: ImageDraw.ImageDraw, y_frac: float, thickness: int = 3) -> None:
    y = int(y_frac * H)
    draw.rectangle([0, y, W - 1, y + thickness - 1], fill=10)


def test_prose_above_grid_splits_in_two():
    im = _page()
    d = ImageDraw.Draw(im)
    for i in range(11):                       # grid rules 0.30 .. 0.98
        _rule(d, 0.30 + i * 0.068)
    regs = detect_regions(_png(im))
    assert regs is not None and len(regs) == 2
    prose, grid = regs
    assert prose["bbox"][1] == 0.0
    assert prose["bbox"][3] == pytest.approx(0.31, abs=0.02)
    assert grid["bbox"][1] == pytest.approx(0.29, abs=0.02)


def test_footer_band_when_grid_ends_early():
    im = _page()
    d = ImageDraw.Draw(im)
    for i in range(11):                       # grid 0.10 .. 0.70
        _rule(d, 0.10 + i * 0.06)
    regs = detect_regions(_png(im))
    assert regs is not None
    assert len(regs) == 3                     # prose + grid + footer
    assert regs[-1]["bbox"][3] == 1.0


def test_no_rules_no_split():
    assert detect_regions(_png(_page())) is None


def test_full_page_grid_no_split():
    im = _page()
    d = ImageDraw.Draw(im)
    for i in range(17):                       # rules 0.02 .. 0.98: no band room
        _rule(d, 0.02 + i * 0.06)
    assert detect_regions(_png(im)) is None


def test_banner_bar_not_treated_as_grid():
    im = _page()
    d = ImageDraw.Draw(im)
    # Thick filled banner near the top — content, not a ruling line.
    d.rectangle([0, int(0.04 * H), W - 1, int(0.06 * H)], fill=10)
    for i in range(11):
        _rule(d, 0.40 + i * 0.05)
    regs = detect_regions(_png(im))
    assert regs is not None and len(regs) in (2, 3)
    # The grid band must start at the rules, not at the banner.
    assert regs[1]["bbox"][1] == pytest.approx(0.39, abs=0.02)


def test_outer_border_line_suppressed():
    im = _page()
    d = ImageDraw.Draw(im)
    _rule(d, 0.005)                           # page border, inside EDGE_MARGIN
    _rule(d, 0.995)
    for i in range(11):
        _rule(d, 0.35 + i * 0.05)
    regs = detect_regions(_png(im))
    assert regs is not None
    assert regs[0]["bbox"][3] == pytest.approx(0.36, abs=0.02)


def test_garbage_bytes_return_none():
    assert detect_regions(b"not an image") is None


def test_stray_header_rule_is_edge_trimmed():
    # A single header-box border above the grid, closer than the global
    # cluster ceiling but an outlier vs the grid's own row spacing — it must
    # be trimmed so the prose band survives (real case: TATA notice whose
    # header line at y=0.023 swallowed the cluster and forced no-split).
    im = _page()
    d = ImageDraw.Draw(im)
    _rule(d, 0.03)                            # stray header-box border
    for i in range(24):                       # dense grid 0.12 .. 0.81
        _rule(d, 0.12 + i * 0.03)             # row gap 0.03 ≪ stray gap 0.09
    regs = detect_regions(_png(im))
    assert regs is not None
    # Prose band exists and the grid starts at the grid, not the stray rule.
    assert regs[0]["bbox"][3] == pytest.approx(0.13, abs=0.02)
    assert regs[1]["bbox"][1] == pytest.approx(0.11, abs=0.02)
