"""
cleanup_orphan_r2_objects.py
----------------------------
Delete R2 objects whose parent ``AuctionProperty`` no longer exists in Neo4j.

Object key shape is ``notices/{auction_id}/{filename}`` (see
pipeline/storage.py).  When an auction is removed from the graph — e.g. by
scripts/remove_non_property_categories.py — the files in R2 are left behind
as orphans. This script finds and deletes them.

Algorithm:
  1. List every key under ``notices/`` in the R2 bucket (paginated).
  2. Group by the ``auction_id`` path segment.
  3. Ask Neo4j which of those auction_ids still exist.
  4. For each auction_id that's gone, delete all of its R2 objects.

Idempotent. Dry-run by default — pass ``--apply`` to actually delete.

Run:
    python -m scripts.cleanup_orphan_r2_objects                 # dry run
    python -m scripts.cleanup_orphan_r2_objects --apply         # delete
"""
from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import storage
from api.neo4j_client import run_query

LIVE_AUCTIONS_CYPHER = """
UNWIND $auction_ids AS aid
MATCH (a:AuctionProperty {auction_id: aid})
RETURN a.auction_id AS auction_id
"""


def list_notice_keys() -> dict[str, list[str]]:
    """Return {auction_id: [keys]} for every object under ``notices/`` in R2."""
    client = storage.r2_client()
    paginator = client.get_paginator("list_objects_v2")

    by_auction: dict[str, list[str]] = defaultdict(list)
    for page in paginator.paginate(Bucket=storage.R2_BUCKET, Prefix="notices/"):
        for obj in page.get("Contents", []) or []:
            key = obj["Key"]
            parts = key.split("/", 2)
            if len(parts) < 3 or parts[0] != "notices":
                continue
            by_auction[parts[1]].append(key)
    return dict(by_auction)


def live_auction_ids(candidate_ids: list[str]) -> set[str]:
    """Subset of ``candidate_ids`` that still have an AuctionProperty node."""
    if not candidate_ids:
        return set()
    rows = run_query(LIVE_AUCTIONS_CYPHER, {"auction_ids": candidate_ids})
    return {row["auction_id"] for row in rows}


def delete_keys(keys: list[str]) -> int:
    """Delete keys in batches of 1000 (R2 / S3 limit). Returns deleted count."""
    client = storage.r2_client()
    deleted = 0
    for i in range(0, len(keys), 1000):
        batch = keys[i : i + 1000]
        resp = client.delete_objects(
            Bucket=storage.R2_BUCKET,
            Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
        )
        for err in resp.get("Errors", []) or []:
            print(f"  [error] {err.get('Key')}: {err.get('Message')}")
        deleted += len(batch) - len(resp.get("Errors", []) or [])
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete orphan objects (default is dry-run).")
    args = parser.parse_args()

    try:
        storage.r2_client()
    except storage.R2ConfigError as e:
        sys.exit(f"R2 is not configured: {e}")

    print("Listing notices/ keys in R2...")
    by_auction = list_notice_keys()
    print(f"  {len(by_auction)} distinct auction_ids, "
          f"{sum(len(v) for v in by_auction.values())} total objects")

    print("Checking Neo4j for live auctions...")
    live = live_auction_ids(list(by_auction.keys()))
    orphan_ids = sorted(set(by_auction) - live)
    orphan_keys = [k for aid in orphan_ids for k in by_auction[aid]]

    print(f"  {len(orphan_ids)} orphan auction_ids, {len(orphan_keys)} orphan objects")

    if not orphan_keys:
        print("Nothing to clean up.")
        return

    sample = orphan_keys[:5]
    print(f"  e.g. {sample}{' ...' if len(orphan_keys) > 5 else ''}")

    if not args.apply:
        print("\nDry run — pass --apply to delete.")
        return

    print("\nDeleting...")
    deleted = delete_keys(orphan_keys)
    print(f"Deleted {deleted}/{len(orphan_keys)} objects.")


if __name__ == "__main__":
    main()
