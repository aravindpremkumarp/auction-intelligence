#!/usr/bin/env python3
"""
scripts/upload_batch_media_to_r2.py
-----------------------------------
Upload a staged batch's rendered media — card PNGs, carousel slides, reel MP4s
— to the private R2 bucket and record the keys in the batch's ``drafts.json``.

Why not git: reel MP4s never could be committed (binaries grow the repo
forever), and as 14-day workflow artifacts they simply vanished — the
2026-07-15 batch kept its captions and cards but lost all six reels. Card PNGs
could be committed and were, but a weekly batch of 5-8 images accumulates for
exactly the reason the 664 property OG cards already live in R2. Both now go to
the same place, so the review page has one story for every artifact.

The keys land in a batch-level ``media_keys`` map, {batch-relative path → R2
key}, rather than on individual manifest rows: the API already knows an
artifact's path, so a path-keyed map lets it resolve any file — card, carousel
slide, or reel — without a per-kind lookup.

Usage (the content-poster workflow calls this twice: after cards render, then
again after reels render):

    python scripts/upload_batch_media_to_r2.py \
        --batch marketing/outputs/2026-08-02 --renders .reel_renders

Exit codes: 0 = uploaded (or nothing to do), 2 = R2 not configured (the caller
should fall back to committing PNGs / publishing the reel artifact), 1 = every
upload failed.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from pipeline import storage  # noqa: E402

# Rendered output only. The islands, drafts.json and review.md stay in git —
# they're small, diffable, and they are the batch's actual source of truth.
_MEDIA_SUFFIXES = {".png", ".mp4", ".webm"}
_VIDEO_SUFFIXES = {".mp4", ".webm"}


def collect_media(batch_dir: Path, renders_dir: Path | None) -> list[tuple[str, Path]]:
    """(batch-relative path, local file) for every rendered asset to upload.

    Two sources: ``<batch>/rendered/`` holds the card and carousel PNGs written
    by render_social.py, and ``renders_dir`` holds the reel MP4s, which
    render_reel.py writes to a scratch directory outside the batch. Reels are
    mapped into the batch's namespace as ``reels/<stem>.mp4`` so both kinds are
    addressed the same way.
    """
    found: list[tuple[str, Path]] = []
    rendered = batch_dir / "rendered"
    if rendered.is_dir():
        for f in sorted(rendered.iterdir()):
            if f.is_file() and f.suffix.lower() in _MEDIA_SUFFIXES:
                found.append((f"rendered/{f.name}", f))
    if renders_dir and renders_dir.is_dir():
        for f in sorted(renders_dir.iterdir()):
            if f.is_file() and f.suffix.lower() in _VIDEO_SUFFIXES:
                found.append((f"reels/{f.name}", f))
    return found


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", required=True,
                    help="batch directory, e.g. marketing/outputs/2026-08-02")
    ap.add_argument("--renders", default=None,
                    help="directory holding rendered reel MP4s (omit for a cards-only pass)")
    args = ap.parse_args()

    batch_dir = Path(args.batch)
    manifest = batch_dir / "drafts.json"
    if not manifest.is_file():
        print(f"no manifest at {manifest} — nothing to upload")
        return 0

    try:
        storage._require_private_config()
        # r2_client() builds one client for both buckets and validates the
        # PUBLIC config on the way, so a private-only write still needs
        # R2_BUCKET / R2_PUBLIC_BASE_URL. Check it up front rather than
        # discovering it once per upload — exit 2 is what tells the workflow to
        # fall back.
        storage._require_config()
    except storage.R2ConfigError as exc:
        print(f"R2 not configured ({exc}) — leaving the manifest untouched")
        return 2

    media = collect_media(batch_dir, Path(args.renders) if args.renders else None)
    if not media:
        print("no rendered media to upload")
        return 0

    data = json.loads(manifest.read_text(encoding="utf-8"))
    keys: dict[str, str] = dict(data.get("media_keys") or {})
    sizes: dict[str, int] = dict(data.get("media_bytes") or {})
    batch_date = batch_dir.name

    uploaded = 0
    for relpath, local in media:
        key = storage.batch_media_key(batch_date, relpath)
        try:
            storage.upload_file_private(local, key)
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            print(f"FAILED {relpath} -> {key}: {exc}", file=sys.stderr)
            continue
        keys[relpath] = key
        # Recorded so the review page can label a reel's load button with its
        # weight before you commit to downloading it.
        sizes[relpath] = local.stat().st_size
        uploaded += 1
        print(f"uploaded {relpath} -> {key} ({sizes[relpath] // 1024} KB)")

    if not uploaded:
        print("every upload failed", file=sys.stderr)
        return 1

    # Merged, not replaced: the cards pass runs before any reel exists, so the
    # second invocation must not drop what the first recorded.
    data["media_keys"] = dict(sorted(keys.items()))
    data["media_bytes"] = dict(sorted(sizes.items()))
    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"recorded {uploaded} key(s) in {manifest} ({len(keys)} total)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
