"""
api/conversations
-----------------
Per-user persistent chat conversations. A
`(User)-[:OWNS]->(Conversation)` edge in Neo4j is the source of truth
when a user is signed in; anonymous sessions stay in-memory only.
"""
from __future__ import annotations

from api.conversations.router import router

__all__ = ["router"]
