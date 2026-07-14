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

from api.neo4j_client import run_query_async, run_read_query_async

# Bound the anonymous id set so a client can't ask us to scan an unbounded
# list. A real watchlist is a handful of properties; 200 is generous.
_MAX_IDS = 200


async def upsert_subscriber(email: str, city: str | None, property_type: str | None,
                            source: str | None, created_at: str) -> None:
    """Idempotently record an auction-alert email subscriber.

    MERGE on the normalized email so re-subscribing (e.g. from a different
    landing page) updates the filter rather than creating a duplicate, and
    re-activates a previously-unsubscribed address. The original created_at,
    source, and unsubscribe token are set once (ON CREATE) so attribution and
    the token survive a re-subscribe. No email is sent from here — this only
    builds the list; the sending engine is a separate, later piece.
    """
    import secrets

    await run_query_async(
        """
        MERGE (s:AlertSubscriber {email: $email})
        ON CREATE SET s.created_at = datetime($created_at),
                      s.source = $source,
                      s.unsubscribe_token = $token
        SET s.city = $city,
            s.property_type = $property_type,
            s.active = true,
            s.updated_at = datetime($created_at)
        RETURN s.email AS email
        """,
        {
            "email": email,
            "city": city,
            "property_type": property_type,
            "source": source,
            "created_at": created_at,
            "token": secrets.token_urlsafe(24),
        },
    )

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
