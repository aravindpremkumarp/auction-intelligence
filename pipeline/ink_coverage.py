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
# Ruling lines are ink no block ever claims. Block bboxes hug their text, so a
# bordered notice's grid — outer border, row separators, column rules — always
# falls outside every box. Worse, the grid is CONNECTED: every rule touches
# every other, so the largest-contiguous-patch test (the whole point of which is
# that a dropped region is one solid mass) sees the table skeleton as one huge
# unread patch and flags a notice whose every cell was read correctly.
#
# Measured over a sample of flagged notices: rule ink is 12-52% of all page ink
# (median ~34%), and 5 of the 6 that flagged stopped flagging once it was
# removed. So strip long runs before measuring — a rule is ink that runs
# straight for a distance no glyph does.
#
# Long is not enough on its own: a filled title banner ("E-AUCTION SALE NOTICE"
# reversed out of black) also runs the width of the page, and that is content.
# A rule is long AND THIN, so anything thicker than RULE_MAX_THICK is kept.
RULE_MIN_LEN_FRAC = 0.06    # of page width (horizontal) / height (vertical)
RULE_MIN_LEN_PX = 24        # floor for small scans; still >> any glyph stroke
RULE_MAX_THICK_FRAC = 0.008  # matches region_detect's filled-bar cutoff
RULE_MAX_THICK_PX = 6
# Graphics are ink that is not text: bank logos, seals, photos, QR codes, and
# reversed-out banners. None of it is text we failed to read, but all of it is
# dark ink outside every block whenever the parser emitted no Image block for
# it — which is exactly when it lands in the unread patch and flags a notice.
#
# Text gives itself away by its strokes: a glyph is thin in at least one
# direction (a few px of pen width) no matter how large the type. Graphics are
# solid — ink that survives erosion in BOTH directions at once. That is the
# test. Note this makes white-on-black banner text invisible to the measure,
# which costs nothing: those glyphs are white, so they were never dark ink to
# begin with — only the bar behind them ever was.
BLOB_MIN_PX = 10
BLOB_MIN_FRAC = 0.012       # of the page's short edge
# Newspaper chrome: these scans are clippings, so the page often carries the
# masthead, edition date and epaper URL of the paper it was cut from. That is
# not notice content and no block will ever cover it, so it lands in the unread
# measure forever.
#
# What separates chrome from a genuinely unread footer is the gutter: chrome is
# marooned at the page edge behind a band of white far wider than any line gap
# inside the notice, whereas an unread footer butts up against the text above
# it. So walk in from each edge and drop only what sits beyond such a gap.
CHROME_GUTTER_TILES = 3     # ~24px at TILE_PX — well past any line spacing
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


def _shift(mask, dx: int, dy: int):
    """``mask`` translated by (dx, dy), vacated edges filled black.

    Pillow's ``ImageChops.offset`` wraps around, which would smear the right
    edge onto the left and invent rules; pasting onto a black canvas does not.
    """
    from PIL import Image
    out = Image.new("L", mask.size, 0)
    out.paste(mask, (dx, dy))
    return out


def _long_runs(mask, length: int, *, horizontal: bool):
    """Pixels belonging to a straight dark run of at least ``length`` px.

    Erode along the axis (a pixel survives only if the whole run does), then
    dilate back so the rule returns to its real thickness. Both use doubling
    shifts, so it is O(log length) Pillow ops rather than a Python pixel loop —
    ~15ms on a full-page notice.
    """
    from PIL import ImageChops
    step, eroded = 1, mask
    while step < length:
        eroded = ImageChops.darker(
            eroded, _shift(eroded, -step if horizontal else 0,
                           0 if horizontal else -step))
        step *= 2
    step, out = 1, eroded
    while step < length:
        out = ImageChops.lighter(
            out, _shift(out, step if horizontal else 0,
                        0 if horizontal else step))
        step *= 2
    return out


def _thin_rules(mask, length: int, thickness: int, *, horizontal: bool):
    """Long runs along the axis, minus the parts thick across it.

    The thickness pass is the same erode-dilate run the other way: whatever
    survives it is a filled bar (a reversed-out title banner, a logo strip) and
    is content, so it is put back.
    """
    from PIL import ImageChops
    long_ = _long_runs(mask, length, horizontal=horizontal)
    thick = _long_runs(long_, thickness + 1, horizontal=not horizontal)
    return ImageChops.subtract(long_, thick)


def _solid_blobs(mask, size: int):
    """Ink thick in both directions at once — graphics, not glyph strokes.

    Erode along each axis in turn (so only ink at least ``size`` px across both
    ways survives), then dilate back the same way to restore the blob's real
    extent.
    """
    return _long_runs(_long_runs(mask, size, horizontal=True),
                      size, horizontal=False)


def _strip_rules(mask, w: int, h: int):
    """``mask`` minus its horizontal and vertical ruling lines."""
    from PIL import ImageChops
    rules = ImageChops.lighter(
        _thin_rules(mask,
                    max(RULE_MIN_LEN_PX, int(w * RULE_MIN_LEN_FRAC)),
                    max(RULE_MAX_THICK_PX, int(h * RULE_MAX_THICK_FRAC)),
                    horizontal=True),
        _thin_rules(mask,
                    max(RULE_MIN_LEN_PX, int(h * RULE_MIN_LEN_FRAC)),
                    max(RULE_MAX_THICK_PX, int(w * RULE_MAX_THICK_FRAC)),
                    horizontal=False),
    )
    return ImageChops.subtract(mask, rules)


def _tile_ink(image_bytes: bytes) -> tuple[list[float], int, int]:
    """Per-tile dark fraction in reading order, plus the tile grid dimensions.

    Ruling lines are removed first (see RULE_MIN_LEN_FRAC) so a bordered grid
    doesn't read as unread content. One BOX resample of the remaining dark mask
    gives exact per-tile means, so this stays cheap regardless of page size.
    """
    from PIL import Image
    with Image.open(io.BytesIO(image_bytes)) as im:
        g = im.convert("L")
        w, h = g.width, g.height
        tw, th = max(1, w // TILE_PX), max(1, h // TILE_PX)
        from PIL import ImageChops
        # dark → 255 so the BOX mean IS the dark fraction (×255).
        mask = _strip_rules(g.point(lambda p: 255 if p < DARK_THRESHOLD else 0),
                            w, h)
        mask = ImageChops.subtract(
            mask, _solid_blobs(mask, max(BLOB_MIN_PX,
                                         int(min(w, h) * BLOB_MIN_FRAC))))
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


def _drop_detached_chrome(ink: list[float], covered: bytearray,
                          tw: int, th: int) -> list[float]:
    """Blank the ink in edge bands cut off from the read page by a wide gutter.

    Works on the tile grid, one axis at a time: from each of the four edges,
    walk inward to the first tile line any block covers. If a run of at least
    ``CHROME_GUTTER_TILES`` blank lines sits between the edge and that line,
    everything outside the run is chrome and its ink is dropped.

    Anchoring on covered lines rather than inked ones is what keeps this
    honest: an unread footer is separated from the *read* page by the same kind
    of gap, so the rule would eat it. Instead the walk stops at the first blank
    run it meets coming inward, which for an unread footer is the gap above the
    footer's own ink — leaving the footer itself measured.
    """
    def line_state(i: int, vertical: bool) -> tuple[bool, bool]:
        """(has ink, has a covered tile) for row/column ``i``."""
        inked = cov = False
        rng = range(tw) if vertical else range(th)
        for j in rng:
            t = i * tw + j if vertical else j * tw + i
            if ink[t] >= INK_TILE_MIN:
                inked = True
            if covered[t]:
                cov = True
            if inked and cov:
                break
        return inked, cov

    out = list(ink)
    for vertical in (True, False):
        span = th if vertical else tw
        states = [line_state(i, vertical) for i in range(span)]
        if not any(c for _, c in states):
            continue                            # nothing read on this axis
        for from_start in (True, False):
            order = range(span) if from_start else range(span - 1, -1, -1)
            blank_run, cut = 0, None
            for i in order:
                inked, cov = states[i]
                if cov:
                    break                       # reached the read page
                if inked:
                    blank_run = 0
                    continue
                blank_run += 1
                if blank_run >= CHROME_GUTTER_TILES:
                    cut = i                     # chrome lies beyond this run
                    break
            if cut is None:
                continue
            dead = (range(0, cut + 1) if from_start
                    else range(cut, span))
            for i in dead:
                rng = range(tw) if vertical else range(th)
                for j in rng:
                    out[i * tw + j if vertical else j * tw + i] = 0.0
    return out


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


def _measure(image_bytes: bytes | None, blocks: list[dict] | None,
             page: int) -> tuple[dict, list[float] | None, bytearray | None,
                                 int, int]:
    """The measurement itself: verdict plus the tile grids it was read off.

    Returns ``(verdict, ink, covered, tw, th)``; the grids are ``None`` on an
    unscorable page. Split out so the annotator's coverage map
    (:func:`coverage_map`) shows the reviewer the exact tiles the score was
    computed from — a second implementation for the UI would drift from the
    verdict the queue is filtered on.
    """
    out: dict = {"uncovered_ratio": None, "flag": False, "details": {}}
    if not image_bytes or not blocks:
        out["details"]["skipped"] = "no-image" if not image_bytes else "no-blocks"
        return out, None, None, 0, 0
    if not any(isinstance(b, dict) and int(b.get("page") or 1) == page
               for b in blocks):
        out["details"]["skipped"] = "no-blocks-on-page"
        return out, None, None, 0, 0
    # Blocks that exist but carry nothing (the page-sized empty block above) are
    # a total parse failure, and reporting ~100% unread is the honest reading —
    # so this deliberately does NOT bail out the way "no blocks" does.

    try:
        ink, tw, th = _tile_ink(image_bytes)
    except Exception as e:                       # unreadable/corrupt image
        out["details"]["skipped"] = f"unreadable-image: {type(e).__name__}"
        return out, None, None, 0, 0

    covered = _covered_tiles(blocks, tw, th, page)
    ink = _drop_detached_chrome(ink, covered, tw, th)
    total = missed = 0.0
    for i, v in enumerate(ink):
        if v < INK_TILE_MIN:
            continue
        total += v
        if not covered[i]:
            missed += v
    if total < MIN_TOTAL_INK:
        out["details"]["skipped"] = "too-little-ink"
        return out, ink, covered, tw, th

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
    return out, ink, covered, tw, th


def score_ink_coverage(image_bytes: bytes | None, blocks: list[dict] | None,
                       *, page: int = 1) -> dict:
    """Measure how much of the page's ink no parsed block covers.

    Returns ``{"uncovered_ratio": float|None, "flag": bool, "details": {…}}``.
    ``uncovered_ratio`` is ``None`` — unscorable, never flagged — when there is
    no image, no block for the page, or too little ink to judge. "No blocks" in
    particular must not read as "100% missing": a doc that never produced blocks
    is a different failure, already visible upstream.
    """
    return _measure(image_bytes, blocks, page)[0]


def coverage_map(image_bytes: bytes | None, blocks: list[dict] | None,
                 *, page: int = 1) -> dict:
    """:func:`score_ink_coverage`, plus the tile grids behind the verdict.

    Adds, when the page was scorable:

        tile_px    int    tile edge in source pixels
        tile_w/h   int    grid dimensions
        ink        bytes  per-tile dark fraction, 0-255, reading order
        covered    bytes  1 where a content-bearing block covers the tile

    Both grids are ``tile_w * tile_h`` bytes in reading order, so a caller can
    paint them straight onto the page: an inked tile (``ink >= INK_TILE_MIN``)
    that is not covered is exactly the unread ink the flag is scored on.
    """
    out, ink, covered, tw, th = _measure(image_bytes, blocks, page)
    if ink is None or covered is None:
        return out
    return {
        **out,
        "tile_px": TILE_PX,
        "tile_w": tw,
        "tile_h": th,
        "ink_min": INK_TILE_MIN,
        "ink": bytes(min(255, int(round(v * 255))) for v in ink),
        "covered": bytes(covered),
    }
