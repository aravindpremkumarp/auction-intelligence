"""
marketing/research/cli.py
-------------------------
Channel-research CLI: pull videos + captions + engagement metrics from a
social channel for the marketing team's content research, save locally and
(unless --no-upload) push the run to the private R2 bucket for sharing.

    python -m marketing.research instagram natgeo --limit 10
    python -m marketing.research instagram-post https://www.instagram.com/reel/Cxyz.../ --no-upload
    python -m marketing.research x-post https://x.com/SpaceX/status/123... --no-replies

Local, on-demand tooling — never run in CI (Instagram blocks datacenter
IPs). Full setup + limitations: docs/marketing/channel-research.md.

Exit codes (matches marketing_agents/poster.py): 0 ok · 1 nothing fetched ·
2 config/input error.
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import shutil
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv

from marketing.research.schema import (
    ResearchPost,
    load_existing_posts,
    merge_posts,
    write_outputs,
)

DEFAULT_OUT_DIR = Path("marketing/research/data")


def resolve_out_dir(env: dict | None = None) -> Path:
    """Output root: $RESEARCH_OUT_DIR override, else the gitignored default."""
    env = env if env is not None else os.environ
    override = env.get("RESEARCH_OUT_DIR")
    return Path(override) if override else DEFAULT_OUT_DIR


def _channel_dirname(channel: str) -> str:
    """Channel as a directory segment (IG names / X handles are already
    path-safe; just drop a leading @ and guard the empty case)."""
    return channel.strip().lstrip("@") or "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m marketing.research",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_shared(p: argparse.ArgumentParser) -> None:
        p.add_argument("--no-upload", action="store_true",
                       help="skip the R2 upload (local files only)")
        p.add_argument("--metadata-only", action="store_true",
                       help="skip media downloads (CSV/JSON only)")
        p.add_argument("--out", type=Path, default=None,
                       help="output root (default: marketing/research/data, or $RESEARCH_OUT_DIR)")

    ig = sub.add_parser("instagram", help="latest video posts (reels) from a profile")
    ig.add_argument("profile", help="Instagram profile name, e.g. natgeo")
    ig.add_argument("--limit", type=int, default=12,
                    help="video posts to pull (default 12 — keep small, be polite)")
    ig.add_argument("--max-comments", type=int, default=100,
                    help="comments scanned per post for the top-3 (0 disables; best-effort)")
    add_shared(ig)

    igp = sub.add_parser("instagram-post", help="one post/reel by URL or shortcode")
    igp.add_argument("post", help="post/reel URL or bare shortcode")
    igp.add_argument("--max-comments", type=int, default=100,
                     help="comments scanned for the top-3 (0 disables; best-effort)")
    add_shared(igp)

    xp = sub.add_parser("x-post", help="one X (Twitter) post by URL or id")
    xp.add_argument("post", help="status URL or bare tweet id")
    xp.add_argument("--no-replies", action="store_true",
                    help="skip the reply search (top-3 replies)")
    add_shared(xp)
    return parser


# ------------------------------------------------------------------ fetchers
# Each returns list[ResearchPost] (possibly empty) or an int exit code for
# config/input errors. Platform SDKs are imported lazily inside so a missing
# dep for one platform never blocks the other.

def _get_instagram_loader():
    from marketing.research import instagram

    username = os.environ.get("INSTAGRAM_USERNAME", "")
    if not username:
        print("INSTAGRAM_USERNAME is required (burner account only — see "
              "docs/marketing/channel-research.md).", file=sys.stderr)
        return 2
    try:
        return instagram.get_loader(
            username,
            os.environ.get("INSTAGRAM_PASSWORD") or None,
            os.environ.get("INSTAGRAM_SESSION_FILE") or None,
        )
    except instagram.InstagramAuthError as exc:
        print(str(exc), file=sys.stderr)
        return 2


def _fetch_instagram_profile(args: argparse.Namespace, media_dir: Path | None):
    from marketing.research import instagram

    loader = _get_instagram_loader()
    if isinstance(loader, int):
        return loader
    return instagram.fetch_profile_videos(
        loader, args.profile,
        limit=args.limit, media_dir=media_dir, max_comments=args.max_comments,
    )


def _fetch_instagram_single(args: argparse.Namespace, media_dir: Path | None):
    from marketing.research import instagram

    try:
        shortcode = instagram.parse_shortcode(args.post)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    loader = _get_instagram_loader()
    if isinstance(loader, int):
        return loader
    return [instagram.fetch_single_post(loader, shortcode,
                                        media_dir=media_dir, max_comments=args.max_comments)]


def _fetch_x_post(args: argparse.Namespace, media_dir: Path | None):
    from marketing.research import twitter

    try:
        tweet_id = twitter.parse_tweet_id(args.post)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    token = os.environ.get("X_BEARER_TOKEN", "")
    if not token:
        print("X_BEARER_TOKEN is required — set it in .env "
              "(see docs/marketing/channel-research.md).", file=sys.stderr)
        return 2
    client = twitter.get_client(token)
    return [twitter.fetch_post(client, tweet_id,
                               media_dir=media_dir, include_replies=not args.no_replies)]


# -------------------------------------------------------------------- upload

def upload_run(run_dir: Path, posts: list[ResearchPost], *,
               platform: str, channel: str, run_date: str, run_meta: dict) -> int:
    """Push a run's media + CSV/JSON to the private R2 bucket and print a
    24h presigned link to the CSV for team sharing."""
    from pipeline import storage

    try:
        # Media first, so the rewrite below lands the keys in posts.csv/json.
        for post in posts:
            if not post.media_filename or post.media_r2_key:
                continue
            local = run_dir / "media" / post.media_filename
            if not local.exists():
                continue
            key = storage.research_object_key(platform, channel, run_date,
                                              f"media/{post.media_filename}")
            storage.upload_file_private(local, key)
            post.media_r2_key = key
            print(f"uploaded {key}")
        write_outputs(run_dir, posts, run_meta)
        csv_key = ""
        for name in ("posts.csv", "posts.json", "run.json"):
            key = storage.research_object_key(platform, channel, run_date, name)
            storage.upload_file_private(run_dir / name, key)
            print(f"uploaded {key}")
            if name == "posts.csv":
                csv_key = key
        print(f"\nshare (24h presigned CSV): {storage.presigned_get_url(csv_key, expires_in=86400)}")
    except storage.R2ConfigError as exc:
        print(f"{exc} — set the R2_* vars in .env or re-run with --no-upload.", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    out_root = args.out or resolve_out_dir()
    run_date = dt.date.today().isoformat()
    platform = "x" if args.command == "x-post" else "instagram"

    # Channel-known-upfront runs write media straight into the run dir; the
    # single-post commands discover the channel from the post, so media stages
    # in a temp dir and is moved once the owner is known.
    tmp_holder: tempfile.TemporaryDirectory | None = None
    if args.command == "instagram":
        channel = _channel_dirname(args.profile)
        run_dir = out_root / platform / channel / run_date
        media_dir = None if args.metadata_only else run_dir / "media"
        result = _fetch_instagram_profile(args, media_dir)
    else:
        tmp_holder = tempfile.TemporaryDirectory(prefix="channel-research-")
        media_dir = None if args.metadata_only else Path(tmp_holder.name)
        fetch = _fetch_instagram_single if args.command == "instagram-post" else _fetch_x_post
        result = fetch(args, media_dir)

    if isinstance(result, int):
        return result
    posts = result
    if not posts:
        print("nothing fetched — profile has no video posts, or all fetches failed")
        return 1

    if args.command != "instagram":
        channel = _channel_dirname(posts[0].channel)
        run_dir = out_root / platform / channel / run_date
        if tmp_holder is not None:
            for post in posts:
                src = Path(tmp_holder.name) / post.media_filename
                if post.media_filename and src.exists():
                    (run_dir / "media").mkdir(parents=True, exist_ok=True)
                    shutil.move(str(src), str(run_dir / "media" / post.media_filename))
            tmp_holder.cleanup()

    # Same-day re-runs accumulate (deduped by post_id) instead of clobbering.
    posts = merge_posts(load_existing_posts(run_dir), posts)
    n_partial = sum(1 for p in posts if p.fetch_status == "partial")
    run_meta = {
        "command": args.command,
        "platform": platform,
        "channel": channel,
        "run_date": run_date,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "posts": len(posts),
        "partial": n_partial,
        "args": {k: str(v) for k, v in vars(args).items() if k != "command"},
    }
    paths = write_outputs(run_dir, posts, run_meta)
    print(f"\n{len(posts)} post(s) ({n_partial} partial) → {run_dir}/")
    for p in paths:
        print(f"  {p}")

    if args.no_upload:
        print("--no-upload: skipping R2.")
        return 0
    return upload_run(run_dir, posts, platform=platform, channel=channel,
                      run_date=run_date, run_meta=run_meta)


if __name__ == "__main__":
    raise SystemExit(main())
