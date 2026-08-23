"""
api/agent3/benchmark_price.py
-----------------------------
"Is this priced right?" — reserve price per square foot against comparables.

**Read the refusals before the numbers.** This tool declines more often than
it answers, and each refusal is a real limit of the data rather than
conservatism for its own sake:

1. **No sold prices exist.** `Auction.outcome` is only ever "unsold". Every
   figure here is *reserve* against *reserve* — a bank's floor compared with
   other banks' floors. It is NOT market value, and the tool says so in
   every response so the caveat cannot be dropped in summarising.

2. **Only single-lot notices can be priced.** Reserve price is on the
   listing; extent is on the lot. When a notice covers several lots there is
   no way to say which lot the price refers to, so the division is
   meaningless. Measured: of 2,750 listings with a reserve price, only
   **832 sit on a single-lot notice with a usable extent** — so this tool
   refuses roughly 70% of listings, by design.

3. **Thin rings are refused, not averaged.** A percentile off three
   comparables is noise wearing a number's clothes.

**Area rings are usually too thin** and that is expected: only 36 of 417
areas have 5+ priceable listings, against 35 of 48 cities and 30 of 38
districts. The walk will normally land on city.
"""
from __future__ import annotations

from api.agent3.common import ToolInputError, tool
from api.neo4j_client import run_read_query

#: A property is not smaller than this. The 1-sqft floor used elsewhere lets
#: parse errors through into a DIVISION, where they explode: extents of 1.2
#: and 1.6 sqft produced ₹1.4M and ₹8.4M per sqft. Raising the floor to 100
#: for pricing drops the corpus maximum from ₹8,387,097 to ₹229,358 per sqft
#: and costs only 30 of 832 listings.
PRICING_SQFT_FLOOR = 100.0
PRICING_SQFT_CEIL = 500_000.0

#: Plausible band for reserve ₹/sqft in this corpus (median ₹2,523).
#: Agricultural land legitimately sits in the low hundreds, prime Chennai in
#: the tens of thousands; outside this, the input was wrong, not the market.
PSF_FLOOR = 10.0
PSF_CEIL = 100_000.0

#: Below this, a ring is reported as too thin rather than summarised.
MIN_COMPARABLES = 5

_SUBJECT = """
MATCH (a:AuctionProperty {auction_id: $id})
OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)
WITH a, count(DISTINCT l) AS lot_count
CALL (a) {
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l2:Lot)
        -[e:HAS_EXTENT]->(m:Measurement)
  WHERE e.is_headline AND m.sqft_norm >= $sqft_floor AND m.sqft_norm <= $sqft_ceil
  RETURN max(m.sqft_norm) AS sqft
}
OPTIONAL MATCH (a)-[:LOCATED_IN_AREA]->(ar:Area)
OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
OPTIONAL MATCH (a)-[:LOCATED_IN_DISTRICT]->(d:District)
RETURN a.auction_id AS auction_id, a.reserve_price_num AS reserve_price,
       lot_count, sqft, ar.name AS area, c.name AS city, d.name AS district,
       [(a)-[:HAS_PROPERTY_TYPE]->(pt:PropertyType) | pt.name] AS property_types
"""

#: One ring. `$match` is substituted from _RINGS, never from user input.
_RING = """
MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)
WITH a, count(DISTINCT l) AS lots
WHERE lots = 1 AND a.reserve_price_num IS NOT NULL AND a.auction_id <> $id
{match}
CALL (a) {{
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l2:Lot)
        -[e:HAS_EXTENT]->(m:Measurement)
  WHERE e.is_headline AND m.sqft_norm >= $sqft_floor AND m.sqft_norm <= $sqft_ceil
  RETURN max(m.sqft_norm) AS sqft
}}
WITH a, a.reserve_price_num / sqft AS psf
WHERE psf >= $psf_floor AND psf <= $psf_ceil
RETURN count(*) AS n,
       round(percentileCont(psf, 0.25), 0) AS p25,
       round(percentileCont(psf, 0.5), 0) AS median,
       round(percentileCont(psf, 0.75), 0) AS p75,
       sum(CASE WHEN psf < $subject_psf THEN 1 ELSE 0 END) AS below_subject
"""

#: Widening rings, tightest first. Each is (label, MATCH clause, param key).
_RINGS = [
    ("same area", "MATCH (a)-[:LOCATED_IN_AREA]->(g:Area) WHERE g.name = $area", "area"),
    ("same city", "MATCH (a)-[:LOCATED_IN_CITY]->(g:City) WHERE g.name = $city", "city"),
    ("same district",
     "MATCH (a)-[:LOCATED_IN_DISTRICT]->(g:District) WHERE g.name = $district", "district"),
    ("same property type, statewide",
     "MATCH (a)-[:HAS_PROPERTY_TYPE]->(g:PropertyType) WHERE g.name IN $property_types",
     "property_types"),
]

_NOT_MARKET_VALUE = (
    "Reserve price per sqft, compared with other listings' reserve prices. "
    "This graph holds NO sold prices, so this is not market value and not "
    "what anything sold for — it is where this bank set its floor against "
    "where other banks set theirs."
)


@tool
def benchmark_price(auction_id: str | int) -> dict:
    """Compare a listing's reserve price per sqft against similar listings.

    Walks widening rings — same area, same city, same district, then the same
    property type statewide — and reports median/p25/p75 and the subject's
    position in each ring that has at least 5 comparables. Thinner rings are
    reported as too thin rather than summarised.

    **This is reserve price against reserve price, never market value.** The
    graph records no sold prices at all, so nothing here says what a property
    is worth or what one fetched. Carry that caveat into your answer; it is
    in `basis` on every response.

    **Only works on single-lot notices.** Reserve price belongs to the
    listing and extent belongs to the lot, so when a notice covers several
    lots there is no defensible way to divide one by the other. About 70% of
    listings are refused for this reason, with `reason` explaining which.
    That is a limit of the data, not a failure — report it plainly and, if
    the user wants a size comparison instead, use `find_properties` with
    `area_sqft_min`/`max`.
    """
    aid = str(auction_id).strip()
    if not aid:
        raise ToolInputError("auction_id is required.")

    params = {"id": aid, "sqft_floor": PRICING_SQFT_FLOOR,
              "sqft_ceil": PRICING_SQFT_CEIL}
    rows = run_read_query(_SUBJECT, params, timeout=20.0, max_rows=1)
    if not rows:
        return {"auction_id": aid, "priced": False,
                "reason": "No listing carries this auction_id.",
                "basis": _NOT_MARKET_VALUE}

    s = rows[0]
    refusal = _why_not_priceable(s)
    if refusal:
        return {"auction_id": aid, "priced": False, "reason": refusal,
                "notice_lot_count": s.get("lot_count"),
                "basis": _NOT_MARKET_VALUE}

    sqft = float(s["sqft"])
    psf = s["reserve_price"] / sqft
    if not (PSF_FLOOR <= psf <= PSF_CEIL):
        return {
            "auction_id": aid, "priced": False,
            "reason": (f"Reserve works out to ₹{psf:,.0f}/sqft on a recorded "
                       f"extent of {sqft:,.0f} sqft, which is outside the "
                       f"plausible ₹{PSF_FLOOR:,.0f}–₹{PSF_CEIL:,.0f} band — "
                       f"the extent or the price is wrong in the notice, so "
                       f"any comparison would be too."),
            "basis": _NOT_MARKET_VALUE,
        }

    ring_params = dict(params)
    ring_params.update({
        "psf_floor": PSF_FLOOR, "psf_ceil": PSF_CEIL, "subject_psf": psf,
        "area": s.get("area"), "city": s.get("city"),
        "district": s.get("district"),
        "property_types": s.get("property_types") or [],
    })

    comparisons, thin = [], []
    for label, match, key in _RINGS:
        if not ring_params.get(key):
            continue
        r = run_read_query(_RING.format(match=match), ring_params,
                           timeout=25.0, max_rows=1)
        row = r[0] if r else {}
        n = int(row.get("n") or 0)
        if n < MIN_COMPARABLES:
            thin.append({"ring": label, "comparables": n})
            continue
        comparisons.append({
            "ring": label, "comparables": n,
            "median_psf": row.get("median"), "p25_psf": row.get("p25"),
            "p75_psf": row.get("p75"),
            "subject_percentile": round(100 * (row.get("below_subject") or 0) / n),
        })

    out = {
        "auction_id": aid, "priced": True,
        "subject": {
            "reserve_price": s["reserve_price"],
            "extent_sqft": round(sqft, 1),
            "reserve_per_sqft": round(psf, 0),
            "area": s.get("area"), "city": s.get("city"),
            "district": s.get("district"),
            "property_types": s.get("property_types") or [],
        },
        "comparisons": comparisons,
        "basis": _NOT_MARKET_VALUE,
    }
    if thin:
        out["rings_too_thin"] = thin
        out["thin_note"] = (
            f"Rings with fewer than {MIN_COMPARABLES} comparables are not "
            f"summarised — a percentile off a handful of listings is noise. "
            f"Area rings are thin for most places (only 36 of 417 areas have "
            f"5+ priceable listings), so landing on city is normal.")
    if not comparisons:
        out["priced"] = False
        out["reason"] = ("A reserve per sqft was computed, but no ring had "
                         f"{MIN_COMPARABLES}+ comparables to judge it against.")
    return out


def _why_not_priceable(s: dict) -> str | None:
    """The refusal, named precisely enough to be reported to a user."""
    lot_count = s.get("lot_count") or 0
    if s.get("reserve_price") is None:
        return "This listing has no reserve price recorded."
    if lot_count == 0:
        return ("No sale-notice lot could be read for this listing, so there "
                "is no extent to divide the price by.")
    if lot_count > 1:
        return (f"The sale notice covers {lot_count} lots and does not say "
                f"which one this listing is. The reserve price belongs to "
                f"the listing and the extent to a lot, so a price per sqft "
                f"here would be a made-up number. Only single-lot notices "
                f"can be priced this way.")
    if s.get("sqft") is None:
        return (f"The notice gives no extent between {PRICING_SQFT_FLOOR:,.0f} "
                f"and {PRICING_SQFT_CEIL:,.0f} sqft that this tool could use "
                f"— nothing to divide the price by.")
    return None
