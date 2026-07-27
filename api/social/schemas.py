"""
api/social/schemas.py
---------------------
Response/request models for the staged-content review surface.

`status` is deliberately absent from the on-disk batch: the filesystem holds
what the Poster staged, Neo4j holds what a human decided about it. The two are
joined at read time in the router.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

# 'pending' is the absence of a :SocialContent node, so it is a valid input
# (it clears the status) but never a stored value.
Status = Literal["pending", "approved", "rejected", "posted"]
ArtifactKind = Literal["card", "reel", "carousel"]


class StatusCounts(BaseModel):
    """Per-batch rollup. `pending` is derived by the caller (total - decided),
    so it is not included here — this only counts what was actually stored."""
    approved: int = 0
    rejected: int = 0
    posted: int = 0


class BatchSummary(BaseModel):
    date: str
    generated_at: str | None = None
    editor_notes: str | None = None
    n_drafts: int = 0
    n_cards: int = 0
    n_reels: int = 0
    n_carousels: int = 0
    statuses: StatusCounts = StatusCounts()
    # Set when drafts.json exists but could not be parsed. The batch is still
    # listed (silently dropping it would look like the batch was never staged).
    error: str | None = None


class ArtifactOut(BaseModel):
    kind: ArtifactKind
    stem: str
    template: str
    auction_id: str | None = None
    headline: str | None = None
    # Path of the #data island JSON, relative to the batch directory.
    island_path: str
    # Rendered PNGs, relative to the batch directory. A single card has one; a
    # carousel has one per slide; empty when the render step never ran (or the
    # draft was staged with needs_image: false).
    png_paths: list[str] = []
    png_available: bool = False
    # Reel-only: the on-screen hook, so the page can show what the first frame
    # says without an MP4. Reel MP4s are 14-day workflow artifacts, not committed.
    hook: str | None = None
    # Joined from :SocialContent.
    status: str = "pending"
    note: str | None = None
    posted_url: str | None = None
    updated_at: str | None = None
    updated_by_email: str | None = None


class DraftOut(BaseModel):
    draft_index: int
    auction_id: str | None = None
    angle: str | None = None
    hook_mechanism: str | None = None
    post: str = ""
    pinned_comment: str | None = None
    hashtags: list[str] = []
    alt_text: str | None = None
    video_title: str | None = None
    location_tag: str | None = None
    engagement_question: str | None = None
    save_line: str | None = None
    reel_hook: dict | None = None
    reel_context_lines: list[str] = []
    hook_alternatives: list[str] = []
    # The grounded facts the copy was written from, verbatim off drafts.json.
    source: dict = {}
    artifacts: list[ArtifactOut] = []


class BatchDetail(BaseModel):
    date: str
    generated_at: str | None = None
    editor_notes: str | None = None
    stats: dict = {}
    drafts: list[DraftOut] = []
    # Artifacts with no owning draft — the stats reel (draft_index 0) and the
    # city carousel, which is about a city rather than one auction.
    orphan_artifacts: list[ArtifactOut] = []
    rejected: list[str] = []


class StatusIn(BaseModel):
    status: Status
    note: str | None = None
    posted_url: str | None = None
