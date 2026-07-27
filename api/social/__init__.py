"""
api/social
----------
Admin review surface for staged social content. The Poster
(`marketing_agents/poster.py`) writes a batch to `marketing/outputs/<date>/`
and the content-poster workflow commits it; until now the only way to review
that batch was to open the files in a local checkout. This package serves the
committed batch over HTTP and persists an approve/reject/posted status per
artifact as `:SocialContent` nodes, so the human publish gate is auditable.

Nothing here writes into `marketing/outputs/` — the batch is read-only from
the API's point of view. The Poster and the workflow remain its only authors.
"""
from __future__ import annotations

from api.social.router import router

__all__ = ["router"]
