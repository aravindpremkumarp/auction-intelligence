"""Notice localisation (pipeline/notice_locate.py) on synthetic newspaper pages.

Pure Pillow — each test draws a page with several boxed "notices" (framed
rectangles filled with fake word-like ink) and feeds in OCR-style blocks
with text. The real-image validation lives in scripts/auto_crop_notices.py
--preview; these pin the contracts: the right notice is picked from its
property hints, the crop snaps to that notice's frame (or gutter) and not
its neighbours', and the no-anchor fallbacks return None.
"""
from __future__ import annotations

import io
import random

import pytest

pytest.importorskip("PIL")
pytest.importorskip("rapidfuzz")
from PIL import Image, ImageDraw  # noqa: E402

from pipeline import notice_locate as nl  # noqa: E402
from pipeline.notice_locate import (  # noqa: E402
    anchor_blocks, build_hints, locate_notice, score_text, snap_to_frame,
)

W, H = 1600, 2400


def _png(im: Image.Image) -> bytes:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def _fill_words(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                rng: random.Random, line_h: int = 18) -> None:
    """Fake body text: staggered dark word-rects on every line of the box."""
    x0, y0, x1, y1 = box
    y = y0
    while y + line_h <= y1:
        x = x0 + rng.randint(0, 20)
        while x < x1 - 20:
            wlen = rng.randint(25, 90)
            d.rectangle([x, y + 4, min(x1, x + wlen), y + line_h - 4], fill=20)
            x += wlen + rng.randint(6, 14)
        y += line_h


def _framed_notice(d: ImageDraw.ImageDraw, box: tuple[int, int, int, int],
                   rng: random.Random, *, frame: bool = True,
                   inner_pad: int = 14, thick: int = 3) -> None:
    x0, y0, x1, y1 = box
    if frame:
        d.rectangle([x0, y0, x1, y1], outline=15, width=thick)
    _fill_words(d, (x0 + inner_pad, y0 + inner_pad, x1 - inner_pad, y1 - inner_pad), rng)


def _norm(box: tuple[int, int, int, int]) -> list[float]:
    return [box[0] / W, box[1] / H, box[2] / W, box[3] / H]


def _blk(bid: str, box: tuple[int, int, int, int], text: str) -> dict:
    return {"id": bid, "page": 1, "bbox": _norm(box), "text": text, "label": "Text"}


# Three notices: A top-left (target), B top-right, C bottom full-width.
A = (60, 80, 760, 900)
B = (820, 80, 1540, 900)
C = (60, 980, 1540, 2300)

PROPS = [{
    "reserve_price": 3250000,
    "borrowers": ["Mr Dineshkumar M", "Mrs Kala D"],
    "bank": "Canara Bank",
    "auction_start": "2026-03-25T11:30:00",
    "website_description": (
        "All that piece and parcel of land measuring 1200 sq ft with building "
        "at Door No 5, Nehru Street, Ranipet, comprised in Survey No 112/2"),
    "city": "Ranipet",
}]


def _page_with_three() -> bytes:
    rng = random.Random(7)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    for box in (A, B, C):
        _framed_notice(d, box, rng)
    return _png(im)


def _blocks_for_three() -> list[dict]:
    # Blocks sit inside each frame; the target's evidence is split over
    # three blocks in notice A, while B shares the city and C has a
    # different price and date. (A same-bank neighbour would join the
    # cluster by design — see notice_locate.CLUSTER_GAP.)
    return [
        _blk("a1", (80, 100, 740, 160), "CANARA BANK  Sale Notice for sale of immovable properties"),
        _blk("a2", (80, 180, 740, 520),
             "All that piece and parcel of land measuring 1200 sq ft with building at "
             "Door No 5, Nehru Street, Ranipet, comprised in Survey No 112/2. "
             "Borrower: Mr Dineshkumar M"),
        _blk("a3", (80, 540, 740, 880), "Reserve Price Rs. 32,50,000/-  EMD Rs 3,25,000  Date 25.03.2026"),
        _blk("b1", (840, 100, 1520, 160), "UNION BANK OF INDIA  Sale notice - Vellore branch"),
        _blk("b2", (840, 180, 1520, 880), "Reserve Price Rs. 18,00,000/- borrower Mr Raghavan S"),
        _blk("c1", (80, 1000, 1520, 2280), "Union Bank of India e-auction 1,20,00,000 date 30.03.2026"),
    ]


def test_build_hints_covers_every_kind():
    kinds = {h["kind"] for h in build_hints(PROPS)}
    assert kinds == {"price", "borrower", "bank", "date", "description", "place"}
    pats = {h["pattern"] for h in build_hints(PROPS) if h["kind"] == "date"}
    assert {"25.03.2026", "25/03/2026", "25-03-2026", "25 mar 2026"} <= pats


def test_score_text_counts_each_kind_once():
    hints = build_hints(PROPS)
    s, kinds = score_text("Rs 32,50,000 or 3250000 - Canara Bank - 25.03.2026", hints)
    assert kinds == ["bank", "date", "price"]
    assert s == pytest.approx(nl.W_PRICE + nl.W_BANK + nl.W_DATE)
    assert score_text("nothing relevant here", hints) == (0.0, [])


def test_anchor_cluster_stays_inside_target_notice():
    hints = build_hints(PROPS)
    anchor = anchor_blocks(_blocks_for_three(), hints)
    assert anchor is not None
    assert set(anchor["blocks"]) == {"a1", "a2", "a3"}   # b2 shares only the city
    assert anchor["bbox"][2] <= A[2] / W + 1e-6


def test_locate_snaps_to_target_frame_not_neighbours():
    res = locate_notice(_page_with_three(), _blocks_for_three(), PROPS)
    assert res is not None and res["snapped"]
    x0, y0, x1, y1 = res["bbox"]
    # Includes A's frame line (3px) plus padding, but nowhere near B or C.
    assert x0 == pytest.approx(A[0] / W, abs=0.012)
    assert y0 == pytest.approx(A[1] / H, abs=0.012)
    assert x1 == pytest.approx(A[2] / W, abs=0.012)
    assert y1 == pytest.approx(A[3] / H, abs=0.012)
    assert x1 < B[0] / W
    assert y1 < C[1] / H
    assert "price" in res["matched"] and "description" in res["matched"]


def test_snap_crosses_internal_rules_and_padding():
    """A column rule inside the notice (a table) must not stop the walk."""
    rng = random.Random(3)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    _framed_notice(d, A, rng)
    # Internal table column rule spanning only the lower half of A.
    d.rectangle([400, 520, 402, A[3] - 14], fill=15)
    png = _png(im)
    # Seed = a single cell right of the internal rule, in the table half.
    seed = _norm((420, 560, 740, 860))
    out = snap_to_frame(png, seed)
    assert out is not None
    # The first x-pass stops at the internal rule (it spans the seed), the
    # y-pass then grows the box to A's full height, and the next x-pass
    # sees the rule at < RULE_FRAC of that height and crosses it to A's frame.
    assert out[0] == pytest.approx(A[0] / W, abs=0.012)
    assert out[1] == pytest.approx(A[1] / H, abs=0.012)


def test_snap_walks_through_filled_banner_to_frame():
    """A black 'SALE NOTICE' strip inside the box is content, not a rule."""
    rng = random.Random(5)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    _framed_notice(d, A, rng)
    d.rectangle([A[0] + 14, A[1] + 14, A[2] - 14, A[1] + 70], fill=10)  # banner
    out = snap_to_frame(_png(im), _norm((100, 300, 700, 800)))
    assert out is not None
    assert out[1] == pytest.approx(A[1] / H, abs=0.012)   # above the banner
    assert out[0] == pytest.approx(A[0] / W, abs=0.012)


def test_dense_text_rows_are_not_rules():
    """Lines of body text (dark, but broken into words) never stop the walk."""
    rng = random.Random(9)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    _framed_notice(d, A, rng)
    out = snap_to_frame(_png(im), _norm((300, 400, 500, 500)))   # tiny seed
    assert out is not None
    assert out[:2] == pytest.approx([A[0] / W, A[1] / H], abs=0.012)
    assert out[2:] == pytest.approx([A[2] / W, A[3] / H], abs=0.012)


def test_frame_inside_anchor_with_tight_gutter_above():
    """The real JM17727039268646 layout: the notice is the framed bottom of
    the page, the OCR block box includes the frame line itself, and the
    gutter to the two notices above is only ~0.6% of the page height — with
    their bottom frames sitting in it as faint partial lines. The walk must
    still stop at the notice's own top frame, not run up into the neighbours."""
    rng = random.Random(21)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    top_l = (60, 80, 760, 880)
    top_r = (820, 80, 1540, 900)
    target = (60, 914, 1540, 2300)                    # gutter 880/900 → 914
    for box in (top_l, top_r):
        _framed_notice(d, box, rng)
    _framed_notice(d, target, rng)
    # Internal full-width table border inside the target, near its top.
    d.rectangle([target[0] + 14, target[1] + 300, target[2] - 14, target[3] - 14],
                outline=15, width=2)
    anchor = _norm((target[0], target[1], target[2], target[3]))   # includes frame
    out = snap_to_frame(_png(im), anchor)
    assert out is not None
    assert out[1] == pytest.approx(target[1] / H, abs=0.006)
    assert out[1] > top_r[3] / H                       # never into the neighbours
    assert out[3] == pytest.approx(target[3] / H, abs=0.012)
    assert out[0] == pytest.approx(target[0] / W, abs=0.012)


def test_snap_stops_at_gutter_when_unframed():
    rng = random.Random(11)
    im = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(im)
    _framed_notice(d, A, rng, frame=False)
    _framed_notice(d, B, rng, frame=False)
    _framed_notice(d, C, rng, frame=False)
    out = snap_to_frame(_png(im), _norm((100, 200, 700, 800)))
    assert out is not None
    gutter_mid = (A[2] + B[0]) / 2 / W
    assert out[2] == pytest.approx(gutter_mid, abs=0.01)
    assert out[3] < C[1] / H
    assert out[0] < A[0] / W + 0.02 and out[1] < A[1] / H + 0.02


def test_no_hints_or_no_match_returns_none():
    assert locate_notice(_page_with_three(), _blocks_for_three(), []) is None
    assert locate_notice(_page_with_three(), _blocks_for_three(),
                         [{"reserve_price": 999, "bank": "Nowhere Bank"}]) is None


def test_collapsed_full_page_block_returns_none():
    blocks = [_blk("giant", (10, 10, W - 10, H - 10),
                   "Canara Bank 32,50,000 Dineshkumar 25.03.2026")]
    assert locate_notice(_page_with_three(), blocks, PROPS) is None


def test_weak_evidence_is_rejected():
    # Bank + city only: shared by every notice that bank placed on the page.
    blocks = [_blk("a1", (80, 100, 740, 160), "Canara Bank Ranipet branch")]
    assert locate_notice(_page_with_three(), blocks, PROPS) is None


def test_unreadable_image_falls_back_to_padded_anchor():
    res = locate_notice(b"not-an-image", _blocks_for_three(), PROPS)
    assert res is not None and not res["snapped"]
    assert res["bbox"][0] == pytest.approx(80 / W - nl.PAD, abs=1e-3)
