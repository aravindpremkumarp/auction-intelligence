"""
api/watchlist
-------------
Per-user saved-auctions store. A `(User)-[:SAVED]->(AuctionProperty)` edge
in Neo4j is the source of truth when a user is signed in; the frontend
falls back to localStorage for anonymous/guest sessions.
"""
from __future__ import annotations

from api.watchlist.router import router

__all__ = ["router"]
