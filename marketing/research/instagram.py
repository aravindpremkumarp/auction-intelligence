"""
marketing/research/instagram.py
-------------------------------
Hardened instaloader wrapper for channel research. Differences from the
original Colab notebook this replaces:

- credentials come from env vars, never source (the notebook's leaked and
  had to be rotated);
- the session is cached to disk after the first login and reused — repeat
  password logins are what trigger Instagram checkpoints;
- video posts are detected via ``post.is_video`` (the notebook's
  ``typename == 'GraphVideo'`` silently misses reels on newer responses);
- comments and media downloads are best-effort: Instagram often returns
  403 login_required on the comments/info endpoints even when logged in,
  so failures degrade to ``fetch_status="partial"`` instead of crashing.

instaloader is imported lazily so the pure helpers (and their tests) work
without it installed.
"""
from __future__ import annotations

import random
import re
import time
from itertools import islice
from pathlib import Path
from typing import Any

from marketing.research.media import download_media
from marketing.research.schema import ResearchPost, top_n_comments

_SHORTCODE_RE = re.compile(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_BARE_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,15}$")

_COMMENTS_HINT = (
    "comments unavailable — Instagram often 403s the comments endpoint even "
    "when logged in (known limitation; metadata kept)"
)


class InstagramAuthError(RuntimeError):
    """Login/session failure with an actionable message."""


def parse_shortcode(url_or_shortcode: str) -> str:
    """Shortcode from a post/reel URL (any of /p/ /reel/ /reels/ /tv/,
    query strings tolerated); a bare shortcode passes through."""
    value = url_or_shortcode.strip()
    m = _SHORTCODE_RE.search(value)
    if m:
        return m.group(1)
    if "/" not in value and _BARE_SHORTCODE_RE.match(value):
        return value
    raise ValueError(f"Not an Instagram post URL or shortcode: {url_or_shortcode!r}")


def get_loader(username: str, password: str | None = None, session_file: str | None = None):
    """Logged-in ``instaloader.Instaloader``, reusing a cached session when
    one exists (``session_file=None`` → instaloader's default location,
    ``~/.config/instaloader/session-<username>``)."""
    import instaloader

    loader = instaloader.Instaloader(
        quiet=True,
        download_comments=False,
        save_metadata=False,
        download_geotags=False,
        post_metadata_txt_pattern="",
    )
    try:
        loader.load_session_from_file(username, session_file)
        print(f"session reused for {username} (no login performed)")
        return loader
    except FileNotFoundError:
        pass

    if not password:
        raise InstagramAuthError(
            "No cached session and INSTAGRAM_PASSWORD is not set. Set it in .env "
            f"(burner account only) or create a session once with `instaloader --login {username}`."
        )
    try:
        loader.login(username, password)
    except instaloader.TwoFactorAuthRequiredException as exc:
        raise InstagramAuthError(
            "This account has 2FA enabled. Create a session interactively once "
            f"with `instaloader --login {username}`, then re-run."
        ) from exc
    except instaloader.BadCredentialsException as exc:
        raise InstagramAuthError("Instagram rejected the credentials.") from exc
    except instaloader.ConnectionException as exc:
        raise InstagramAuthError(
            f"Instagram blocked the login ({exc}). Usually a checkpoint or a "
            "datacenter IP — use a burner account from a residential connection."
        ) from exc
    loader.save_session_to_file(session_file)
    print(f"logged in as {username}; session cached for future runs")
    return loader


def fetch_profile_videos(
    loader,
    profile_name: str,
    *,
    limit: int,
    media_dir: Path | None,
    max_comments: int,
    sleep_range: tuple[float, float] = (2.0, 5.0),
) -> list[ResearchPost]:
    """Latest ``limit`` video posts (reels) from a profile. Sleeps a random
    interval between posts to stay under Instagram's rate heuristics."""
    import instaloader

    profile = instaloader.Profile.from_username(loader.context, profile_name)
    posts: list[ResearchPost] = []
    for post in profile.get_posts():
        if not post.is_video:
            continue
        rp = _post_to_research(post, profile_name, media_dir, max_comments)
        posts.append(rp)
        print(f"[{len(posts)}/{limit}] {rp.url} ({rp.fetch_status})")
        if len(posts) >= limit:
            break
        time.sleep(random.uniform(*sleep_range))
    return posts


def fetch_single_post(loader, shortcode: str, *, media_dir: Path | None, max_comments: int) -> ResearchPost:
    """One post/reel by shortcode; channel = the post owner's username."""
    import instaloader

    post = instaloader.Post.from_shortcode(loader.context, shortcode)
    return _post_to_research(post, post.owner_username, media_dir, max_comments)


def _post_to_research(post: Any, channel: str, media_dir: Path | None, max_comments: int) -> ResearchPost:
    errors: list[str] = []
    posted_at = post.date_utc.isoformat() + "Z" if getattr(post, "date_utc", None) else ""
    rp = ResearchPost(
        platform="instagram",
        channel=channel,
        post_id=str(post.mediaid),
        shortcode=post.shortcode,
        url=f"https://www.instagram.com/p/{post.shortcode}/",
        posted_at=posted_at,
        caption=post.caption or "",
        likes=getattr(post, "likes", None),
        views=getattr(post, "video_view_count", None),
        comments_count=getattr(post, "comments", None),
        media_type="video" if post.is_video else "image",
    )
    if max_comments > 0:
        try:
            rp.top_comments = top_n_comments(islice(post.get_comments(), max_comments))
        except Exception as exc:  # best-effort by design: 403s here are routine
            errors.append(f"{_COMMENTS_HINT}: {exc}")
    if media_dir is not None:
        try:
            filename = f"{post.shortcode}.mp4" if post.is_video else f"{post.shortcode}.jpg"
            src = post.video_url if post.is_video else post.url
            download_media(src, media_dir / filename)
            rp.media_filename = filename
        except Exception as exc:  # keep the metadata even when the CDN 403s
            errors.append(f"media download failed (Instagram often 403s video URLs): {exc}")
    if errors:
        rp.fetch_status = "partial"
        rp.error = " | ".join(errors)
    return rp
