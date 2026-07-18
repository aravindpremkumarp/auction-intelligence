"""
pipeline/region_detect.py
-------------------------
Heuristic region detection for sales-notice images: split a notice into
horizontal bands (prose above / ruled table / footer below) so multi-region
re-ingest can OCR each band separately.

Why: full-page MinerU vlm OCR of a fully-bordered notice collapses the whole
page into one giant Table and can degenerate (repetition loops, dropped
properties). Cropping the prose away from the grid before OCR was proven to
fix both — this module finds those crop bands automatically so the 65+
health-flagged notices don't need a reviewer to draw regions by hand.

How it works (Pillow-only, no new runtime deps):
 1. Grayscale + binarize, then compute each row's dark-pixel fraction
    (a single BOX-resample to a 1-px-wide column gives exact row means).
 2. Rows whose dark fraction ≥ RULE_DARK_FRAC are "rule rows". Adjacent
    rule rows merge into one rule; merged runs THICKER than
    MAX_RULE_THICKNESS_FRAC are filled bars (title banners, logo strips) —
    content, not ruling lines — and are discarded.
 3. The property grid is the densest cluster of rules: gaps inside the
    cluster are small (row separators every few % of height). We take the
    longest run of consecutive rules whose gaps ≤ max(MIN_CLUSTER_GAP,
    CLUSTER_GAP_FACTOR × median gap) as the table span.
 4. Bands: [0, table_top] if tall enough → prose region; the table span;
    [table_bottom, 1] if tall enough → footer region. Neighbouring bands
    overlap by BAND_OVERLAP so border pixels aren't lost to either side.

Returns ``None`` when no confident split exists (single-region pages, low
contrast, photos) — callers fall back to full-page OCR unchanged.
"""
from __future__ import annotations

import io


# Row is a "rule row" when ≥ this fraction of its pixels are dark. High on
# purpose: true ruling lines are near-solid (observed 0.75-0.96 dark), while
# banner-edge bleed hovers just above 0.55 — precision beats recall here
# because a missed split falls back to full-page OCR while a wrong split
# wastes an OCR run.
RULE_DARK_FRAC = 0.70
# Binarization threshold on the 0-255 grayscale.
DARK_THRESHOLD = 160
# Merged rule runs thicker than this fraction of page height are filled
# bars (title banners), not ruling lines.
MAX_RULE_THICKNESS_FRAC = 0.008
# Cluster gap ceiling: gap ≤ max(MIN_CLUSTER_GAP, factor × median gap).
# Generous on purpose: property rows in these notices run 6-16% of page
# height apart, and after border/bar suppression the prose area contributes
# no rules at all — so a wide ceiling keeps the grid whole without letting
# the cluster creep into the prose.
MIN_CLUSTER_GAP = 0.18
CLUSTER_GAP_FACTOR = 2.5
# Rules this close to the page top/bottom are the notice's outer border,
# not grid structure — suppressed before clustering.
EDGE_MARGIN = 0.015
# Rules this close to a filled bar are the bar's own edge rows leaking
# through the thickness filter (white text inside a banner breaks the bar
# into thin dark runs) — suppressed.
BAR_ADJACENCY = 0.012
# A real grid rule sits between near-white rows; a banner-edge rule sits
# inside a dark block. Rules whose ±NEIGHBORHOOD_WINDOW surround averages
# more than NEIGHBORHOOD_MAX dark are inside filled content — suppressed.
# (White text fragments a banner into thin sub-max_thick runs that pass the
# thickness filter; their dark surroundings give them away.)
NEIGHBORHOOD_WINDOW = 0.012
NEIGHBORHOOD_MAX = 0.45
# A prose/footer band must be at least this tall to be worth its own OCR.
MIN_BAND_FRAC = 0.03
# Adjacent bands overlap by this much so the boundary rule line lands in both.
BAND_OVERLAP = 0.01
# Need at least this many rules to believe the page has a grid at all.
MIN_RULES = 4


def _row_dark_fractions(image_bytes: bytes) -> list[float]:
    """Per-row fraction of dark pixels, top to bottom, via one BOX resample."""
    from PIL import Image
    with Image.open(io.BytesIO(image_bytes)) as im:
        g = im.convert("L")
        h = g.height
        # dark → 255 so the BOX mean IS the dark fraction (×255).
        mask = g.point(lambda p: 255 if p < DARK_THRESHOLD else 0)
        col = mask.resize((1, h), Image.BOX)
        return [v / 255.0 for v in col.getdata()]


def _find_rules(fracs: list[float]) -> list[float]:
    """Merge consecutive rule rows into rule centers (normalized y).

    Three suppressions turn raw dark rows into grid structure only:
      * runs thicker than MAX_RULE_THICKNESS_FRAC are filled bars (title
        banners, logo strips) — content, not ruling lines;
      * thin runs within BAR_ADJACENCY of a bar are that bar's edge rows
        leaking through (white text inside a banner splits it into thin
        dark runs);
      * rules within EDGE_MARGIN of the page top/bottom are the notice's
        outer border.
    """
    h = len(fracs)
    if h == 0:
        return []
    max_thick = max(1, int(h * MAX_RULE_THICKNESS_FRAC))
    thin: list[float] = []
    bars: list[tuple[float, float]] = []           # (top, bottom) normalized
    run_start: int | None = None
    for i, f in enumerate(fracs + [0.0]):          # sentinel closes last run
        if f >= RULE_DARK_FRAC:
            if run_start is None:
                run_start = i
        elif run_start is not None:
            if (i - run_start) <= max_thick:
                thin.append((run_start + i - 1) / 2.0 / h)
            else:
                bars.append((run_start / h, (i - 1) / h))
            run_start = None

    def near_bar(y: float) -> bool:
        return any(top - BAR_ADJACENCY <= y <= bottom + BAR_ADJACENCY
                   for top, bottom in bars)

    def in_dark_neighborhood(y: float) -> bool:
        win = max(2, int(h * NEIGHBORHOOD_WINDOW))
        c = int(y * h)
        lo, hi = max(0, c - win), min(h, c + win + 1)
        window = fracs[lo:hi]
        return (sum(window) / len(window)) > NEIGHBORHOOD_MAX if window else False

    return [y for y in thin
            if EDGE_MARGIN <= y <= 1.0 - EDGE_MARGIN
            and not near_bar(y)
            and not in_dark_neighborhood(y)]


def _densest_cluster(rules: list[float]) -> tuple[float, float] | None:
    """Longest run of consecutive rules with small gaps → (top, bottom)."""
    if len(rules) < MIN_RULES:
        return None
    gaps = [b - a for a, b in zip(rules, rules[1:])]
    if not gaps:
        return None
    median_gap = sorted(gaps)[len(gaps) // 2]
    ceiling = max(MIN_CLUSTER_GAP, CLUSTER_GAP_FACTOR * median_gap)

    best: tuple[int, int] | None = None      # (start_idx, end_idx) inclusive
    start = 0
    for i, gap in enumerate(gaps + [1.0]):   # sentinel breaks last cluster
        if gap > ceiling:
            if best is None or (i - start) > (best[1] - best[0]):
                best = (start, i)
            start = i + 1
    if best is None or (best[1] - best[0]) + 1 < MIN_RULES:
        return None
    return rules[best[0]], rules[best[1]]


def detect_regions(image_bytes: bytes, *, page: int = 1) -> list[dict] | None:
    """Detect horizontal OCR bands for a notice image.

    Returns ``[{"bbox": [0.0, y0, 1.0, y1], "page": page}, ...]`` (2-3
    bands, top to bottom) or ``None`` when the page has no confident
    prose/table split — callers then keep full-page OCR.
    """
    try:
        fracs = _row_dark_fractions(image_bytes)
    except Exception:
        return None
    rules = _find_rules(fracs)
    cluster = _densest_cluster(rules)
    if cluster is None:
        return None
    top, bottom = cluster

    bands: list[list[float]] = []
    if top >= MIN_BAND_FRAC:
        bands.append([0.0, min(1.0, top + BAND_OVERLAP)])
    bands.append([max(0.0, top - BAND_OVERLAP),
                  min(1.0, bottom + BAND_OVERLAP)])
    if 1.0 - bottom >= MIN_BAND_FRAC:
        bands.append([max(0.0, bottom - BAND_OVERLAP), 1.0])
    if len(bands) < 2:
        # Table fills the page — nothing to separate; full-page OCR is
        # already the faithful treatment.
        return None
    return [{"bbox": [0.0, y0, 1.0, y1], "page": page} for y0, y1 in bands]
