"""
api/watchlist/repository.py
---------------------------
Cypher gateway for the per-user watchlist (`User -[:SAVED]-> AuctionProperty`).

The auction node label in this graph is `AuctionProperty` keyed by
`auction_id`; the user node is keyed by `supabase_id` (see api/auth/repository).
"""
from __future__ import annotations

from api.neo4j_client import run_query


def list_saved_auction_ids(supabase_id: str) -> list[str]:
    rows = run_query(
        """
        MATCH (u:User {supabase_id: $sub})-[r:SAVED]->(a:AuctionProperty)
        RETURN a.auction_id AS auction_id
        ORDER BY r.saved_at DESC
        """,
        {"sub": supabase_id},
    )
    return [r["auction_id"] for r in rows if r.get("auction_id")]


def add_saved(supabase_id: str, auction_id: str) -> bool:
    """Create a SAVED edge if the auction exists. Returns True if the
    auction was found (edge created or already present), False otherwise."""
    rows = run_query(
        """
        MATCH (u:User {supabase_id: $sub})
        MATCH (a:AuctionProperty {auction_id: $aid})
        MERGE (u)-[r:SAVED]->(a)
          ON CREATE SET r.saved_at = datetime()
        RETURN a.auction_id AS auction_id
        """,
        {"sub": supabase_id, "aid": auction_id},
    )
    return bool(rows)


def remove_saved(supabase_id: str, auction_id: str) -> None:
    run_query(
        """
        MATCH (:User {supabase_id: $sub})-[r:SAVED]->(:AuctionProperty {auction_id: $aid})
        DELETE r
        """,
        {"sub": supabase_id, "aid": auction_id},
    )
