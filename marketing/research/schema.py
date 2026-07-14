"""
marketing/research/schema.py
----------------------------
Shared data model + writers for channel-research pulls. One ``ResearchPost``
per pulled post, identical shape for both platforms; ``posts.csv`` keeps the
stable column order below (documented in docs/marketing/channel-research.md),
``posts.json`` keeps full fidelity.

Pure stdlib — no platform SDKs — so everything here is unit-testable offline.
"""
from __future__ import annotations

import csv
import json
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Stable CSV column order. Append-only: downstream analysis reads by name,
# but a stable order keeps diffs and spreadsheets sane.
CSV_COLUMNS: tuple[str, ...] = (
    "platform",        # "instagram" | "x"
    "channel",         # profile/handle the post belongs to
    "post_id",         # IG: mediaid; X: tweet id
    "shortcode",       # IG shortcode; "" for X
    "url",             # canonical post URL
    "posted_at",       # ISO-8601 UTC, "" if unknown
    "caption",         # IG caption / X text ("" never None)
    "likes",           # int | None (None = platform didn't expose it)
    "views",           # IG video_view_count / X impression_count | None
    "comments_count",  # IG comments / X reply_count | None
    "top_comments",    # JSON: [{"username","text","likes"}], max 3, [] if unavailable
    "media_type",      # "video" | "image" | "none"
    "media_filename",  # basename under media/; "" when metadata-only or failed
    "media_r2_key",    # "" until uploaded
    "fetch_status",    # "ok" | "partial" (metadata got, comments/media failed) | "failed"
    "error",           # human-readable reason when partial/failed
    "extra",           # JSON of platform extras (X: retweets/quotes/bookmarks; IG: {})
)


@dataclass
class ResearchPost:
    """One pulled post, platform-agnostic. Field per CSV_COLUMNS entry."""
    platform: str
    channel: str
    post_id: str
    shortcode: str = ""
    url: str = ""
    posted_at: str = ""
    caption: str = ""
    likes: int | None = None
    views: int | None = None
    comments_count: int | None = None
    top_comments: list[dict] = field(default_factory=list)
    media_type: str = "none"
    media_filename: str = ""
    media_r2_key: str = ""
    fetch_status: str = "ok"
    error: str = ""
    extra: dict = field(default_factory=dict)


def _comment_record(c: Any) -> dict:
    """Normalise one comment/reply into {"username","text","likes"}.

    Accepts a plain mapping (the X path builds dicts) or an instaloader-style
    object. ``likes_count`` is not reliably present on instaloader comments —
    a missing/None attribute counts as 0 instead of crashing (the original
    notebook worked around this with getattr too).
    """
    if isinstance(c, Mapping):
        return {
            "username": str(c.get("username") or ""),
            "text": str(c.get("text") or ""),
            "likes": int(c.get("likes") or 0),
        }
    owner = getattr(c, "owner", None)
    username = getattr(owner, "username", None) or getattr(c, "username", None) or ""
    return {
        "username": str(username),
        "text": str(getattr(c, "text", None) or ""),
        "likes": int(getattr(c, "likes_count", 0) or 0),
    }


def top_n_comments(comments: Iterable[Any], n: int = 3) -> list[dict]:
    """Top-``n`` comments by like count, descending. Empty input → []."""
    records = [_comment_record(c) for c in comments]
    records.sort(key=lambda r: r["likes"], reverse=True)
    return records[:n]


def to_csv_row(post: ResearchPost) -> dict:
    """Flatten a post for csv.DictWriter — structured fields JSON-encoded."""
    row = asdict(post)
    row["top_comments"] = json.dumps(post.top_comments, ensure_ascii=False)
    row["extra"] = json.dumps(post.extra, ensure_ascii=False)
    return row


def load_existing_posts(run_dir: Path) -> list[ResearchPost]:
    """Posts from a previous run in the same dir (best effort — [] on any
    problem). Lets same-day re-runs accumulate instead of clobbering."""
    try:
        raw = json.loads((run_dir / "posts.json").read_text(encoding="utf-8"))
        return [ResearchPost(**row) for row in raw]
    except (OSError, json.JSONDecodeError, TypeError):
        return []


def merge_posts(existing: list[ResearchPost], new: list[ResearchPost]) -> list[ResearchPost]:
    """Existing + new, deduped by (platform, post_id); a re-pulled post wins."""
    fresh = {(p.platform, p.post_id) for p in new}
    kept = [p for p in existing if (p.platform, p.post_id) not in fresh]
    return kept + new


def write_outputs(run_dir: Path, posts: list[ResearchPost], run_meta: dict) -> list[Path]:
    """Write posts.csv (CSV_COLUMNS order), posts.json (full fidelity) and
    run.json (run metadata) under ``run_dir``. Returns the written paths."""
    run_dir.mkdir(parents=True, exist_ok=True)
    csv_path = run_dir / "posts.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(CSV_COLUMNS))
        writer.writeheader()
        for post in posts:
            writer.writerow(to_csv_row(post))
    json_path = run_dir / "posts.json"
    json_path.write_text(
        json.dumps([asdict(p) for p in posts], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    run_path = run_dir / "run.json"
    run_path.write_text(json.dumps(run_meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return [csv_path, json_path, run_path]
