"""
pipeline/ink_fingerprint.py
---------------------------
Recognise the same sale notice filed under a different name, by fingerprinting
where its ink sits rather than trusting its bytes or its file name.

Why this exists
~~~~~~~~~~~~~~~
Portals name an upload with the millisecond it was uploaded, so one bank
publishing one notice against six lots produces six file names:
``KARNTK17819383495370.jpg`` … ``KARNTK17819391325440.jpg``, 1.6 seconds apart
and byte-for-byte identical. ``scripts/dedupe_documents.py`` groups on
``filename``, so it cannot see them, and every downstream count — notices
ingested, OCR spend, lots on a page — counts one page six times.

The two measurements
~~~~~~~~~~~~~~~~~~~~
1. :func:`content_hash` — SHA-256 of the bytes. Exact re-uploads (the case
   above, and the common one: 23 groups covering 29 redundant copies in a
   1,590-notice corpus) collide here with no threshold to argue about and no
   false positives.
2. :func:`ink_signature` — a 1024-bit hash of where the page's ink falls. The
   same notice re-scanned, re-cropped or served at another resolution has
   different bytes, so only this level sees it.

The ink is prepared exactly as ``pipeline/ink_coverage.py`` prepares it —
binarize at ``DARK_THRESHOLD``, strip the ruling lines, strip the solid
graphics — and only then resampled onto a fixed ``GRID`` × ``GRID`` mesh. Two
reasons for reusing that preparation rather than hashing the raw page:

* **Scale independence.** Coverage tiles in source pixels (``TILE_PX``) because
  it compares against bboxes on that page. A fingerprint compares across pages,
  so the mesh is a fixed cell *count* and the cell size floats with the image —
  the same notice at 750px and at 1950px lands on the same mesh.
* **Discrimination.** Stripping rules and blobs removes precisely the ink two
  *different* notices from the same bank share: the same table skeleton, the
  same logo, the same reversed-out banner. What is left is the text layout,
  which is what actually differs between two notices.

What it can and cannot tell apart
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Measured over the corpus (``scripts/find_duplicate_notices.py --calibrate``),
ink distance is a **high-precision, low-recall** test, and the report is built
around that shape:

* Under ``SAME_PAGE_MAX_DISTANCE`` it is almost always right. Ten pairs scored
  there; nine were confirmed the same page — including two whose stored OCR
  text agrees only 0.70, which no text comparison would have caught.
* It cannot separate a duplicate from a **re-auction**. The tenth pair was the
  same lots re-advertised months later: same bank template, same table, only the
  dates changed. Nothing measured on page shape can tell those apart, which is
  why callers here surface candidates to confirm and never merge on this
  evidence alone (``scripts/link_reauctions.py`` owns the re-auction case).
* Above that distance it goes quiet quickly. Of 48 pairs whose stored markdown
  agrees ≥0.95, the median ink distance is 0.22 — the same page re-typeset by
  another newspaper is a genuinely different page. Catching those is the text
  pass in ``scripts/find_duplicate_notices.py``, not this module.
* Detail has to survive for the mesh to match. Re-encoding and upscaling leave
  a signature where it was, but halving a notice's resolution blurs its strokes
  under ``DARK_THRESHOLD``, and what is left is honestly a different amount of
  ink: measured on a corpus notice, the same page at 0.75× lands at 0.15 and at
  0.5× at 0.30, both outside the threshold. So a badly shrunken copy is a miss
  here, not a false negative to tune away.

Scope: single-page rasters, like coverage — a PDF must be rendered per page by
the caller. And this answers "is this the same page?", never "is this the same
property?": two lots legitimately share one multi-property notice file, which is
one document to store once and link twice, not a duplicate auction.
"""
from __future__ import annotations

import hashlib
import io

from pipeline.ink_coverage import (
    BLOB_MIN_FRAC,
    BLOB_MIN_PX,
    DARK_THRESHOLD,
    INK_TILE_MIN,
    _solid_blobs,
    _strip_rules,
)


# Mesh edge. 32×32 = 1024 bits ≈ 256 hex chars, small enough to store on every
# Document node and to compare the whole corpus pairwise in memory. Each cell is
# ~1/32 of the page — a few text lines tall on a notice — so a cell tracks where
# a paragraph or a column sits, not which glyph is in it. That is the right
# altitude: glyph-level detail would make a re-scan of the same page look
# different, and anything coarser would make two notices of the same shape look
# the same. Both were measured: 16×16 and 48×48 each separated the corpus's
# known duplicates worse than 32×32 did.
GRID = 32
# Total mesh darkness (0..GRID²) below which the page is blank or near-blank — a
# scan of an empty page, a photo of a wall. Its bits would be noise, and two such
# pages would match each other, so they get no signature at all. Same shape as
# ``ink_coverage.MIN_TOTAL_INK``, scaled to this mesh.
MIN_SIGNATURE_INK = INK_TILE_MIN * GRID * GRID
# Normalized Hamming distance at or below which two pages are the same page.
#
# Calibrated over 1,561 distinct notices — 1.2M pairs — against two independent
# readings of truth: the pair judged by eye, and the Jaccard overlap of the two
# documents' stored markdown. Ten pairs scored at or under 0.12 and nine were the
# same page; unrelated pairs sit far above (median nearest-neighbour distance
# 0.29), so there is a wide empty band above this line rather than a crowd.
#
# The tenth is the limit of the method, not a tuning failure: same lots, same
# bank template, re-advertised with new dates. Moving the line down to exclude it
# would also drop three confirmed duplicates sitting just under it, and would not
# make the measure able to see a date. So the line stays where precision is, and
# the caller confirms.
SAME_PAGE_MAX_DISTANCE = 0.12


def content_hash(image_bytes: bytes | None) -> str | None:
    """SHA-256 of the raw bytes, or ``None`` when there are none.

    The first pass, and the only one needing no threshold: a portal that
    re-uploads one file under six names hands us six identical byte strings.
    """
    if not image_bytes:
        return None
    return hashlib.sha256(image_bytes).hexdigest()


def _ink_mesh(image_bytes: bytes) -> tuple[list[float], int, int]:
    """Per-cell dark fraction on the fixed ``GRID`` mesh, plus the page size.

    Same preparation as ``ink_coverage._tile_ink`` — binarize, strip rules, strip
    solid graphics — resampled onto a fixed cell count instead of a fixed cell
    size. One BOX resample gives exact per-cell means whatever the source
    resolution, which is what makes the mesh scale-independent.
    """
    from PIL import Image, ImageChops
    with Image.open(io.BytesIO(image_bytes)) as im:
        g = im.convert("L")
        w, h = g.width, g.height
        mask = _strip_rules(g.point(lambda p: 255 if p < DARK_THRESHOLD else 0),
                            w, h)
        mask = ImageChops.subtract(
            mask, _solid_blobs(mask, max(BLOB_MIN_PX,
                                         int(min(w, h) * BLOB_MIN_FRAC))))
        small = mask.resize((GRID, GRID), Image.BOX)
        return [v / 255.0 for v in small.getdata()], w, h


def _bits(mesh: list[float]) -> str:
    """The mesh reduced to one bit per cell: darker than the page's median.

    A rank threshold rather than a fixed one is what survives re-encoding. A
    lighter scan, a heavier one, or a lower JPEG quality moves every cell in the
    same direction, and the median moves with them; a fixed cutoff would not.
    When the median is zero — a sparse page where most cells are blank — the
    split falls back to "any ink at all", which is the same comparison the
    mesh's own noise floor makes.
    """
    ordered = sorted(mesh)
    cut = max(ordered[len(ordered) // 2], 0.0)
    n = 0
    for v in mesh:
        n = (n << 1) | (1 if v > cut else 0)
    return f"{n:0{GRID * GRID // 4}x}"


def ink_signature(image_bytes: bytes | None) -> dict:
    """Fingerprint one page's ink.

    Returns ``{"signature": str|None, "aspect": float|None, "ink": float|None,
    "skipped": str|None}``:

        signature  GRID²-bit hash of the ink mesh, hex
        aspect     source width / height — reported, never gated on, because a
                   re-crop of the same notice changes it and a reviewer wants to
                   see that rather than have the pair silently dropped
        ink        total mesh darkness, 0..GRID²
        skipped    why there is no signature, when there is none

    ``signature`` is ``None`` — unscorable, never anyone's duplicate — for a
    missing, corrupt or near-blank page. Same convention as
    ``ink_coverage.score_ink_coverage``: the absence of a reading is not a
    reading.
    """
    out: dict = {"signature": None, "aspect": None, "ink": None, "skipped": None}
    if not image_bytes:
        out["skipped"] = "no-image"
        return out
    try:
        mesh, w, h = _ink_mesh(image_bytes)
    except Exception as e:                       # unreadable/corrupt image
        out["skipped"] = f"unreadable-image: {type(e).__name__}"
        return out
    total = sum(mesh)
    out["aspect"] = round(w / h, 4) if h else None
    out["ink"] = round(total, 4)
    if total < MIN_SIGNATURE_INK:
        out["skipped"] = "too-little-ink"
        return out
    out["signature"] = _bits(mesh)
    return out


def signature_distance(a: str | None, b: str | None) -> float | None:
    """Normalized Hamming distance between two signatures, 0..1.

    ``None`` when either side is missing or malformed — the absence of a
    comparison, which a caller must not read as "far apart".
    """
    if not a or not b or len(a) != len(b):
        return None
    try:
        x = int(a, 16) ^ int(b, 16)
    except ValueError:
        return None
    return x.bit_count() / (len(a) * 4)


def is_same_page(a: str | None, b: str | None) -> bool:
    """Do these two signatures describe the same page?

    False whenever the distance is unavailable: an unscorable page is never
    claimed as anyone's duplicate. True is "same page, confirm it" — see the
    module docstring on re-auctions — never "safe to delete".
    """
    d = signature_distance(a, b)
    return d is not None and d <= SAME_PAGE_MAX_DISTANCE
