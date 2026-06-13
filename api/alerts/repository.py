"""
api/alerts/repository.py
------------------------
Read-only Cypher gateway for deadline alerts. Returns the minimal row shape
(`auction_id`, `title`, `city`, `deadline`) the alerts service needs; the
classification lives in api/alerts/service.py.

Two entry points:
  - `deadlines_for_saved` — a logged-in user's `:SAVED` properties (the
    authed `GET /alerts` path).
  - `deadlines_for_ids` — an explicit id set (the anonymous `POST /alerts`
    path, where the client sends its localStorage watchlist).

Both filter out properties with no `application_deadline_dt` at the database
so the service never has to reason about nulls.
"""
from __future__ import annotations

from api.neo4j_client import run_read_query_async

# Bound the anonymous id set so a client can't ask us to scan an unbounded
# list. A real watchlist is a handful of properties; 200 is generous.
_MAX_IDS = 200

_RETURN = """
    RETURN a.auction_id AS auction_id, a.title AS title,
           toString(a.application_deadline_dt) AS deadline,
           c.name AS city
"""


async def deadlines_for_saved(supabase_id: str) -> list[dict]:
    cypher = f"""
        MATCH (u:User {{supabase_id: $sub}})-[:SAVED]->(a:AuctionProperty)
        WHERE a.application_deadline_dt IS NOT NULL
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
        {_RETURN}
    """
    return await run_read_query_async(cypher, {"sub": supabase_id}, max_rows=_MAX_IDS)


async def deadlines_for_ids(auction_ids: list[str]) -> list[dict]:
    ids = _dedupe(auction_ids)
    if not ids:
        return []
    cypher = f"""
        MATCH (a:AuctionProperty)
        WHERE a.auction_id IN $ids
          AND a.application_deadline_dt IS NOT NULL
        OPTIONAL MATCH (a)-[:LOCATED_IN_CITY]->(c:City)
        {_RETURN}
    """
    return await run_read_query_async(cypher, {"ids": ids}, max_rows=_MAX_IDS)


def _dedupe(auction_ids: list[str] | None) -> list[str]:
    out: list[str] = []
    for raw in auction_ids or []:
        s = str(raw).strip()
        if s and s not in out:
            out.append(s)
        if len(out) >= _MAX_IDS:
            break
    return out
