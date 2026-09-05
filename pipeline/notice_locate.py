"""
pipeline/notice_locate.py
-------------------------
Find the one sale notice we care about on a scanned full-newspaper page, and
return the crop box around it.

Why: portals sometimes upload the whole newspaper page (e.g. the classifieds
page with ten different banks' notices on it) as a property's "sale notice".
OCR of that page reads every notice, so the property's markdown is buried in
nine unrelated notices and every downstream step (description matching,
extraction, health scoring) degrades. Reviewers have been drawing the crop by
hand in the annotator; this module draws it for them.

How it works (Pillow-only, no new runtime deps):
 1. **Text anchoring.** The full-page OCR blocks we already store carry a
    normalized bbox and text. Each block is scored against *hints* derived
    from the property record the page is linked to — reserve price (every
    format ``markdown_match`` knows), borrower name tokens, bank name,
    auction date, and a fuzzy match of the scraped website description.
    Blocks that mention none of these score 0.
 2. **Clustering.** The best-scoring block seeds a cluster; other scoring
    blocks join only if they sit next to the cluster (within
    ``CLUSTER_GAP``). A distant block that merely mentions the same bank is
    another notice and stays out.
 3. **Snap to the frame.** Newspaper notices are boxed, or at least separated
    by a white gutter. Starting from the cluster's bbox we walk outward on
    each axis over the page's dark-pixel profile and stop at the first
    ruling line that spans the cluster (include it) or the first white
    gutter wider than ``MIN_GUTTER`` that leads to more ink (stop mid-gutter).
    Short blank runs (word spacing, cell padding) and partial internal rules
    are crossed. Two x/y passes so each axis benefits from the other's frame.

Returns ``None`` when there is nothing to anchor on (no hints, no matching
block, or the OCR collapsed the page into one giant block) — callers keep the
full page.
"""
from __future__ import annotations

import io
import re
from typing import Any

# ── Scoring weights ─────────────────────────────────────────────────────────
# A reserve price is close to a fingerprint; a bank name is shared by every
# notice that bank placed on the page, so it only corroborates.
W_PRICE = 3.0
W_DESCRIPTION = 3.0
W_BORROWER = 2.0
W_DATE = 1.0
W_BANK = 1.0
W_PLACE = 0.5
# Fuzzy description match (rapidfuzz partial_ratio, 0-100) must reach this
# to count. Block text is short, so this is a "the paragraph is here" test.
DESCRIPTION_MIN_SCORE = 80.0
# Description matching is only meaningful against a block long enough to
# hold part of a description.
DESCRIPTION_MIN_BLOCK_CHARS = 40
# A cluster needs at least this much evidence before we trust it.
MIN_CLUSTER_SCORE = 3.0

# ── Geometry (all fractions of the page) ───────────────────────────────────
# A block whose only evidence is a place name (city / area — printed in
# every notice of the district) joins the cluster only when this close to
# it. Every other scoring block joins at any distance: a price, borrower or
# description is near-unique on the page, and the bank name / auction date
# mark the notice's own header and footer, which sit far from the lot row
# in a multi-lot notice. (A same-bank neighbour on the page joins too; the
# crop then spans both notices — conservative, never cuts the target.)
CLUSTER_GAP = 0.02
# Hint kinds weighing at least this are "strong" (reported for audit).
STRONG_HINT_WEIGHT = 2.0
# A block this large is the OCR collapsing most of the page into one table
# — nothing to localize with.
MAX_ANCHOR_AREA = 0.45
# Ink profile: pixel is dark below this grayscale value.
DARK_THRESHOLD = 160
# A profile column/row is a ruling line when *solid* ink (unbroken runs at
# least SOLID_RUN of the extent long — text glyphs never are) covers at
# least this fraction of the cluster's extent. < 1 tolerates scan breaks.
RULE_FRAC = 0.60
# Run length, as a fraction of the extent, that counts as solid. Must be
# longer than a text line's x-height so a dense line can't pass.
SOLID_RUN = 0.03
SOLID_RUN_MIN_PX = 12
# A run of rule columns/rows thicker than this fraction of the axis is a
# filled bar (title banner) — content the walk passes through, not a frame.
MAX_RULE_THICK = 0.004
# A column/row is "white" (gutter / padding) when its mean darkness is below
# this. Leaves room for scan speckle and the frame lines crossing it.
GUTTER_FRAC = 0.03
# Just past a candidate frame rule, the perpendicular side line "continues"
# (so the rule is a table row, not the frame) when its mean darkness over
# one gutter length is at least this.
CONTINUES_FRAC = 0.5
# A thin, faint run (≤ MAX_RULE_THICK, mean darkness ≤ this) inside a blank
# stretch is a neighbour's partial frame line or a fold crease, not content:
# it is merged into the blank run so a gutter still reads as a gutter.
NOISE_FRAC = 0.15
# Walks start this far *inside* the anchor. OCR block boxes routinely include
# the frame line itself, so a walk starting at the box edge would step over
# its own frame without seeing it.
START_INSET = 0.01
# A blank run must be at least this wide to be a gutter between notices;
# narrower runs are padding or spacing inside the notice and are crossed.
# Newspaper column gutters run ~1% of the page width; box padding ~0.5%.
MIN_GUTTER_X = 0.008
MIN_GUTTER_Y = 0.008
# ...and at least this many times the widest blank gap inside the anchor,
# capped at MAX_GUTTER so an empty stretch inside the notice (a signature
# space, empty table cells) can't push the threshold past real gutters.
GUTTER_OVER_INTERNAL = 1.25
MAX_GUTTER = 0.02
# Never expand further than this from the cluster on either side — a page
# with no frame and no gutter would otherwise swallow everything.
MAX_REACH = 0.35
# Padding added around the final box (inside gutters, outside rules).
PAD = 0.004
# A located box smaller than this is degenerate — rejected.
MIN_CROP_AREA = 0.01


# ── Hints ───────────────────────────────────────────────────────────────────

_MONTHS = ["jan", "feb", "mar", "apr", "may", "jun",
           "jul", "aug", "sep", "oct", "nov", "dec"]
_BANK_NOISE = re.compile(
    r"\b(?:the|ltd\.?|limited|co\.?|company|bank|of|india)\b|[().,]",
    re.IGNORECASE)


def _date_variants(iso: str | None) -> list[str]:
    """``2026-03-25T11:30:00`` → ``25.03.2026``, ``25/03/2026``, ``25-03-2026``,
    ``25.03.26``, ``25 Mar 2026``, ``25th March 2026``-ish prefixes."""
    if not iso:
        return []
    m = re.match(r"\s*(\d{4})-(\d{2})-(\d{2})", str(iso))
    if not m:
        return []
    y, mo, d = m.group(1), m.group(2), m.group(3)
    out = [f"{d}.{mo}.{y}", f"{d}/{mo}/{y}", f"{d}-{mo}-{y}",
           f"{d}.{mo}.{y[2:]}", f"{d}/{mo}/{y[2:]}", f"{d}-{mo}-{y[2:]}"]
    try:
        mon = _MONTHS[int(mo) - 1]
    except (ValueError, IndexError):
        return out
    dd = d.lstrip("0") or "0"
    out += [f"{d} {mon} {y}", f"{dd} {mon} {y}", f"{d}-{mon}-{y}",
            f"{dd}-{mon}-{y}", f"{d} {mon}", f"{dd} {mon}"]
    return out


def build_hints(properties: list[dict]) -> list[dict]:
    """Turn linked property records into ``{kind, pattern, weight, ci}`` probes.

    Each property may carry ``reserve_price``, ``borrowers`` (list of names),
    ``bank``, ``auction_start`` (ISO), ``website_description``, ``city``,
    ``area``. Missing fields simply contribute no hints. ``kind ==
    "description"`` hints are fuzzy (``pattern`` is the normalized
    description); every other kind is a substring probe.
    """
    from api.review.markdown_match import (
        _borrower_token, _normalize_for_match, _price_patterns,
        strip_field_bleed,
    )
    hints: list[dict] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, pattern: str, weight: float, ci: bool) -> None:
        key = (kind, pattern.lower() if ci else pattern)
        if not pattern or key in seen:
            return
        seen.add(key)
        hints.append({"kind": kind, "pattern": pattern,
                      "weight": weight, "ci": ci})

    for p in properties or []:
        for pat, ci in _price_patterns(p.get("reserve_price")):
            add("price", pat, W_PRICE, ci)
        for b in p.get("borrowers") or []:
            tok = _borrower_token(b)
            if tok:
                add("borrower", tok, W_BORROWER, True)
        bank = (p.get("bank") or "").strip()
        if bank:
            add("bank", bank, W_BANK, True)
            core = " ".join(_BANK_NOISE.sub(" ", bank).split())
            if len(core) >= 4 and core.lower() != bank.lower():
                add("bank", core, W_BANK, True)
        for v in _date_variants(p.get("auction_start")):
            add("date", v, W_DATE, True)
        desc = _normalize_for_match(strip_field_bleed(p.get("website_description")))
        if len(desc) >= DESCRIPTION_MIN_BLOCK_CHARS:
            add("description", desc, W_DESCRIPTION, True)
        for k in ("city", "area"):
            v = (p.get(k) or "").strip()
            if len(v) >= 4:
                add("place", v, W_PLACE, True)
    return hints


def score_text(text: str | None, hints: list[dict], *,
               with_strength: bool = False):
    """Sum of the weights of every hint kind the text satisfies.

    A kind counts once per block (three price formats matching the same
    number is one piece of evidence), so the score is the sum over matched
    *kinds*, with the best weight in each. Returns ``(score, kinds)``, or
    ``(score, kinds, strong)`` with ``with_strength=True`` where ``strong``
    says a hint weighing ≥ ``STRONG_HINT_WEIGHT`` matched.
    """
    if not text or not hints:
        return (0.0, [], False) if with_strength else (0.0, [])
    low = text.lower()
    best: dict[str, float] = {}
    for h in hints:
        kind, pat, w = h["kind"], h["pattern"], float(h["weight"])
        if kind == "description":
            if len(text) < DESCRIPTION_MIN_BLOCK_CHARS:
                continue
            from rapidfuzz import fuzz
            from api.review.markdown_match import _normalize_for_match
            nb = _normalize_for_match(text)
            if not nb:
                continue
            # The block is the short side: how much of the block is in the
            # description, not the other way round.
            probe, hay = (nb, pat) if len(nb) <= len(pat) else (pat, nb)
            if fuzz.partial_ratio(probe, hay) < DESCRIPTION_MIN_SCORE:
                continue
            hit = True
        else:
            hay = low if h.get("ci") else text
            needle = pat.lower() if h.get("ci") else pat
            hit = needle in hay
        if hit and w > best.get(kind, 0.0):
            best[kind] = w
    score, kinds = sum(best.values()), sorted(best)
    if with_strength:
        return score, kinds, any(w >= STRONG_HINT_WEIGHT for w in best.values())
    return score, kinds


# ── Clustering ──────────────────────────────────────────────────────────────

def _bbox_of(block: dict) -> list[float] | None:
    b = block.get("bbox")
    if not (isinstance(b, (list, tuple)) and len(b) == 4):
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in b)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= x0 < x1 <= 1.0 and 0.0 <= y0 < y1 <= 1.0):
        return None
    return [x0, y0, x1, y1]


def _area(b: list[float]) -> float:
    return (b[2] - b[0]) * (b[3] - b[1])


def _near(a: list[float], b: list[float], gap: float) -> bool:
    return (a[0] - gap <= b[2] and b[0] - gap <= a[2]
            and a[1] - gap <= b[3] and b[1] - gap <= a[3])


def _union(a: list[float], b: list[float]) -> list[float]:
    return [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]


def anchor_blocks(blocks: list[dict], hints: list[dict], *,
                  page: int = 1) -> dict | None:
    """Score the page's blocks and grow a cluster around the best one.

    Returns ``{"bbox", "score", "matched", "blocks"}`` (bbox = union of the
    cluster's blocks, blocks = their ids) or ``None`` when no block on
    ``page`` scores, or the best block is too large to localize anything.
    """
    scored: list[tuple[float, list[str], list[float], str]] = []
    for blk in blocks or []:
        if int(blk.get("page") or 1) != page:
            continue
        bbox = _bbox_of(blk)
        if bbox is None:
            continue
        s, kinds = score_text(blk.get("text"), hints)
        if s > 0:
            anywhere = any(k != "place" for k in kinds)
            scored.append((s, kinds, bbox, str(blk.get("id") or ""), anywhere))
    if not scored:
        return None
    scored.sort(key=lambda t: (-t[0], _area(t[2])))
    seed_score, _, seed_bbox, _, _ = scored[0]
    if _area(seed_bbox) > MAX_ANCHOR_AREA:
        return None

    cluster = list(seed_bbox)
    members: list[int] = [0]
    remaining = list(range(1, len(scored)))
    grew = True
    while grew:
        grew = False
        for i in list(remaining):
            bbox, anywhere = scored[i][2], scored[i][4]
            if _area(bbox) > MAX_ANCHOR_AREA:
                remaining.remove(i)
                continue
            if anywhere or _near(cluster, bbox, CLUSTER_GAP):
                cluster = _union(cluster, bbox)
                members.append(i)
                remaining.remove(i)
                grew = True
    matched: set[str] = set()
    total = 0.0
    for i in members:
        total += scored[i][0]
        matched.update(scored[i][1])
    return {"bbox": cluster, "score": round(total, 2),
            "matched": sorted(matched),
            "blocks": [scored[i][3] for i in members if scored[i][3]]}


# ── Frame snapping ──────────────────────────────────────────────────────────

def _dark_mask(image_bytes: bytes):
    from PIL import Image
    with Image.open(io.BytesIO(image_bytes)) as im:
        g = im.convert("L")
        g.load()
    # dark → 255 so a BOX mean IS the dark fraction (×255).
    return g.point(lambda p: 255 if p < DARK_THRESHOLD else 0)


def _profile(mask, axis: str, lo: int, hi: int) -> tuple[list[float], list[float]]:
    """``(dark, solid)`` per column (axis='x', over rows lo..hi) or per row
    (axis='y', over columns lo..hi).

    ``dark`` is the mean dark fraction — what tells ink from white. ``solid``
    is the fraction of the extent covered by unbroken dark runs at least
    ``SOLID_RUN`` long: a ruling line scores ~1 on the axis it spans, text
    (short glyph runs) scores ~0, so a partial internal rule scores its own
    span and not the text sitting beside it. Computed with one BOX downscale
    by the run length (a cell is full only if every pixel in it is dark),
    a threshold, and one BOX collapse.
    """
    from PIL import Image
    w, h = mask.size
    extent = hi - lo
    k = max(SOLID_RUN_MIN_PX, int(extent * SOLID_RUN))
    if axis == "x":
        strip = mask.crop((0, lo, w, hi))
        dark = strip.resize((w, 1), Image.BOX)
        cells = max(1, extent // k)
        solid = (strip.resize((w, cells), Image.BOX)
                 .point(lambda p: 255 if p >= 240 else 0)
                 .resize((w, 1), Image.BOX))
    else:
        strip = mask.crop((lo, 0, hi, h))
        dark = strip.resize((1, h), Image.BOX)
        cells = max(1, extent // k)
        solid = (strip.resize((cells, h), Image.BOX)
                 .point(lambda p: 255 if p >= 240 else 0)
                 .resize((1, h), Image.BOX))
    return ([v / 255.0 for v in dark.tobytes()],
            [v / 255.0 for v in solid.tobytes()])


RULE, BAR, INK, BLANK, EDGE, REACH = "rule", "bar", "ink", "blank", "edge", "reach"


def _tokens(dark: list[float], solid: list[float], start: int, step: int, *,
            max_thick: int, max_reach: int) -> list[tuple[str, int, int]]:
    """Runs along the profile from ``start`` in direction ``step``, as
    ``(kind, first, last)`` with ``first``/``last`` inclusive in walk order.
    Kinds: RULE (thin solid line), BAR (solid run thicker than
    ``max_thick`` — a filled banner), INK, BLANK; the list always ends with
    a zero-width EDGE (page edge) or REACH (``max_reach`` exhausted)."""
    n = len(dark)

    def kind_at(i: int) -> str:
        if solid[i] >= RULE_FRAC:
            return RULE
        return BLANK if dark[i] <= GUTTER_FRAC else INK

    raw: list[tuple[str, int, int]] = []
    i = start
    travelled = 0
    while 0 <= i < n and travelled <= max_reach:
        k = kind_at(i)
        j = i
        while 0 <= j + step < n and kind_at(j + step) == k:
            j += step
        if k == RULE and abs(j - i) + 1 > max_thick:
            k = BAR
        elif (k == INK and abs(j - i) + 1 <= max_thick
              and max(dark[min(i, j):max(i, j) + 1]) <= NOISE_FRAC):
            k = BLANK                      # faint speck / partial line: not content
        raw.append((k, i, j))
        travelled += abs(j - i) + 1
        i = j + step
    # Merge the runs the noise rule turned blank into their neighbours.
    out: list[tuple[str, int, int]] = []
    for k, a, b in raw:
        if out and out[-1][0] == k:
            out[-1] = (k, out[-1][1], b)
        else:
            out.append((k, a, b))
    out.append((EDGE if not (0 <= i < n) else REACH, i, i))
    return out


def _solid_across(mask, axis: str, band: tuple[int, int],
                  span: tuple[int, int]) -> float:
    """Best solid-run coverage of any single column (axis='x': ``band`` is a
    column range, ``span`` a row range) or row (axis='y': the reverse) inside
    ``band`` across ``span``. Used to ask "does a frame side run along here?"
    — a thin line inside a band of a few pixels, so per-line rather than a
    band mean which would dilute it."""
    from PIL import Image
    lo, hi = sorted(span)
    b0, b1 = sorted(band)
    extent = hi - lo + 1
    if extent < 4 or b1 < b0:
        return 0.0
    k = max(SOLID_RUN_MIN_PX, int(extent * SOLID_RUN))
    cells = max(1, extent // k)
    if axis == "x":
        strip = mask.crop((b0, lo, b1 + 1, hi + 1))
        bw = b1 - b0 + 1
        solid = (strip.resize((bw, cells), Image.BOX)
                 .point(lambda p: 255 if p >= 240 else 0)
                 .resize((bw, 1), Image.BOX))
    else:
        strip = mask.crop((lo, b0, hi + 1, b1 + 1))
        bw = b1 - b0 + 1
        solid = (strip.resize((cells, bw), Image.BOX)
                 .point(lambda p: 255 if p >= 240 else 0)
                 .resize((1, bw), Image.BOX))
    return max(solid.tobytes()) / 255.0


def _widest_blank(dark: list[float], lo: int, hi: int) -> int:
    """Longest run of blank (≤ GUTTER_FRAC) entries in ``dark[lo:hi+1]``."""
    best = run = 0
    for v in dark[max(0, lo):hi + 1]:
        run = run + 1 if v <= GUTTER_FRAC else 0
        best = max(best, run)
    return best


def _dark_along(mask, axis: str, band: tuple[int, int],
                span: tuple[int, int]) -> float:
    """Mean darkness of the darkest single column (axis='x') or row
    (axis='y') inside ``band`` across ``span``. For the short look just past
    a rule: a line that carries straight on scores ~1, a gutter then a
    neighbour's line scores by how much of the span the gutter leaves."""
    from PIL import Image
    lo, hi = sorted(span)
    b0, b1 = sorted(band)
    if hi < lo or b1 < b0:
        return 0.0
    if axis == "x":
        strip = mask.crop((b0, lo, b1 + 1, hi + 1)).resize((b1 - b0 + 1, 1), Image.BOX)
    else:
        strip = mask.crop((lo, b0, hi + 1, b1 + 1)).resize((1, b1 - b0 + 1), Image.BOX)
    return max(strip.tobytes()) / 255.0


def _extend(profile: tuple[list[float], list[float]], start: int, step: int, *,
            min_gutter: int, max_reach: int, pad: int, max_thick: int,
            closes_frame=None) -> int:
    """Walk from ``start`` in direction ``step`` (±1) and return the index
    (inclusive) where the notice ends on that side.

    The walk reads the profile as runs (see :func:`_tokens`) and decides on
    what lies *beyond* each run, because a ruling line on its own is
    ambiguous — the notice's frame, a table border inside it, or the
    neighbour's frame all look the same:

    * A RULE is the notice's frame when ``closes_frame(px)`` says the
      perpendicular frame sides run from it back to the walk's start (a
      closed rectangle — no table border inside the notice sits on the
      notice's own frame columns), or when beyond it is the page edge,
      another rule, or a blank run ≥ ``min_gutter``. Return its far side +
      ``pad``. A rule with ink close behind it is internal: keep walking.
      Rules separated by less than a rule's thickness merge (double rules).
    * A BLANK run ≥ ``min_gutter`` followed by ink is the gutter between
      unframed notices: return its middle. Followed by a rule that is a
      frame by the test above, include that rule (it is ours, with generous
      padding); followed by a rule with ink behind it, it is the
      neighbour's: return the gutter middle.
    * A BLANK run reaching the page edge is margin: cut ``pad`` into it.
    * BAR and INK are content. At ``max_reach`` return the far edge of the
      last blank run seen, else the reach limit.
    """
    dark, solid = profile
    n = len(dark)
    toks = _tokens(dark, solid, start, step, max_thick=max_thick, max_reach=max_reach)

    def clamp(i: int) -> int:
        return max(0, min(n - 1, i))

    def length(t: tuple[str, int, int]) -> int:
        return abs(t[2] - t[1]) + 1

    def rule_end(k: int) -> tuple[int, int]:
        """Merge double rules starting at token k → (last_token_idx, far_px)."""
        m = k
        while (m + 2 < len(toks) and toks[m + 1][0] == BLANK
               and length(toks[m + 1]) <= max_thick and toks[m + 2][0] == RULE):
            m += 2
        return m, toks[m][2]

    def rule_is_frame(k: int) -> tuple[bool, int, int]:
        """(is_frame, last_token_idx, far_px) for the rule at token k."""
        m, far = rule_end(k)
        if closes_frame is not None and closes_frame(far):
            return True, m, far
        beyond = toks[m + 1] if m + 1 < len(toks) else (EDGE, far, far)
        if beyond[0] in (EDGE, REACH):
            return True, m, far
        if beyond[0] == BLANK:
            after = toks[m + 2][0] if m + 2 < len(toks) else EDGE
            if after in (EDGE, REACH, RULE) or length(beyond) >= min_gutter:
                return True, m, far
            return False, m, far
        return False, m, far           # ink / bar right behind it

    last_blank_end: int | None = None
    k = 0
    while k < len(toks):
        kind, a, b = toks[k]
        if kind == RULE:
            is_frame, m, far = rule_is_frame(k)
            if is_frame:
                return clamp(far + step * pad)
            k = m + 1
        elif kind == BLANK:
            nxt = toks[k + 1][0] if k + 1 < len(toks) else EDGE
            if nxt in (EDGE, REACH):
                return clamp(a + step * min(pad, length(toks[k]) - 1))
            if length(toks[k]) >= min_gutter:
                if nxt == RULE:
                    is_frame, _, far = rule_is_frame(k + 1)
                    if is_frame:
                        return clamp(far + step * pad)
                return a + step * (length(toks[k]) // 2)
            last_blank_end = b
            k += 1
        elif kind in (INK, BAR):
            k += 1
        elif kind == EDGE:
            return 0 if step < 0 else n - 1
        else:                          # REACH
            return last_blank_end if last_blank_end is not None else clamp(a - step)
    return clamp(start)


def snap_to_frame(image_bytes: bytes, bbox: list[float]) -> list[float] | None:
    """Grow ``bbox`` (normalized) outward to the notice's frame or gutters.

    Returns the normalized ``[x0, y0, x1, y1]``, or ``None`` when the image
    can't be read.
    """
    try:
        mask = _dark_mask(image_bytes)
    except Exception:
        return None
    w, h = mask.size
    if w < 8 or h < 8:
        return None
    ax0 = int(bbox[0] * w)
    ay0 = int(bbox[1] * h)
    ax1 = max(ax0 + 1, int(bbox[2] * w))
    ay1 = max(ay0 + 1, int(bbox[3] * h))
    # A gutter between notices must be wider than any blank gap the notice
    # has inside itself, so the page-fraction floor is raised to the widest
    # blank run found inside the anchor on that axis (× GUTTER_OVER_INTERNAL).
    # This is what keeps a 900-px-tall single-notice photo, where 0.8% is
    # seven pixels, from reading its own paragraph spacing as gutters.
    gx = max(2, int(MIN_GUTTER_X * w),
             min(int(MAX_GUTTER * w),
                 int(GUTTER_OVER_INTERNAL * _widest_blank(
                     _profile(mask, "x", ay0, ay1 + 1)[0], ax0, ax1))))
    gy = max(2, int(MIN_GUTTER_Y * h),
             min(int(MAX_GUTTER * h),
                 int(GUTTER_OVER_INTERNAL * _widest_blank(
                     _profile(mask, "y", ax0, ax1 + 1)[0], ay0, ay1))))
    px = max(1, int(PAD * w))
    py = max(1, int(PAD * h))
    rx = int(MAX_REACH * w)
    ry = int(MAX_REACH * h)
    tx = max(2, int(MAX_RULE_THICK * w))
    ty = max(2, int(MAX_RULE_THICK * h))

    # Walks start a little inside the anchor (OCR boxes include the frame
    # line), capped so a thin anchor still has an inside to start from.
    ix = min(max(1, int(START_INSET * w)), max(1, (ax1 - ax0) // 3))
    iy = min(max(1, int(START_INSET * h)), max(1, (ay1 - ay0) // 3))
    sx0, sx1 = ax0 + ix, ax1 - ix
    sy0, sy1 = ay0 + iy, ay1 - iy

    # A rule closes the frame when the perpendicular sides run from it back
    # to the walk start AND stop at it — a corner. A table row inside a
    # notice is also a rectangle (row rules between the table's column
    # lines), but there the column lines carry straight on past the row
    # rule; at the notice's real frame they end. The look past the rule is
    # one gutter long and measures mean darkness, so a line that continues
    # scores ~1 while a neighbour's aligned frame past even a thin gutter
    # scores well under CONTINUES_FRAC.
    def closes_x(y0: int, y1: int, x_start: int, step: int):
        bands = ((y0, y0 + py + ty), (y1 - py - ty, y1))

        def check(px_: int) -> bool:
            sides = max(_solid_across(mask, "y", b, (px_, x_start)) for b in bands)
            if sides < RULE_FRAC:
                return False
            a, b_ = px_ + step * (tx + 1), px_ + step * (tx + gx)
            span = (max(0, min(a, b_)), min(w - 1, max(a, b_)))
            # A corner needs only ONE side to end there: a stray mark past
            # the other corner (a footer glyph, a neighbour's line) must not
            # turn the frame into "internal".
            return min(_dark_along(mask, "y", b, span) for b in bands) < CONTINUES_FRAC
        return check

    def closes_y(x0: int, x1: int, y_start: int, step: int):
        bands = ((x0, x0 + px + tx), (x1 - px - tx, x1))

        def check(py_: int) -> bool:
            sides = max(_solid_across(mask, "x", b, (py_, y_start)) for b in bands)
            if sides < RULE_FRAC:
                return False
            a, b_ = py_ + step * (ty + 1), py_ + step * (ty + gy)
            span = (max(0, min(a, b_)), min(h - 1, max(a, b_)))
            return min(_dark_along(mask, "x", b, span) for b in bands) < CONTINUES_FRAC
        return check

    # Fixed-point iteration. Every pass walks from the ANCHOR's edges — never
    # from the previous pass's result, which already sits in the gutter
    # beyond the frame and would walk on into the neighbour — but measures
    # the profile over the other axis's current extent. That lets an
    # internal rule that spans the anchor (a table column rule under a
    # price cell) fall below RULE_FRAC once the y-pass has grown the box
    # to the notice's full height, so the next x-pass crosses it.
    x0, y0, x1, y1 = ax0, ay0, ax1, ay1
    for _ in range(4):
        prof = _profile(mask, "x", y0, y1 + 1)
        nx0 = _extend(prof, sx0, -1, min_gutter=gx, max_reach=rx, pad=px,
                      max_thick=tx, closes_frame=closes_x(y0, y1, sx0, -1))
        nx1 = _extend(prof, sx1, +1, min_gutter=gx, max_reach=rx, pad=px,
                      max_thick=tx, closes_frame=closes_x(y0, y1, sx1, +1))
        nx0, nx1 = min(nx0, ax0), max(nx1, ax1)
        prof = _profile(mask, "y", nx0, nx1 + 1)
        ny0 = _extend(prof, sy0, -1, min_gutter=gy, max_reach=ry, pad=py,
                      max_thick=ty, closes_frame=closes_y(nx0, nx1, sy0, -1))
        ny1 = _extend(prof, sy1, +1, min_gutter=gy, max_reach=ry, pad=py,
                      max_thick=ty, closes_frame=closes_y(nx0, nx1, sy1, +1))
        ny0, ny1 = min(ny0, ay0), max(ny1, ay1)
        if (nx0, ny0, nx1, ny1) == (x0, y0, x1, y1):
            break
        x0, y0, x1, y1 = nx0, ny0, nx1, ny1

    return [round(x0 / w, 4), round(y0 / h, 4),
            round(min(w, x1 + 1) / w, 4), round(min(h, y1 + 1) / h, 4)]


# ── Entry point ─────────────────────────────────────────────────────────────

def locate_notice(image_bytes: bytes | None, blocks: list[dict],
                  properties: list[dict], *, page: int = 1) -> dict[str, Any] | None:
    """Crop box for the linked property's notice on a full-page scan.

    Returns ``{"bbox", "page", "score", "matched", "anchor_bbox", "blocks",
    "snapped"}`` or ``None`` when nothing on the page can be anchored to the
    property. ``bbox`` is normalized to the full image, ready for
    ``api.review.blocks.set_crop``. ``snapped`` is False when the image was
    unreadable and the box is the raw block-union with padding.
    """
    hints = build_hints(properties)
    if not hints:
        return None
    anchor = anchor_blocks(blocks, hints, page=page)
    if anchor is None or anchor["score"] < MIN_CLUSTER_SCORE:
        return None
    a = anchor["bbox"]
    snapped = snap_to_frame(image_bytes, a) if image_bytes else None
    if snapped is None:
        bbox = [max(0.0, a[0] - PAD), max(0.0, a[1] - PAD),
                min(1.0, a[2] + PAD), min(1.0, a[3] + PAD)]
    else:
        bbox = snapped
    # Only a degenerate box is rejected here. A box that is most of the
    # page is a valid answer ("the notice IS the page") — callers decide
    # whether that is worth a crop (see scripts.auto_crop_notices.MAX_AUTO_AREA).
    if _area(bbox) < MIN_CROP_AREA:
        return None
    return {"bbox": [round(v, 4) for v in bbox], "page": page,
            "score": anchor["score"], "matched": anchor["matched"],
            "anchor_bbox": [round(v, 4) for v in a],
            "blocks": anchor["blocks"], "snapped": snapped is not None}
