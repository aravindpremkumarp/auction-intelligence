"""
scripts/upload_tn_to_r2.py
---------------------------
Uploads all files from downloads/tn_properties/ to Cloudflare R2.
Key format: notices/<auction_id>/<filename>

Auction ID is looked up from tn_auction_data.jsonl by matching the filename
inside downloads_list. Falls back to 'tn_unknown' if not matched.

Run:
    python scripts/upload_tn_to_r2.py           # live upload
    python scripts/upload_tn_to_r2.py --dry-run # preview only
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

import boto3
from botocore.exceptions import ClientError

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_ID   = os.environ["R2_ACCOUNT_ID"]
ACCESS_KEY   = os.environ["R2_ACCESS_KEY_ID"]
SECRET_KEY   = os.environ["R2_SECRET_ACCESS_KEY"]
BUCKET       = os.environ["R2_BUCKET"]
PUBLIC_BASE  = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")

TN_DIR       = ROOT / "downloads" / "tn_properties"
JSONL_FILE   = ROOT / "data" / "tn_auction_data.jsonl"

# ── Build filename → auction_id map from JSONL ────────────────────────────────
def build_filename_map() -> dict[str, str]:
    """filename -> the auction_id whose folder the file is stored under.

    A batch notice is referenced by every property it covers, so this is a
    many-to-one choice. It used to be last-writer-wins, which made the key
    depend on JSONL record order: regenerating the file moved shared notices
    to a different key, the HEAD check missed, and a byte-identical copy was
    uploaded beside the old one. Lowest auction_id wins instead, so the same
    input always yields the same key.
    """
    mapping: dict[str, str] = {}
    with open(JSONL_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            aid = rec.get("auction_id", "tn_unknown")
            for dl in rec.get("downloads_list") or []:
                fname = (dl or "").strip()
                if fname and fname.upper() != "N/A":
                    prev = mapping.get(fname)
                    if prev is None or _aid_sort_key(aid) < _aid_sort_key(prev):
                        mapping[fname] = aid
    return mapping


def _aid_sort_key(aid: str) -> tuple[int, object]:
    """Numeric auction_ids sort numerically; anything else sorts after, by text."""
    return (0, int(aid)) if str(aid).isdigit() else (1, str(aid))


def list_existing_keys(client) -> set[str]:
    """Every object key already under notices/. One paginated pass."""
    keys: set[str] = set()
    for page in client.get_paginator("list_objects_v2").paginate(
            Bucket=BUCKET, Prefix="notices/"):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def plan_uploads(filenames, fname_map, existing_keys):
    """Decide, per file, whether to upload it and under which key.

    Returns [(filename, key, action)] with action 'reuse' or 'upload'.

    If any copy of this filename is already in R2 -- under ANY auction_id --
    reuse that key. That is what stops a shared notice being stored once per
    property, and it makes the script idempotent across JSONL rebuilds.
    """
    by_name: dict[str, str] = {}
    for k in sorted(existing_keys):          # sorted -> deterministic winner
        by_name.setdefault(k.rsplit("/", 1)[-1], k)

    plan = []
    for fname in filenames:
        hit = by_name.get(fname)
        if hit:
            plan.append((fname, hit, "reuse"))
            continue
        aid = fname_map.get(fname, "tn_unknown")
        plan.append((fname, f"notices/{aid}/{fname}", "upload"))
    return plan

# ── R2 helpers ────────────────────────────────────────────────────────────────
def make_client():
    return boto3.client(
        "s3",
        endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
        aws_access_key_id=ACCESS_KEY,
        aws_secret_access_key=SECRET_KEY,
        region_name="auto",
    )

def object_exists(client, key: str) -> bool:
    try:
        client.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("404", "NoSuchKey"):
            return False
        raise

def guess_content_type(filename: str) -> str:
    ct, _ = mimetypes.guess_type(filename)
    return ct or "application/octet-stream"

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview uploads without touching R2.")
    args = parser.parse_args()

    print("Building filename -> auction_id map from tn_auction_data.jsonl...")
    fname_map = build_filename_map()
    print(f"  {len(fname_map):,} filenames mapped\n")

    files = sorted(TN_DIR.iterdir())
    files = [f for f in files if f.is_file()]
    print(f"Files in tn_properties/: {len(files):,}")

    client = make_client()

    print("Listing existing R2 objects...")
    existing_keys = list_existing_keys(client)
    print(f"  {len(existing_keys):,} objects already under notices/\n")

    by_path = {f.name: f for f in files}
    plan = plan_uploads([f.name for f in files], fname_map, existing_keys)

    uploaded      = 0
    already_in_r2 = sum(1 for _, _, action in plan if action == "reuse")
    errors        = 0

    for fname, key, action in plan:
        if action == "reuse":
            continue
        if args.dry_run:
            print(f"  [dry-run] {key}  ({guess_content_type(fname)})")
            uploaded += 1
            continue
        try:
            with open(by_path[fname], "rb") as fp:
                client.put_object(
                    Bucket=BUCKET,
                    Key=key,
                    Body=fp,
                    ContentType=guess_content_type(fname),
                )
            uploaded += 1
            print(f"  [uploaded] {key}")
        except Exception as e:
            errors += 1
            print(f"  [ERROR] {fname}: {e}")

    print(f"\n{'='*55}")
    print(f"  Total files     : {len(files):,}")
    print(f"  Uploaded        : {uploaded:,}")
    print(f"  Already in R2   : {already_in_r2:,}")
    print(f"  Errors          : {errors}")
    print(f"{'='*55}")
    print(f"\nPublic base URL : {PUBLIC_BASE}/notices/")


if __name__ == "__main__":
    main()
