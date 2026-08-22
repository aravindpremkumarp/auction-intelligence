"""
api/agent3/reauction_history.py
-------------------------------
Has this property failed to sell before, and has the bank cut the price?

A falling reserve across attempts is the strongest buy signal this graph
holds, and it comes from two independent places that must not be conflated:

**1. `Auction.attempt_no`** — the notice's own statement of which attempt
this is. 206 auctions sit at attempt ≥ 2 (144 at 2, 36 at 3, 18 at 4, and a
tail to 8). Authoritative about the count, but it carries **no earlier
price**: every attempt-2 row observed shows only its own reserve.

**2. `SAME_PROPERTY_AS`** — 80 links joining listings the pipeline matched as
the same property across notices. These DO carry the earlier price, and the
drops are real and consistent: ₹45.58L→₹41L, ₹69L→₹62.1L, ₹52L→₹46.8L — all
close to 10%, which is the SARFAESI convention for reducing a reserve after a
failed auction.

So `attempt_no` answers "has this failed before" and the link chain answers
"by how much has it come down". Neither substitutes for the other, and the
link is a **pipeline inference** carrying a `confidence` of high or medium —
surfaced on every linked listing, because a medium-confidence match is a
weaker claim about the world than the notice's own attempt number.
"""
from __future__ import annotations

from api.agent3.common import ToolInputError, json_safe, scope_of, tool
from api.neo4j_client import run_read_query

_ATTEMPTS = """
MATCH (a:AuctionProperty {auction_id: $id})
OPTIONAL MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(l:Lot)
WITH a, count(DISTINCT l) AS lot_count
CALL (a) {
  MATCH (a)-[:HAS_DOCUMENT]->(:Document)-[:HAS_LOT]->(:Lot)
        -[:OFFERED_IN]->(au:Auction)
  RETURN collect(DISTINCT {
    attempt_no: au.attempt_no, reserve_price: au.reserve_price_num,
    emd: au.emd_num, auction_start: au.auction_start_dt,
    sarfaesi_stage: au.sarfaesi_stage, outcome: au.outcome
  }) AS attempts
}
RETURN a.auction_id AS auction_id, a.reserve_price_num AS reserve_price,
       a.auction_start_dt AS auction_start, lot_count, attempts
"""

#: Traversed in BOTH directions: the link is stored one-way but means "the
#: same property", so an earlier listing points at a later one as readily as
#: the reverse. Following only the stored direction would hide half the
#: chains.
_LINKED = """
MATCH (a:AuctionProperty {auction_id: $id})-[r:SAME_PROPERTY_AS]-(o:AuctionProperty)
// A pair can carry the relationship in BOTH directions, and the undirected
// match then yields the same listing twice. Collapse per listing, keeping the
// strongest confidence rather than whichever row arrived first.
WITH o, collect(r) AS rels
WITH o, [x IN rels WHERE x.confidence = 'high'] AS strong, head(rels) AS any_rel
WITH o, CASE WHEN size(strong) > 0 THEN head(strong) ELSE any_rel END AS r
OPTIONAL MATCH (o)-[:CONDUCTED_BY]->(b:Bank)
RETURN o.auction_id AS auction_id, o.reserve_price_num AS reserve_price,
       o.auction_start_dt AS auction_start, b.name AS bank,
       r.confidence AS confidence, r.match_reason AS match_reason
ORDER BY o.auction_start_dt
"""


@tool
def reauction_history(auction_id: str | int) -> dict:
    """Has this property been auctioned before, and has the price come down?

    Combines two independent signals, kept separate on purpose:

    - `attempts` — what the sale notice itself says (`attempt_no`). 206
      auctions in this graph are at attempt 2 or later. Authoritative about
      how many times it has run, but carries no earlier price.
    - `earlier_listings` — other listings the pipeline matched as the same
      property (`SAME_PROPERTY_AS`), each with its reserve price, so a real
      drop can be computed. This is an INFERRED link with a `confidence` of
      high or medium — say which when you rely on it.

    `price_change` is only filled when a linked listing carries a price. A
    drop near 10% is the SARFAESI convention for re-offering after a failed
    auction, not a bargain unique to this property — say so rather than
    presenting it as a discount someone negotiated.

    A property with no attempts above 1 and no links is simply a first-time
    listing; that is the common case, not a gap.
    """
    aid = str(auction_id).strip()
    if not aid:
        raise ToolInputError("auction_id is required.")

    rows = run_read_query(_ATTEMPTS, {"id": aid}, timeout=20.0, max_rows=1)
    if not rows:
        return {"auction_id": aid, "found": False,
                "hint": "No listing carries this auction_id."}

    row = json_safe(rows[0])
    lot_count = row.get("lot_count") or 0
    attempts = [a for a in (row.get("attempts") or []) if a.get("attempt_no")]
    attempts.sort(key=lambda a: a["attempt_no"])
    max_attempt = max((a["attempt_no"] for a in attempts), default=None)

    linked = json_safe(run_read_query(_LINKED, {"id": aid},
                                      timeout=20.0, max_rows=20))
    subject_price = row.get("reserve_price")
    for other in linked:
        prior = other.get("reserve_price")
        if prior and subject_price and prior != subject_price:
            delta = subject_price - prior
            other["price_change"] = {
                "from": prior, "to": subject_price,
                "change": round(delta, 0),
                "percent": round(100 * delta / prior, 1),
            }

    out: dict = {
        "auction_id": aid, "found": True,
        "reserve_price": subject_price,
        "auction_start": row.get("auction_start"),
        "notice_lot_count": lot_count,
        "attempts": attempts,
        "highest_attempt_no": max_attempt,
        "earlier_listings": linked,
    }

    if max_attempt and max_attempt >= 2:
        out["scope"] = scope_of(lot_count)
        if lot_count > 1:
            out["scope_note"] = (
                f"`attempt_no` comes from the sale notice, which covers "
                f"{lot_count} lots — it describes the notice's auction, not "
                f"necessarily this listing alone.")

    out["summary"] = _summarise(max_attempt, linked)
    return out


def _summarise(max_attempt: int | None, linked: list[dict]) -> str:
    """One line the agent can quote without re-deriving it."""
    drops = [o["price_change"] for o in linked if o.get("price_change")]
    falls = [d for d in drops if d["percent"] < 0]

    if not max_attempt or max_attempt < 2:
        if falls:
            best = min(falls, key=lambda d: d["percent"])
            return (f"The notice does not mark this as a re-auction, but a "
                    f"linked earlier listing was priced {abs(best['percent'])}% "
                    f"higher — treat the link as inferred, not stated.")
        return ("No earlier attempt recorded: the notice marks this as a "
                "first auction and no earlier listing is linked to it.")

    base = f"The notice marks this as attempt {max_attempt}"
    if not falls:
        return (f"{base}, so it has failed to sell at least once. No earlier "
                f"listing with a price is linked, so the size of any "
                f"reduction is not recorded here.")
    best = min(falls, key=lambda d: d["percent"])
    return (f"{base}, and the reserve has come down {abs(best['percent'])}% "
            f"from ₹{best['from']:,.0f} to ₹{best['to']:,.0f}. A cut near 10% "
            f"is the standard SARFAESI reduction after a failed auction, not "
            f"a discount specific to this property.")
