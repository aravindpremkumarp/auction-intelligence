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

The same tile grid answers a second question the markdown cannot: *where the
page's columns are*. :func:`page_columns` reads them off the ink as the
full-height whitespace channels between text, which is what
``pipeline/block_order.py`` needs to know whether the parser emitted its blocks
in reading order. Coverage itself never uses it — it is layout, not coverage.
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
# Block boxes sit tight against their text, so ascenders, descenders and ruled
# borders fall a pixel or two outside. Growing each box by one tile before
# measuring absorbs that without letting it reach into a neighbouring region.
COVER_DILATE_TILES = 1
# Flag on the LARGEST CONTIGUOUS patch of unread ink, not the page total.
# Both readings were measured across the corpus: totals put 475 documents over
# 15%, but 60% of them fell back under once boxes were dilated by two tiles —
# they were never missing a region, they were shedding a thin fringe around
# every block, and summing that fringe across a dense notice clears any total
# threshold. A dropped column or an unread footer is one solid patch, so
# thresholding the largest patch keeps those and drops the fringe.
MISSING_REGION_MIN_RATIO = 0.12
# Below this much total ink the page is blank/near-blank (a scan of an empty
# page, a photo) and the ratio is meaningless — return unscorable instead.
MIN_TOTAL_INK = 50.0

# ── page columns (read by pipeline/block_order.py) ──────────────────────────
# A tile column reads as text when more than this share of its tiles carry ink.
# Deliberately above zero: a gutter on these notices is not pristine white, the
# page border and a header/footer rule cross it. 5% of the page height is a
# handful of such crossings — while a real text column inks a third of its
# tiles or more, so the two never trade places. The cost of this tolerance is
# the heavily-ruled notice: a dozen full-width rules ink the gutter past 5% and
# the page reads as one column. That is the safe way to be wrong — a fully
# ruled notice is a table, whose cells are not independent columns anyway.
GUTTER_MAX_OCCUPANCY = 0.05
# A whitespace channel must be this wide (share of page width) to be counted as
# separating columns. Cell padding inside a table grid is far narrower; a real
# column gutter on these notices runs 3–6%.
MIN_GUTTER_FRAC = 0.025
# ...and each resulting column at least this wide, else it is a marginal strip
# (a rule, a stamp, a page number) and gets folded into its neighbour.
MIN_COLUMN_FRAC = 0.10


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


# An Image block covering more than this share of the page is the parser
# giving up — "the page is a picture" — not a figure it actually handled.
IMAGE_MAX_AREA = 0.5


def _covers(block: dict) -> bool:
    """Does this block represent content we actually read?

    Area alone is not enough. Datalab can return a single ``Text`` block spanning
    the whole page with **empty text**: it claims every tile, so naive coverage
    reports 0% unread on a notice where literally nothing was read. Five such
    documents turned up in the corpus, each with a page-sized empty block and a
    perfect-looking score.

    So a block covers ink only when it carries text (table HTML counts — the
    canonical shape stores it in ``text``), or when it is a genuine embedded
    figure. Figures are exempt because a logo or photo is legitimately ink we
    are not expected to transcribe — but a page-sized one is the same giving-up
    case, so it does not count either.
    """
    if (block.get("text") or "").strip():
        return True
    if block.get("label") == "Image":
        bbox = block.get("bbox")
        if isinstance(bbox, (list, tuple)) and len(bbox) >= 4:
            try:
                area = (float(bbox[2]) - float(bbox[0])) * (float(bbox[3]) - float(bbox[1]))
            except (TypeError, ValueError):
                return False
            return 0 < area < IMAGE_MAX_AREA
    return False


def _covered_tiles(blocks: list[dict], tw: int, th: int, page: int) -> bytearray:
    """Flat tw×th mask of tiles overlapped by a *content-bearing* block on ``page``.

    Block bboxes are normalized 0..1 by both engines (see pipeline/datalab.py
    and pipeline/mineru.py), so they scale straight onto the tile grid.
    """
    covered = bytearray(tw * th)
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if int(b.get("page") or 1) != page:
            continue
        if not _covers(b):
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
        pad = COVER_DILATE_TILES
        ty0 = max(0, int(y0 * th) - pad)
        ty1 = min(th - 1, int(y1 * th) + pad)
        tx0 = max(0, int(x0 * tw) - pad)
        tx1 = min(tw - 1, int(x1 * tw) + pad)
        for ty in range(ty0, ty1 + 1):
            row = ty * tw
            for tx in range(tx0, tx1 + 1):
                covered[row + tx] = 1
    return covered


def _largest_unread_patch(ink: list[float], covered: bytearray,
                          tw: int, th: int) -> tuple[float, tuple[int, int, int, int]]:
    """Ink mass of the biggest connected run of unread inked tiles, and its box.

    4-connected flood fill over the tile grid, iterative so a page-sized patch
    cannot blow the recursion limit. This is what separates a dropped region
    from bbox fringe: the fringe is thousands of isolated edge tiles, a dropped
    column is one connected mass.
    """
    seen = bytearray(len(ink))
    best_mass, best_box = 0.0, (0, 0, 0, 0)
    for start in range(len(ink)):
        if seen[start] or covered[start] or ink[start] < INK_TILE_MIN:
            continue
        stack = [start]
        seen[start] = 1
        mass = 0.0
        x0 = x1 = start % tw
        y0 = y1 = start // tw
        while stack:
            i = stack.pop()
            mass += ink[i]
            x, y = i % tw, i // tw
            x0, x1 = min(x0, x), max(x1, x)
            y0, y1 = min(y0, y), max(y1, y)
            for nx, ny in ((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)):
                if not (0 <= nx < tw and 0 <= ny < th):
                    continue
                j = ny * tw + nx
                if seen[j] or covered[j] or ink[j] < INK_TILE_MIN:
                    continue
                seen[j] = 1
                stack.append(j)
        if mass > best_mass:
            best_mass, best_box = mass, (x0, y0, x1, y1)
    return best_mass, best_box


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
    # Blocks that exist but carry nothing (the page-sized empty block above) are
    # a total parse failure, and reporting ~100% unread is the honest reading —
    # so this deliberately does NOT bail out the way "no blocks" does.

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
    patch_mass, (px0, py0, px1, py1) = _largest_unread_patch(ink, covered, tw, th)
    patch_ratio = patch_mass / total
    out["uncovered_ratio"] = round(ratio, 4)
    out["patch_ratio"] = round(patch_ratio, 4)
    out["flag"] = patch_ratio >= MISSING_REGION_MIN_RATIO
    out["details"] = {
        "uncovered_ratio": round(ratio, 4),
        "patch_ratio": round(patch_ratio, 4),
        # Where the patch sits, normalized 0..1 — enough for a reviewer (or the
        # crop + re-ingest path) to go straight to the missing band.
        "patch_bbox": [round(px0 / tw, 3), round(py0 / th, 3),
                       round((px1 + 1) / tw, 3), round((py1 + 1) / th, 3)],
        "tile_grid": [tw, th],
        "worst_column": _worst_third(ink, covered, tw, th, vertical=False),
        "worst_band": _worst_third(ink, covered, tw, th, vertical=True),
    }
    return out


# ── page layout ─────────────────────────────────────────────────────────────

def _inked_runs(ink: list[float], tw: int, th: int) -> list[list[int]]:
    """Maximal runs of tile columns whose ink occupancy clears the gutter cut.

    Occupancy — the *share of a column's tiles that carry ink*, not their ink
    mass — is the measure that separates a gutter from text. Mass would let one
    heavy full-width rule outweigh a whole column of light type; occupancy asks
    only "does ink appear down the height of this strip", which is exactly what
    a column does and a gutter does not.
    """
    runs: list[list[int]] = []
    for tx in range(tw):
        hits = sum(1 for ty in range(th) if ink[ty * tw + tx] >= INK_TILE_MIN)
        if hits / th <= GUTTER_MAX_OCCUPANCY:
            continue
        if runs and tx == runs[-1][1] + 1:
            runs[-1][1] = tx
        else:
            runs.append([tx, tx])
    return runs


def page_columns(image_bytes: bytes | None) -> dict:
    """Where the page's text columns are, read off the ink itself.

    Returns ``{"columns": [[x0, x1], …], "skipped": str|None}``, columns
    left-to-right in normalized page coordinates. A single-column notice yields
    one entry, not zero — "one column" is a real answer, and the caller should
    not have to distinguish it from a failure to measure.

    Detecting the layout beats assuming it. ``scripts/fix_missing_regions.py``
    slots recovered blocks into reading order by splitting the page into fixed
    thirds (``_column_band``); that is fine for placing one block, but a notice
    whose columns split at 40/60 puts the boundary in the wrong third, and a
    single-column notice gets three bands that do not exist. Here the gutters
    come from the page.
    """
    out: dict = {"columns": [], "skipped": None}
    if not image_bytes:
        out["skipped"] = "no-image"
        return out
    try:
        ink, tw, th = _tile_ink(image_bytes)
    except Exception as e:                       # unreadable/corrupt image
        out["skipped"] = f"unreadable-image: {type(e).__name__}"
        return out

    runs = _inked_runs(ink, tw, th)
    if not runs:
        out["skipped"] = "no-inked-columns"
        return out

    # Close every gap too narrow to be a column gutter (word spacing that lined
    # up down the page, cell padding inside a grid).
    min_gutter = max(2, int(MIN_GUTTER_FRAC * tw))
    cols: list[list[int]] = [list(runs[0])]
    for r in runs[1:]:
        if r[0] - cols[-1][1] - 1 < min_gutter:
            cols[-1][1] = r[1]
        else:
            cols.append(list(r))

    # Fold away strips too narrow to be a column, absorbing the gutter next to
    # them. Merging into the *closer* neighbour keeps a marginal rule with the
    # column it belongs to rather than jumping the page.
    min_col = max(1, int(MIN_COLUMN_FRAC * tw))
    while len(cols) > 1:
        i = min(range(len(cols)), key=lambda k: cols[k][1] - cols[k][0])
        if cols[i][1] - cols[i][0] + 1 >= min_col:
            break
        left = cols[i][0] - cols[i - 1][1] if i else None
        right = cols[i + 1][0] - cols[i][1] if i + 1 < len(cols) else None
        j = i - 1 if right is None or (left is not None and left <= right) else i + 1
        lo, hi = min(i, j), max(i, j)
        cols[lo] = [cols[lo][0], cols[hi][1]]
        del cols[hi]

    out["columns"] = [[round(a / tw, 3), round((b + 1) / tw, 3)] for a, b in cols]
    out["tile_grid"] = [tw, th]
    return out
