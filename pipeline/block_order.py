"""
pipeline/block_order.py
-----------------------
Detect blocks emitted in the wrong reading order, by checking the sequence the
parser produced against the page's own column layout.

Why this exists
~~~~~~~~~~~~~~~
``pipeline/ink_coverage.py`` answers "was this ink read at all". It cannot
answer "was it read in the right order": coverage merges every block into one
on/off mask, so block identity — and with it sequence — is gone before anything
is measured. A notice whose two columns were emitted interleaved covers 100% of
its ink, closes every tag, repeats nothing, and scores 100.

Order is not cosmetic here. Markdown is assembled in block order, and downstream
extraction reads lot details positionally — the reserve price that follows a lot
number is taken to belong to it. Interleave the columns of a two-column notice
and every lot keeps its neighbour's money.

The measurement
~~~~~~~~~~~~~~~
1. Ask ``ink_coverage.page_columns`` where the columns are (full-height
   whitespace channels in the ink).
2. Assign each content-bearing block to a column. A block straddling a gutter —
   a full-width header, a page-wide table — is **spanning** and takes part in no
   comparison; its correct position is genuinely ambiguous.
3. For every ordered pair of blocks whose relative order is unambiguous, ask
   whether the emitted order matches the expected one:

   - different columns → left column before right (**column-major**: the whole
     left column, then the whole centre, then the right). This is the reading
     order these notices actually have, and the same one
     ``scripts/fix_missing_regions.py`` slots recovered blocks into.
   - same column, vertically disjoint → higher on the page first.
   - anything else (a pair sharing a spanning block, or two blocks side by side
     at the same height) → skipped, not counted either way.

4. ``inversion_ratio`` = violating pairs / comparable pairs.

Pairs, not a sort distance. Comparing the emitted sequence against a sorted one
position-by-position makes the number depend on where the sort put an ambiguous
block; counting only pairs the layout can actually adjudicate keeps every
comparison one we can defend.

What it catches, and what it does not
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
A fully interleaved two-column notice lands around 0.2–0.25 and flags. An
adjacent swap costs one pair out of the whole triangle and does not — a single
misplacement is not worth a health penalty and would fire constantly. Partial
scrambles sit in between and some will be missed. This is a systematic-failure
detector, not a linter.

The ratio is a share of *pairs*, so it scales with how much of the document a
misplacement contradicts, not with a fixed budget of mistakes. One block emitted
far from its place contradicts every other block: about ``2/N`` of the pairs on
an N-block page. On a dense notice that is nothing; below ~14 blocks it clears
the threshold on its own, which is the honest reading — on a page of eight
blocks, one in the wrong place really is a quarter of the document's order.

**Uncalibrated.** ``MISSING_REGION_MIN_RATIO`` next door was set by measuring
the corpus. This threshold was not — it comes from the arithmetic of the pair
count above. Run ``scripts/score_ink_coverage.py --with-order --dry-run`` over a
slice before trusting the rate.

Scope: single-page raster notices, like coverage — blocks carry a 1-indexed
``page`` and only that page's are considered.

The result feeds ``ocr_health.score_ocr_health(markdown, order=…)``, which adds
the ``block-order`` flag and its penalty.
"""
from __future__ import annotations

from pipeline.ink_coverage import page_columns


# A block is spanning — excluded from comparison — when a second column takes
# this share of its width. Below it, the overhang is bbox slop reaching over a
# gutter, not a block that genuinely belongs to both sides.
SPAN_MIN_FRAC = 0.2
# Two blocks in one column count as vertically ordered when the lower one
# starts no higher than this share of the shorter block into the upper one.
# Real text lines abut and their boxes graze each other; a quarter of a block's
# height of overlap is still plainly "one above the other".
Y_OVERLAP_TOL = 0.25
# Below this many comparable pairs the ratio is noise — a handful of blocks can
# swing it from 0 to 0.5. Unscorable, never flagged.
MIN_COMPARABLE_PAIRS = 6
# Flagging threshold on the inversion ratio. See the header: a full column
# interleave scores ~0.21 for four bands and tends to 0.25.
ORDER_INVERSION_MIN_RATIO = 0.15
# Pair counting is O(n²). Notices run to a few hundred blocks; this bounds the
# pathological case rather than expressing a real limit.
MAX_BLOCKS = 400


def _page_blocks(blocks: list[dict], page: int) -> list[tuple[int, tuple]]:
    """``(emitted_index, bbox)`` for blocks on ``page`` that carry text.

    Text, not ``ink_coverage._covers``: coverage counts a figure as ink it is
    fair not to transcribe, but a figure contributes nothing to the *sequence*
    of the markdown, so sequencing it would invent violations no reader could
    see. Table HTML lives in ``text`` and so counts, as one unit.
    """
    out: list[tuple[int, tuple]] = []
    for i, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        if int(b.get("page") or 1) != page:
            continue
        if not (b.get("text") or "").strip():
            continue
        bbox = b.get("bbox")
        if not (isinstance(bbox, (list, tuple)) and len(bbox) >= 4):
            continue
        try:
            box = (float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3]))
        except (TypeError, ValueError):
            continue
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        out.append((i, box))
        if len(out) >= MAX_BLOCKS:
            break
    return out


def _overlaps(box: tuple, columns: list[list[float]]) -> list[float]:
    """Width of this block's overlap with each column."""
    return [max(0.0, min(box[2], c1) - max(box[0], c0)) for c0, c1 in columns]


def _column_of(box: tuple, columns: list[list[float]]) -> int | None:
    """The block's column, or ``None`` when it spans a gutter or misses them all."""
    ov = _overlaps(box, columns)
    best = max(range(len(ov)), key=lambda i: ov[i])
    if ov[best] <= 0:
        return None                      # sits entirely in a gutter/margin
    width = box[2] - box[0]
    second = max((v for i, v in enumerate(ov) if i != best), default=0.0)
    if width > 0 and second / width >= SPAN_MIN_FRAC:
        return None                      # full-width header, page-wide table
    return best


def _primary_column(box: tuple, columns: list[list[float]]) -> int:
    """The column a block mostly sits in — spanning blocks included.

    Used only for :func:`reading_order`, which has to place every block
    somewhere. Scoring uses :func:`_column_of` and refuses to guess.
    """
    ov = _overlaps(box, columns)
    return max(range(len(ov)), key=lambda i: ov[i])


def _vertical_order(a: tuple, b: tuple) -> int:
    """``-1`` a above b, ``1`` b above a, ``0`` they share height (no verdict)."""
    tol = Y_OVERLAP_TOL * min(a[3] - a[1], b[3] - b[1])
    if a[3] <= b[1] + tol:
        return -1
    if b[3] <= a[1] + tol:
        return 1
    return 0


def reading_order(blocks: list[dict] | None, columns: list[list[float]] | None,
                  *, page: int = 1) -> list[int]:
    """Emitted block indexes, resequenced column-major for the given columns.

    What a fix path would write back. Spanning blocks are placed by the column
    they mostly occupy, which puts a full-width header at the top of the left
    column (right) and a full-width footer at its foot (not right — it belongs
    after the last column). Rather than guess, this keeps the same convention
    ``scripts/fix_missing_regions._column_band`` already uses; a caller that
    needs footers handled properly must special-case them.
    """
    if not blocks or not columns:
        return []
    items = _page_blocks(blocks, page)
    return [i for i, _ in sorted(
        items, key=lambda it: (_primary_column(it[1], columns), it[1][1], it[1][0]))]


def score_block_order(image_bytes: bytes | None, blocks: list[dict] | None,
                      *, page: int = 1) -> dict:
    """Measure how much of the emitted block sequence contradicts the layout.

    Returns ``{"inversion_ratio": float|None, "flag": bool, "details": {…}}``.
    ``inversion_ratio`` is ``None`` — unscorable, never flagged — when the page
    layout cannot be read or too few pairs can be adjudicated. As with
    coverage, "we could not tell" is reported as such and never as a failure.
    """
    out: dict = {"inversion_ratio": None, "flag": False, "details": {}}
    if not image_bytes or not blocks:
        out["details"]["skipped"] = "no-image" if not image_bytes else "no-blocks"
        return out

    items = _page_blocks(blocks, page)
    if len(items) < 2:
        out["details"]["skipped"] = "too-few-blocks-on-page"
        return out

    layout = page_columns(image_bytes)
    if layout["skipped"] or not layout["columns"]:
        out["details"]["skipped"] = layout["skipped"] or "no-columns"
        return out
    columns = layout["columns"]

    placed = [(i, box, _column_of(box, columns)) for i, box in items]
    cross = cross_bad = within = within_bad = 0
    first_bad: dict | None = None
    for a in range(len(placed)):
        ia, box_a, col_a = placed[a]
        if col_a is None:
            continue
        for b in range(a + 1, len(placed)):
            ib, box_b, col_b = placed[b]
            if col_b is None:
                continue
            if col_a != col_b:
                cross += 1
                bad = col_a > col_b
                kind = "cross-column"
            else:
                v = _vertical_order(box_a, box_b)
                if v == 0:               # side by side — no order to violate
                    continue
                within += 1
                bad = v > 0
                kind = "within-column"
            if not bad:
                continue
            if kind == "cross-column":
                cross_bad += 1
            else:
                within_bad += 1
            if first_bad is None:
                first_bad = {"kind": kind, "emitted": [ia, ib],
                             "columns": [col_a, col_b],
                             "bboxes": [[round(v, 3) for v in box_a],
                                        [round(v, 3) for v in box_b]]}

    comparable = cross + within
    if comparable < MIN_COMPARABLE_PAIRS:
        out["details"] = {"skipped": "too-few-comparable-pairs",
                          "comparable_pairs": comparable,
                          "columns": columns}
        return out

    inversions = cross_bad + within_bad
    ratio = inversions / comparable
    out["inversion_ratio"] = round(ratio, 4)
    out["flag"] = ratio >= ORDER_INVERSION_MIN_RATIO
    out["details"] = {
        "inversion_ratio": round(ratio, 4),
        "columns": columns,
        "blocks_compared": len(placed),
        "spanning_blocks": sum(1 for _, _, c in placed if c is None),
        "comparable_pairs": comparable,
        "inversions": inversions,
        # Which of the two failures this is, and so what a fix would do:
        # cross-column means the columns were interleaved (resequence
        # column-major), within-column means blocks came out of vertical order.
        "cross_column": {"pairs": cross, "inversions": cross_bad,
                         "ratio": round(cross_bad / cross, 3) if cross else 0.0},
        "within_column": {"pairs": within, "inversions": within_bad,
                          "ratio": round(within_bad / within, 3) if within else 0.0},
    }
    if first_bad:
        # The first offending pair, so a reviewer can go straight to it rather
        # than re-deriving the comparison from a ratio.
        out["details"]["first_inversion"] = first_bad
    return out
