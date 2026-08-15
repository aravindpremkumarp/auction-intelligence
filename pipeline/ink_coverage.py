"""
pipeline/ink_coverage.py
------------------------
Detect notice content the parser never read, by comparing where ink physically
sits on the page against where the parsed blocks landed.

Why this exists
~~~~~~~~~~~~~~~
``pipeline/ocr_health.py`` judges the markdown we already have. It cannot see
what is missing: a notice whose whole right-hand column never made it into the
text is well-formed, loop-free and leak-free, so it scores 100 with no flags.
``SBI17861055659662.png`` is exactly that — 29 blocks, health 100, and 38% of
the page's ink untouched by any block.

The measurement
~~~~~~~~~~~~~~~
1. Binarize the page and tile it (``TILE_PX``). Each tile's dark fraction is
   its **ink mass** — where text physically is.
2. Mark every tile that any parsed block's bbox overlaps — what we read.
3. ``uncovered_ratio`` = ink mass in inked-but-unmarked tiles / total ink mass.

**Tiles, not rows.** The obvious cheap version — scan row by row, the way
``pipeline/region_detect.py`` finds rules — is wrong for these notices. They are
multi-column, so a block in the left column marks those rows "covered" and a
missing right column becomes invisible: measured row-wise, the SBI notice above
reports 1.1% missing instead of 38%. Coverage has to be two-dimensional.

Scope: single-page raster notices (the overwhelming majority). Blocks carry a
1-indexed ``page``; only that page's blocks are considered, and a caller with a
multi-page PDF must render and pass each page itself.

The result feeds ``ocr_health.score_ocr_health(markdown, region=…)``, which adds
the ``missing-region`` flag and its penalty — so a flagged doc surfaces in the
same review queue and routes to the same crop + re-ingest path as every other
OCR failure mode.
"""
from __future__ import annotations

import io


# Binarization threshold on the 0-255 grayscale — shared with region_detect.
DARK_THRESHOLD = 160
# Tile edge in source pixels. 8px is about half an x-height at notice
# resolution: fine enough to localize a missing column, coarse enough that one
# stray descender outside a bbox doesn't register as unread content.
TILE_PX = 8
# A tile counts as inked at >=2% dark. Below that is scanner speckle and paper
# grain, which would otherwise pile up into a large fake "missing" mass across
# the page margins.
INK_TILE_MIN = 0.02
# Flag the document at or above this share of unread ink. Set well above the
# few-percent noise floor of ordinary bbox slop (tight boxes clipping ascenders,
# ruled borders no block claims) so only a genuinely dropped region trips it.
MISSING_REGION_MIN_RATIO = 0.15
# Below this much total ink the page is blank/near-blank (a scan of an empty
# page, a photo) and the ratio is meaningless — return unscorable instead.
MIN_TOTAL_INK = 50.0


def _tile_ink(image_bytes: bytes) -> tuple[list[float], int, int]:
    """Per-tile dark fraction in reading order, plus the tile grid dimensions.

    One BOX resample of the dark mask gives exact per-tile means, so this stays
    a single Pillow operation regardless of page size.
    """
    from PIL import Image
    with Image.open(io.BytesIO(image_bytes)) as im:
        g = im.convert("L")
        w, h = g.width, g.height
        tw, th = max(1, w // TILE_PX), max(1, h // TILE_PX)
        # dark → 255 so the BOX mean IS the dark fraction (×255).
        mask = g.point(lambda p: 255 if p < DARK_THRESHOLD else 0)
        small = mask.resize((tw, th), Image.BOX)
        return [v / 255.0 for v in small.getdata()], tw, th


def _covered_tiles(blocks: list[dict], tw: int, th: int, page: int) -> bytearray:
    """Flat tw×th mask of tiles overlapped by a block bbox on ``page``.

    Block bboxes are normalized 0..1 by both engines (see pipeline/datalab.py
    and pipeline/mineru.py), so they scale straight onto the tile grid.
    """
    covered = bytearray(tw * th)
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if int(b.get("page") or 1) != page:
            continue
        bbox = b.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) >= 4):
            continue
        try:
            x0, y0, x1, y1 = (float(bbox[0]), float(bbox[1]),
                              float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError):
            continue
        # +1 on the far edge: a bbox ending mid-tile still covers that tile.
        ty0, ty1 = max(0, int(y0 * th)), min(th - 1, int(y1 * th))
        tx0, tx1 = max(0, int(x0 * tw)), min(tw - 1, int(x1 * tw))
        for ty in range(ty0, ty1 + 1):
            row = ty * tw
            for tx in range(tx0, tx1 + 1):
                covered[row + tx] = 1
    return covered


def _worst_third(ink: list[float], covered: bytearray, tw: int, th: int,
                 *, vertical: bool) -> dict:
    """Unread share of the worst third of the page, by column or by band.

    Which third is worst is the actionable half of this signal: an even spread
    is bbox slop, one third at ~100% is a dropped column or a cut-off footer,
    and that is what the crop + re-ingest path needs to know.
    """
    labels = (("top", "middle", "bottom") if vertical
              else ("left", "centre", "right"))
    best_label, best_ratio = labels[0], 0.0
    span = th if vertical else tw
    for i, label in enumerate(labels):
        lo, hi = span * i // 3, span * (i + 1) // 3
        total = missed = 0.0
        for ty in range(th):
            if vertical and not (lo <= ty < hi):
                continue
            row = ty * tw
            for tx in range(tw):
                if not vertical and not (lo <= tx < hi):
                    continue
                v = ink[row + tx]
                if v < INK_TILE_MIN:
                    continue
                total += v
                if not covered[row + tx]:
                    missed += v
        ratio = (missed / total) if total else 0.0
        if ratio > best_ratio:
            best_label, best_ratio = label, ratio
    return {"where": best_label, "ratio": round(best_ratio, 3)}


def score_ink_coverage(image_bytes: bytes | None, blocks: list[dict] | None,
                       *, page: int = 1) -> dict:
    """Measure how much of the page's ink no parsed block covers.

    Returns ``{"uncovered_ratio": float|None, "flag": bool, "details": {…}}``.
    ``uncovered_ratio`` is ``None`` — unscorable, never flagged — when there is
    no image, no block for the page, or too little ink to judge. "No blocks" in
    particular must not read as "100% missing": a doc that never produced blocks
    is a different failure, already visible upstream.
    """
    out: dict = {"uncovered_ratio": None, "flag": False, "details": {}}
    if not image_bytes or not blocks:
        out["details"]["skipped"] = "no-image" if not image_bytes else "no-blocks"
        return out
    if not any(isinstance(b, dict) and int(b.get("page") or 1) == page
               for b in blocks):
        out["details"]["skipped"] = "no-blocks-on-page"
        return out

    try:
        ink, tw, th = _tile_ink(image_bytes)
    except Exception as e:                       # unreadable/corrupt image
        out["details"]["skipped"] = f"unreadable-image: {type(e).__name__}"
        return out

    covered = _covered_tiles(blocks, tw, th, page)
    total = missed = 0.0
    for i, v in enumerate(ink):
        if v < INK_TILE_MIN:
            continue
        total += v
        if not covered[i]:
            missed += v
    if total < MIN_TOTAL_INK:
        out["details"]["skipped"] = "too-little-ink"
        return out

    ratio = missed / total
    out["uncovered_ratio"] = round(ratio, 4)
    out["flag"] = ratio >= MISSING_REGION_MIN_RATIO
    out["details"] = {
        "uncovered_ratio": round(ratio, 4),
        "tile_grid": [tw, th],
        "worst_column": _worst_third(ink, covered, tw, th, vertical=False),
        "worst_band": _worst_third(ink, covered, tw, th, vertical=True),
    }
    return out
