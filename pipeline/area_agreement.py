"""Does the area a listing shows agree with the area its lot's notice states?

Shaped after :mod:`pipeline.price_agreement` — same verdicts, same severity
grammar, same rebuilt-each-pass storage — but the two witnesses are NOT the
same pair. The portal never published an area: the scraper captures no such
field, and every `AuctionProperty.total_area` in the graph was written by the
LEGACY enrichment path (`verify_and_enrich.flatten_enrichment`), the earlier
vision-LLM read of the sale notice. The grounded LangExtract path reads the
same notice again and puts its extent on the matched lot. So this module
compares two OCR generations of one document — and, more importantly, the two
values agent3 serves side by side today: `total_area` in the listing block and
the lot's measurement in the property block. When they disagree, the product
contradicts itself on screen. Measured before this module existed: of 2,178
comparable pairs, 296 disagree beyond 10% and 18 are clean 10x/100x slips.

Verdicts are recorded, never auto-applied (the 2026-08-31 plan decision:
queue first, decide from the scorecard).

Pure functions, no I/O — the graph work lives in apply_extractions.
"""
from __future__ import annotations

import re

from pipeline.measures import UNIT_ALIASES, parse_area, parse_quantity

# Matching price_agreement's bands exactly, so "agree" means one thing
# corpus-wide: within 1%, else a clean power-of-ten gap, else a disagreement.
TOLERANCE_PCT = 1.0
_MAGNITUDES = (10.0, 100.0, 1000.0)
_MAGNITUDE_TOL = 0.05

# Every alias that canonicalises to square feet, longest first — the same
# collision discipline measures.py documents ("square feet" contains "are").
_SQFT_ALIASES = sorted(
    (a for a, u in UNIT_ALIASES.items() if u == "sq_ft"), key=len, reverse=True)
# The numeric phrase admits mixed and unicode fractions — "7527 1/2 sq.ft"
# means 7527.5, and a plain \d+ grab reads the "2" alone (a live listing,
# 748228, turned into a 3763x false disagreement that way). The captured
# phrase is evaluated by measures.parse_quantity, which owns fraction grammar.
_NUM = r"(?:\d[\d,]*(?:\.\d+)?(?:\s+\d+\s*/\s*\d+|\s*[½¼¾⅓⅔⅛⅜⅝⅞])?|\d+\s*/\s*\d+)"
_SQFT_RE = re.compile(
    r"(" + _NUM + r")\s*(?:" + "|".join(re.escape(a) for a in _SQFT_ALIASES)
    + r")(?![a-z])",
    re.IGNORECASE,
)


def _norm(s: str) -> str:
    s = re.sub(r"\s*\.\s*", ".", str(s))
    return re.sub(r"\s{2,}", " ", s)


def stated_sqft(raw) -> tuple[float | None, str]:
    """The square-feet figure a free-text area string claims, plus how.

    Enrichment area strings mix a land unit with its own conversion —
    "0.10 1/4 acre (4625 Sq ft)" — and the bracketed sq.ft is the writer's own
    bottom line, so an explicit sq.ft figure anywhere in the string outranks
    converting the land unit ourselves (whose "0.10 1/4" also defeats any
    fraction grammar). Only when no sq.ft is stated does the string fall
    through to `measures.parse_area` for a unit conversion.

    Returns (sqft, how):
      how = 'stated'      an explicit sq.ft figure (all of them equal, if several)
            'converted'   no sq.ft stated; a land unit converted by parse_area
            'multi_value' several DIFFERENT sq.ft figures — "7200 Sq. ft (each
                          plot 1800 Sq. ft.)" describes more than one thing,
                          and picking either would be the guess this whole
                          phase exists to stop
            'unparsed'    no number+unit this code understands
    """
    if raw in (None, ""):
        return None, "unparsed"
    text = _norm(raw)
    stated = []
    for m in _SQFT_RE.finditer(text):
        v = parse_quantity(m.group(1))
        if v is not None:
            stated.append(v)
    if stated:
        if len({round(v, 2) for v in stated}) == 1:
            return stated[0], "stated"
        return None, "multi_value"
    _value, _unit, sqft = parse_area(text)
    if sqft is not None:
        return sqft, "converted"
    return None, "unparsed"


def _ratio(a: float, b: float) -> float:
    lo, hi = min(a, b), max(a, b)
    return hi / lo if lo else 0.0


def is_magnitude_slip(listing: float, notice: float) -> bool:
    """A clean power-of-ten gap — one side dropped or grew a zero, the
    signature of a misread figure rather than of two different properties."""
    if not listing or not notice:
        return False
    r = _ratio(float(listing), float(notice))
    return any(abs(r - m) <= m * _MAGNITUDE_TOL for m in _MAGNITUDES)


def compare_areas(listing_sqft, notice_sqft) -> tuple[str, float | None]:
    """Grade one pair of areas, both already in square feet.

    Verdicts mirror compare_prices:

      agree            within TOLERANCE_PCT — unit-conversion rounding lands
                       well inside this
      magnitude_slip   a clean 10x/100x/1000x gap — a misread, no doubt
      disagree         anything else — a human should look
      unknown          one side has no figure, so nothing to compare
    """
    lv = None if listing_sqft in (None, "") else float(listing_sqft)
    nv = None if notice_sqft in (None, "") else float(notice_sqft)
    if not lv or not nv:
        return "unknown", None
    if abs(lv - nv) <= max(lv, nv) * TOLERANCE_PCT / 100.0:
        return "agree", _ratio(lv, nv)
    if is_magnitude_slip(lv, nv):
        return "magnitude_slip", _ratio(lv, nv)
    return "disagree", _ratio(lv, nv)


def severity_of(verdict: str) -> str | None:
    """How loudly to complain. None means "not a finding"."""
    return {"magnitude_slip": "critical", "disagree": "med"}.get(verdict)


def check_match(listing: dict, lot: dict,
                headline_sqft: float | None = None) -> dict | None:
    """One finding for one CONFIRMED (listing, lot) pair, or None if fine.

    Callers gate on `sole_claimants` — on a contested lot the notice side is
    a guess, and a finding built on a guess would send a reviewer to
    reconcile two numbers that may describe different properties.

    ``headline_sqft`` is the lot's headline `Measurement.sqft_norm` from the
    graph — the figure agent3's property block serves — and is preferred as
    the notice side. It matters: `total_area` on the listing is itself
    overwritten from this extraction's field text on every pass, so comparing
    against that text alone compares a value with itself and finds nothing
    (measured: 0 findings corpus-wide). The headline measurement is selected
    and parsed separately by promote_extractions, which is exactly where the
    two served numbers drift apart. Only when the lot has no headline yet
    (promotion has not run) does the field text stand in.
    """
    listing_raw = listing.get("area_raw")
    notice_raw = (lot.get("fields") or {}).get("total_area")
    l_sqft, l_how = stated_sqft(listing_raw)
    if headline_sqft is not None:
        n_sqft, n_how = float(headline_sqft), "headline"
    else:
        n_sqft, n_how = stated_sqft(notice_raw)
    verdict, ratio = compare_areas(l_sqft, n_sqft)
    sev = severity_of(verdict)
    if sev is None:
        return None
    return {
        "aid": listing.get("aid"),
        "lot_index": lot.get("lot_index"),
        "verdict": verdict,
        "severity": sev,
        "listing_area": listing_raw,
        "notice_area": notice_raw,
        "listing_sqft": l_sqft,
        "notice_sqft": n_sqft,
        "parse_how": f"{l_how}/{n_how}",
        "ratio": round(ratio, 2) if ratio else None,
    }
