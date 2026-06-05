"""
api/chat
--------
The conversational agent surface: `/chat` (run the pydantic-ai agent over the
Neo4j graph) and `/modes` (the mode registry the UI renders). Also owns the
rolling-scope + history-trimming helpers that keep multi-turn context cheap.
"""
from __future__ import annotations

from api.chat.router import router

__all__ = ["router"]
