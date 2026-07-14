"""
marketing/research/twitter.py
-----------------------------
X (Twitter) API v2 wrapper via tweepy for channel research. Tier caveats
(all degrade to ``fetch_status="partial"``, never a crash):

- replies come from recent search (``conversation_id:``) which only covers
  ~7 days and is not available on the free tier;
- ``impression_count`` (views) may be absent depending on tier;
- mp4 ``variants`` are exposed on most tiers — when absent we fall back to
  the preview image, then to metadata-only.

tweepy is imported lazily so the pure helpers (and their tests) work
without it installed.
"""
from __future__ import annotations

import re
from pathlib import Path

from marketing.research.media import download_media
from marketing.research.schema import ResearchPost, top_n_comments

_TWEET_ID_RE = re.compile(r"(?:x\.com|twitter\.com)/[^/]+/status(?:es)?/(\d+)")


def parse_tweet_id(url_or_id: str) -> str:
    """Tweet id from a status URL (x.com / twitter.com / mobile.twitter.com,
    query strings tolerated); a bare numeric id passes through."""
    value = url_or_id.strip()
    m = _TWEET_ID_RE.search(value)
    if m:
        return m.group(1)
    if value.isdigit():
        return value
    raise ValueError(f"Not an X/Twitter status URL or tweet id: {url_or_id!r}")


def pick_video_variant(variants: list[dict] | None) -> str | None:
    """Highest-bitrate mp4 URL from a v2 media ``variants`` list. Ignores
    application/x-mpegURL playlists; None when the tier exposes no variants."""
    best_url, best_rate = None, -1
    for v in variants or []:
        if v.get("content_type") != "video/mp4":
            continue
        rate = v.get("bit_rate") or 0
        if rate > best_rate:
            best_url, best_rate = v.get("url"), rate
    return best_url


def get_client(bearer_token: str):
    """App-auth tweepy client; blocks politely instead of erroring on 429s."""
    import tweepy

    return tweepy.Client(bearer_token=bearer_token, wait_on_rate_limit=True)


def fetch_post(
    client,
    tweet_id: str,
    *,
    media_dir: Path | None,
    include_replies: bool = True,
    max_replies: int = 100,
) -> ResearchPost:
    """One X post by id: text + public metrics, best-effort media download
    and top-3 replies by likes."""
    import tweepy

    resp = client.get_tweet(
        tweet_id,
        expansions=["attachments.media_keys", "author_id"],
        tweet_fields=["public_metrics", "created_at", "conversation_id", "text"],
        media_fields=["type", "url", "preview_image_url", "variants"],
        user_fields=["username"],
    )
    tweet = resp.data
    if tweet is None:
        raise ValueError(f"Tweet {tweet_id} not found (deleted, protected, or wrong id).")
    includes = resp.includes or {}
    users = includes.get("users") or []
    username = users[0].username if users else ""
    metrics = dict(tweet.public_metrics or {})
    errors: list[str] = []

    rp = ResearchPost(
        platform="x",
        channel=username,
        post_id=str(tweet.id),
        url=f"https://x.com/{username or 'i'}/status/{tweet.id}",
        posted_at=tweet.created_at.isoformat() if tweet.created_at else "",
        caption=tweet.text or "",
        likes=metrics.get("like_count"),
        views=metrics.get("impression_count"),  # absent on some tiers → None
        comments_count=metrics.get("reply_count"),
        extra={
            k: metrics[k]
            for k in ("retweet_count", "quote_count", "bookmark_count")
            if k in metrics
        },
    )

    media_list = includes.get("media") or []
    if media_list:
        media = media_list[0]
        media_kind = getattr(media, "type", None)
        url: str | None = None
        suffix = ""
        if media_kind in ("video", "animated_gif"):
            rp.media_type = "video"
            url, suffix = pick_video_variant(getattr(media, "variants", None)), ".mp4"
            if not url:
                url, suffix = getattr(media, "preview_image_url", None), ".jpg"
                errors.append("no mp4 variant on this API tier — saved the preview image instead")
        elif media_kind == "photo":
            rp.media_type = "image"
            url, suffix = getattr(media, "url", None), ".jpg"
        if url and media_dir is not None:
            try:
                filename = f"{tweet.id}{suffix}"
                download_media(url, media_dir / filename)
                rp.media_filename = filename
            except Exception as exc:  # metadata still worth keeping
                errors.append(f"media download failed: {exc}")

    if include_replies:
        try:
            search = client.search_recent_tweets(
                f"conversation_id:{tweet.id}",
                tweet_fields=["public_metrics"],
                max_results=min(max(max_replies, 10), 100),
            )
            replies = [
                {"username": "", "text": r.text, "likes": (r.public_metrics or {}).get("like_count", 0)}
                for r in (search.data or [])
            ]
            rp.top_comments = top_n_comments(replies)
            if not replies and (rp.comments_count or 0) > 0:
                errors.append("replies exist but none in the ~7-day recent-search window")
        except tweepy.Forbidden:
            errors.append("recent search not available on this API tier — replies skipped")
        except tweepy.TweepyException as exc:
            errors.append(f"reply fetch failed: {exc}")

    if errors:
        rp.fetch_status = "partial"
        rp.error = " | ".join(errors)
    return rp
