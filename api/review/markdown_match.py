"""
api/review/markdown_match.py
----------------------------
Locate a property inside a sales-notice's OCR markdown.

The notice's markdown holds one property block per lot. To find where a
property sits, we search for its reserve_price (Indian-lakh + international
formats) and disambiguate duplicate-price lots by proximity to the
property's borrower name.

Shared by:
  - api/review/queries.py:_sort_properties_by_markdown  (notice-order sort)
  - pipeline/score_markdown.py                          (coverage scoring)
"""
from __future__ import annotations

import re


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
