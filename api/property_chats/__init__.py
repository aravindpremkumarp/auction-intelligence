"""
api/property_chats
------------------
Per-(user, property) persistent chat scoped to a single auction's detail
panel. A `(User)-[:OWNS_PROPERTY_CHAT]->(PropertyChat)` edge in Neo4j is
the source of truth when a user is signed in; anonymous sessions stay
in-memory only.

These chats deliberately live in their own entity (not `Conversation`)
so they don't appear in the main conversations sidebar.
"""
from __future__ import annotations

from api.property_chats.router import router

__all__ = ["router"]
