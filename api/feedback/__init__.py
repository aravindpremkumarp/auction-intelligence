"""
api/feedback
------------
Thumbs-up/down and free-text feedback capture (stored as `:Feedback` nodes in
Neo4j) plus the admin/automation surface that lists and resolves it. The
GitHub `sync-feedback` / `resolve-feedback` workflows read and close items
here via a shared resolve token.
"""
from __future__ import annotations

from api.feedback.router import router

__all__ = ["router"]
