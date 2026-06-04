"""
api/review/markdown_match.py
----------------------------
Locate a property inside a sales-notice's OCR markdown.

The notice's markdown holds one property block per lot. To find where a
property sits, we search for its reserve_price (Indian-lakh + international
formats) and disambiguate duplicate-price lots by proximity to the
property's borrower name.

This module also exposes the building blocks of the markdown-quality score:
  - ``description_coverage`` — how much of the scraped website description is
    present in the OCR markdown (the primary fidelity probe).
  - ``price_in_markdown`` / ``borrower_in_markdown`` — corroborating presence
    checks for the reserve price and a borrower name.

Shared by:
  - api/review/queries.py:_sort_properties_by_markdown  (notice-order sort)
  - pipeline/score_markdown.py                          (blended quality score)
"""
from __future__ import annotations

import re

from rapidfuzz import fuzz


_BORROWER_PREFIXES = re.compile(
    r"^\s*(?:m/s\.?|mr\.?|mrs\.?|ms\.?|miss\.?|dr\.?|dr\(mr\)|dr\(mrs\)|smt\.?|shri\.?|sri\.?|tmt\.?|thiru\.?)\b",
    re.IGNORECASE,
)


def _format_indian_lakh(n: int) -> str:
    """Format an int in the Indian numbering system, e.g. 3000000 → '30,00,000'."""
    s = str(abs(int(n)))
    if len(s) <= 3:
        return ("-" if n < 0 else "") + s
    head, tail = s[:-3], s[-3:]
    groups = []
    while len(head) > 2:
        groups.append(head[-2:])
        head = head[:-2]
    if head:
        groups.append(head)
    formatted = ",".join(reversed(groups)) + "," + tail
    return ("-" if n < 0 else "") + formatted


def _trim_zero(s: str) -> str:
    """``'32.80' -> '32.8'``; ``'32.00' -> '32'``. Used for Lakhs/Crores."""
    if "." not in s:
        return s
    return s.rstrip("0").rstrip(".")


def _price_patterns(price) -> list[tuple[str, bool]]:
    """Candidate strings a reserve price might appear as in the notice markdown.

    Returns ``[(pattern, case_insensitive), ...]``. The case-sensitive
    patterns are the digit-only / digit-comma / digit-dot forms — flipping
    those to case-insensitive would not change behaviour but would slow
    matching slightly. The Lakhs/Crores suffix forms are case-insensitive
    so we match ``32.80 Lakhs``/``32.80 lakhs``/``32.80LAKHS`` uniformly.
    """
    if price is None:
        return []
    try:
        n = int(round(float(price)))
    except (TypeError, ValueError):
        return []
    if n <= 0:
        return []
    raw = str(n)
    intl = f"{n:,}"
    indian = _format_indian_lakh(n)
    seen: set[str] = set()
    out: list[tuple[str, bool]] = []
    # Digit forms: indian-lakh (``9,24,00,000``), international (``92,400,000``),
    # raw (``92400000``), and dot-separated twins of the comma forms — OCR
    # frequently renders thousand-separator commas as dots.
    for p in (indian, intl, raw):
        if not p or p in seen:
            continue
        seen.add(p)
        out.append((p, False))
        if "," in p:
            dotted = p.replace(",", ".")
            if dotted not in seen:
                seen.add(dotted)
                out.append((dotted, False))
    # Lakhs / Crores suffix forms (``Rs.32.80 Lakhs``, ``18.00Lakhs``,
    # ``Rs.70.00 Lakhs``). Common in TN sale notices that summarise the
    # reserve price in words instead of the full digit string.
    if n >= 100_000:
        lakh = n / 100_000.0
        for num_str in (f"{lakh:.2f}", _trim_zero(f"{lakh:.2f}"), f"{lakh:g}"):
            for suffix in (" Lakhs", " Lakh", "Lakhs", "Lakh"):
                pat = num_str + suffix
                if pat not in seen:
                    seen.add(pat)
                    out.append((pat, True))
    if n >= 10_000_000:
        cr = n / 10_000_000.0
        for num_str in (f"{cr:.2f}", _trim_zero(f"{cr:.2f}"), f"{cr:g}"):
            for suffix in (" Crores", " Crore", "Crores", "Crore"):
                pat = num_str + suffix
                if pat not in seen:
                    seen.add(pat)
                    out.append((pat, True))
    return out


def _borrower_token(name: str | None) -> str | None:
    """Strip honorific prefixes and return the first remaining word ≥ 4 chars.

    'Mr Dineshkumar M'              → 'Dineshkumar'
    'Dr(Mr) Vedhasalam U'           → 'Vedhasalam'
    'M/s. Subbukshmi Enterprises'   → 'Subbukshmi'
    'Mr A'                          → None  (no word ≥ 4 chars)
    """
    if not name:
        return None
    stripped = _BORROWER_PREFIXES.sub("", str(name)).strip()
    for tok in re.split(r"[\s,./()&-]+", stripped):
        if len(tok) >= 4:
            return tok
    return None


def _all_offsets(haystack: str, needle: str, case_insensitive: bool = False) -> list[int]:
    """Every (non-overlapping) offset of needle inside haystack."""
    if not haystack or not needle:
        return []
    hay = haystack.lower() if case_insensitive else haystack
    pin = needle.lower() if case_insensitive else needle
    offsets: list[int] = []
    start = 0
    while True:
        i = hay.find(pin, start)
        if i < 0:
            break
        offsets.append(i)
        start = i + len(pin)
    return offsets


def property_offset_in_notice(prop: dict, markdown: str) -> int | None:
    """The notice-order offset for one property.

    Returns the markdown character index of the reserve-price occurrence that
    sits closest to one of the property's borrower-name mentions. Falls back
    to the first reserve-price occurrence when no borrower is mentioned, and
    None when the price isn't in the markdown at all (or the property has no
    usable reserve_price).
    """
    if not markdown:
        return None
    price_offsets: list[int] = []
    for pat, case_insensitive in _price_patterns(prop.get("reserve_price")):
        price_offsets.extend(_all_offsets(markdown, pat, case_insensitive))
    if not price_offsets:
        return None

    borrower_offsets: list[int] = []
    for b in prop.get("borrowers") or []:
        tok = _borrower_token(b)
        if not tok:
            continue
        borrower_offsets.extend(_all_offsets(markdown, tok, case_insensitive=True))

    if not borrower_offsets:
        return min(price_offsets)

    def dist_to_borrower(p: int) -> int:
        return min(abs(p - b) for b in borrower_offsets)

    return min(price_offsets, key=lambda p: (dist_to_borrower(p), p))


# ── Website-description coverage ────────────────────────────────────────────
# The scraped website description is often a near-verbatim copy of the
# property paragraph inside the notice. Checking how much of it survives in the
# OCR markdown is a far stronger fidelity probe than the lone reserve price: a
# whole paragraph can't match by coincidence the way a single number can.

_DESC_LABEL = re.compile(r"^\s*property\s+description\s*[:\-]?\s*", re.IGNORECASE)
_HYPHEN_JOIN = re.compile(r"\s*-\s*")
_WS = re.compile(r"\s+")

# The website description has three location fields glued onto its end. They
# come from the graph nodes (State / City / Area), not from the scraped prose,
# and always start at "Province/State", e.g.
#   "…land and buildingProvince/State :Tamil NaduCity/Town :RanipetArea/Town :…"
# These labels contain a slash and never appear in a genuine property
# description, so cutting at the first one is safe.
_BLEED_LABELS = ["Province/State", "City/Town", "Area/Town"]
_FIELD_BLEED = re.compile(
    r"(?:" + "|".join(re.escape(lbl) for lbl in _BLEED_LABELS) + r")"
)


def strip_field_bleed(text: str | None) -> str:
    """Cut the glued-on location fields (Province/State, City/Town, Area/Town)
    off the end of a website description. Returns the text unchanged when none
    is present. Safe on prose — these slashed field names don't occur in a real
    property description ("Registration District of Ranipet" is untouched).
    """
    if not text:
        return text or ""
    m = _FIELD_BLEED.search(text)
    if not m:
        return text
    return text[: m.start()].rstrip(" ,;:-\n\t")


def _normalize_for_match(text: str) -> str:
    """Fold OCR / scraper noise so fuzzy matching compares like with like.

    - drop a leading ``Property description:`` label
    - join hyphenated and line-broken words (``Sub- Division`` → ``subdivision``,
      ``An-chaneyar`` → ``anchaneyar``) so they line up with the un-hyphenated
      OCR rendering
    - lowercase and collapse every whitespace run to a single space
    """
    if not text:
        return ""
    t = _DESC_LABEL.sub("", text).lower()
    t = _HYPHEN_JOIN.sub("", t)
    return _WS.sub(" ", t).strip()


def description_coverage(
    website_desc: str | None, markdown: str | None
) -> tuple[float, tuple[int, int] | None]:
    """How much of the website description appears in the markdown.

    Returns ``(score, span)`` where ``score`` is rapidfuzz's 0–100
    ``partial_ratio`` of the (normalized) website description against the
    (normalized) markdown, and ``span`` is the ``(start, end)`` of the
    best-matching window **in the normalized markdown** — useful for a future
    highlight, but not a raw-markdown offset. ``span`` is ``None`` when either
    side is empty.
    """
    # Strip the scraper's trailing field bleed from the probe only — never from
    # the markdown haystack, which legitimately contains "Reserve Price :" etc.
    nd = _normalize_for_match(strip_field_bleed(website_desc))
    nm = _normalize_for_match(markdown or "")
    if not nd or not nm:
        return 0.0, None
    al = fuzz.partial_ratio_alignment(nd, nm)
    if al is None:
        return 0.0, None
    return round(al.score, 1), (al.dest_start, al.dest_end)


def price_in_markdown(price, markdown: str | None) -> bool:
    """True if the reserve price appears in the markdown (any known format)."""
    if not markdown:
        return False
    for pat, case_insensitive in _price_patterns(price):
        if _all_offsets(markdown, pat, case_insensitive):
            return True
    return False


def borrower_in_markdown(borrowers, markdown: str | None) -> bool:
    """True if any borrower's distinguishing name token appears in the markdown."""
    if not markdown:
        return False
    for b in borrowers or []:
        tok = _borrower_token(b)
        if tok and _all_offsets(markdown, tok, case_insensitive=True):
            return True
    return False


# ── Highlight spans ─────────────────────────────────────────────────────────
# For the review UI: locate where a property's description sits in the OCR
# markdown so the panel can highlight it. Unlike description_coverage (which
# normalizes both sides and so loses raw offsets), this matches on a
# lowercase-only copy of the markdown — lowercasing doesn't change length, so
# the alignment offsets map 1:1 back to the raw markdown characters.

# Below this match quality we don't trust the location enough to highlight it.
_HIGHLIGHT_MIN_SCORE = 75.0


def _snap_to_word_boundaries(text: str, start: int, end: int) -> tuple[int, int]:
    """Grow a span outward to the nearest whitespace on both sides, but never
    across an HTML tag boundary.

    rapidfuzz's partial_ratio alignment can trim a few characters at the edges
    (e.g. a leading "All" the probe's item-number prefix shifted off, or a
    trailing "1.2025)" cut mid-token), leaving a ragged highlight. Extending to
    whitespace boundaries completes the partial word/token at each end without
    pulling in unrelated text — the gap is only ever a fragment of one token.

    MinerU emits tables as raw HTML (``...Tamilnadu, 625602)</td><td>All the
    Piece...``) with no whitespace between cells, so plain "nearest whitespace"
    would walk the span across ``</td><td>`` into the neighbouring cell and
    latch onto the previous cell's trailing token (a pincode or date). When the
    UI later wraps that span in a ``<mark>`` the browser collapses the cross-cell
    tag down to just the previous cell's fragment — so the highlight lands on
    the pincode instead of the description. Treat ``<`` and ``>`` as hard stops
    so the span can never cross a tag.
    """
    n = len(text)
    while start > 0 and not text[start - 1].isspace() and text[start - 1] not in "<>":
        start -= 1
    while end < n and not text[end].isspace() and text[end] not in "<>":
        end += 1
    return start, end


def match_span(
    website_desc: str | None, markdown: str | None, with_score: bool = False
):
    """Raw character offsets of the markdown window that best matches the
    (bleed-stripped) website description, or ``None`` when there's no confident
    match. Offsets index directly into ``markdown``.

    Returns ``(start, end)`` by default; with ``with_score=True`` returns
    ``(score, start, end)`` so callers can pick the best document among several.
    """
    if not website_desc or not markdown:
        return None
    probe = strip_field_bleed(website_desc).strip().lower()
    if not probe:
        return None
    al = fuzz.partial_ratio_alignment(probe, markdown.lower())
    if al is None or al.score < _HIGHLIGHT_MIN_SCORE:
        return None
    start, end = al.dest_start, al.dest_end
    if end <= start:
        return None
    start, end = _snap_to_word_boundaries(markdown, start, end)
    return (al.score, start, end) if with_score else (start, end)

