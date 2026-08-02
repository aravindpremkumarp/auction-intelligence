#!/usr/bin/env python3
"""
scripts/upload_reels_to_r2.py
-----------------------------
Upload a batch's rendered reel MP4s to the private R2 bucket and record their
keys back into the batch's ``drafts.json``.

Why: ``marketing/render_reel.py`` writes MP4s to a scratch directory and the
content-poster workflow used to publish them only as a 14-day GitHub artifact.
Two weeks later the batch still had its captions and card PNGs committed but no
reviewable video at all — and MP4s can't be committed (a handful per run would
grow the repo forever). Uploading to R2 and storing the key in the manifest
makes the video outlive the run without putting binaries in git.

The bucket is the PRIVATE one: a staged reel is unpublished pre-release
material, so it must not be fetchable by URL. api/social re-serves it behind
the same admin gate as the rest of the batch.

Usage:
    python scripts/upload_reels_to_r2.py \
        --batch marketing/outputs/2026-07-15 \
        --renders .reel_renders

Exit codes: 0 = uploaded (or nothing to do), 2 = R2 not configured (the caller
should fall back to the workflow artifact), 1 = an upload actually failed.
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


def plan_uploads(data: dict, batch_date: str, renders_dir: Path) -> list[tuple[dict, Path, str]]:
    """(manifest row, local mp4, r2 key) for every reel that actually rendered.

    `data` is the parsed drafts.json, and the returned rows are the live dicts
    inside it — the caller records each key straight onto the row and writes the
    same object back.

    Rows whose MP4 is missing are skipped rather than failing the run: reel
    rendering is best-effort in the workflow, so a partial render is normal and
    must still upload the reels that did succeed.
    """
    jobs: list[tuple[dict, Path, str]] = []
    for row in data.get("reels") or []:
        stem = Path(str(row.get("data") or "")).stem
        if not stem:
            continue
        mp4 = renders_dir / f"{stem}.mp4"
        if not mp4.is_file():
            print(f"skip {stem}: no {mp4}")
            continue
        jobs.append((row, mp4, storage.reel_object_key(batch_date, stem)))
    return jobs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--batch", required=True,
                    help="batch directory, e.g. marketing/outputs/2026-07-15")
    ap.add_argument("--renders", default=".reel_renders",
                    help="directory holding the rendered <stem>.mp4 files")
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
        # R2_BUCKET / R2_PUBLIC_BASE_URL present. Check it up front rather than
        # discovering it once per upload — exit 2 is what tells the workflow to
        # fall back to the artifact.
        storage._require_config()
    except storage.R2ConfigError as exc:
        print(f"R2 not configured ({exc}) — leaving the manifest untouched")
        return 2

    data = json.loads(manifest.read_text(encoding="utf-8"))
    jobs = plan_uploads(data, batch_dir.name, Path(args.renders))
    if not jobs:
        print("no rendered reels to upload")
        return 0

    uploaded = 0
    for row, mp4, key in jobs:
        stem = Path(str(row.get("data") or "")).stem
        try:
            storage.upload_file_private(mp4, key, content_type="video/mp4")
        except Exception as exc:  # noqa: BLE001 - report every failure, keep going
            print(f"FAILED {stem} -> {key}: {exc}", file=sys.stderr)
            continue
        row["video_key"] = key
        row["video_bytes"] = mp4.stat().st_size
        uploaded += 1
        print(f"uploaded {stem} -> {key} ({row['video_bytes'] // 1024} KB)")

    if not uploaded:
        print("every upload failed", file=sys.stderr)
        return 1

    manifest.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print(f"recorded {uploaded} reel key(s) in {manifest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
