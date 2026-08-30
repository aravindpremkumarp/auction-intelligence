"""Does the price we extracted agree with the price the portal shows?

The portal listing and the sale notice are scraped independently, so the two
reserve prices are two witnesses to the same fact. Nothing compared them.

On a MULTI-lot notice the comparison already happens as a side effect of
matching: `match_lots_to_listings` tries reserve price first, so a listing
that matched on `borrower` or `emd` is telling you the price did NOT line up.
That signal was computed and thrown away.

On a SINGLE-lot notice it never happens at all — the matcher short-circuits
with reason 'single' ("there is only one lot, so it must be this one") and
never looks at the money. That is 961 listings whose two prices sit side by
side and have never been compared.

Measured over those 961: 885 have a price on both sides, 840 agree exactly,
5 agree within 1%, and 40 disagree. Of the 40, **21 are off by exactly 10x or
100x** — a dropped zero, or lakh read as crore. Those are not judgement calls,
which is why they are graded apart from the rest.

Pure functions, no database: the graph work lives in apply_extractions.
"""
from __future__ import annotations

# Matching the ±1% the matcher itself allows, so the two never disagree about
# whether a pair of prices is "the same price".
TOLERANCE_PCT = 1.0

# A slip of exactly one or two decimal places. Real price differences do not
# land on 10x; a dropped zero does.
#
# The 5% band is not slack for its own sake: the two sources round to
# different places, so a real slip lands near the power of ten rather than on
# it. Listing 750895 reads 21,83,000 against 2,13,000 — a ratio of 10.25,
# which a tighter band would grade an ordinary disagreement and hide among the
# ones needing human judgement. Nothing plausible sits between 9.5x and 10.5x
# for one property priced by two sources.
_MAGNITUDES = (10.0, 100.0, 1000.0)
_MAGNITUDE_TOL = 0.05

# The matcher's reasons that mean "the reserve prices agreed". Everything else
# in the cascade is a FALLBACK — it only ran because price failed to separate
# the lots, so the extracted price differs from the portal's.
PRICE_REASONS = frozenset({"exact", "tolerance"})


def _ratio(a: float, b: float) -> float:
    lo, hi = min(a, b), max(a, b)
    return hi / lo if lo else 0.0


def is_magnitude_slip(portal: float, notice: float) -> bool:
    """True when the two prices differ by a clean power of ten.

    A 10x gap is the signature of a dropped zero or a lakh/crore mix-up, not
    of two people disagreeing about what a property is worth.
    """
    if not portal or not notice:
        return False
    r = _ratio(float(portal), float(notice))
    return any(abs(r - m) <= m * _MAGNITUDE_TOL for m in _MAGNITUDES)


def compare_prices(portal, notice) -> tuple[str, float | None]:
    """Grade one pair of reserve prices.

    Returns (verdict, ratio). Verdicts:

      agree            equal, or within TOLERANCE_PCT
      magnitude_slip   off by a clean 10x/100x/1000x — a wrong price, no doubt
      disagree         off by something else — a human should look
      unknown          one side has no price, so there is nothing to compare

    A zero is `unknown`, not a disagreement: the portal writes 0 for "price
    not published", and calling that a 100%-off price would bury the real
    ones under noise.
    """
    p = None if portal in (None, "") else float(portal)
    n = None if notice in (None, "") else float(notice)
    if not p or not n:
        return "unknown", None
    if p == n:
        return "agree", 1.0
    if abs(p - n) <= max(abs(p), abs(n)) * TOLERANCE_PCT / 100.0:
        return "agree", _ratio(p, n)
    if is_magnitude_slip(p, n):
        return "magnitude_slip", _ratio(p, n)
    return "disagree", _ratio(p, n)


def severity_of(verdict: str) -> str | None:
    """How loudly to complain. None means "not a finding"."""
    return {"magnitude_slip": "critical", "disagree": "med"}.get(verdict)


def check_match(listing: dict, lot: dict, reason: str) -> dict | None:
    """One finding for one matched (listing, lot) pair, or None if it is fine.

    `reason` is the matcher's own reason string. On a multi-lot notice a
    reason outside PRICE_REASONS already proves the prices differ, so the
    pair is reported even when one side turns out to have no number to show
    for it — the fallback IS the evidence.
    """
    portal, notice = listing.get("price"), lot.get("reserve")
    verdict, ratio = compare_prices(portal, notice)

    if verdict == "unknown" and reason not in PRICE_REASONS and reason != "single":
        verdict = "disagree"

    sev = severity_of(verdict)
    if sev is None:
        return None
    return {
        "aid": listing.get("aid"),
        "lot_index": lot.get("lot_index"),
        "verdict": verdict,
        "severity": sev,
        "portal_price": portal,
        "notice_price": notice,
        "ratio": round(ratio, 2) if ratio else None,
        "matched_by": reason,
    }


def check_document(matches: list[tuple[dict, dict, str]]) -> list[dict]:
    """Findings for every matched pair on one notice, worst first."""
    out = [f for f in (check_match(li, lo, r) for li, lo, r in matches) if f]
    out.sort(key=lambda f: (f["severity"] != "critical", str(f["aid"])))
    return out
