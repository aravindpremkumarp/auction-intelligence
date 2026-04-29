"""
scripts/upload_downloads_to_r2.py
---------------------------------
Backfill and forward-fill auction sales-notice downloads to Cloudflare R2.

For every AuctionProperty that has a non-empty ``downloads_list``, this
script:

1. Locates each listed file under ``pipeline/config.DOWNLOADS_DIR``.
2. Uploads it to R2 at ``notices/{auction_id}/{filename}`` (idempotent —
   HEAD check skips already-uploaded objects).
3. Upserts a ``:Document`` node with ``storage_key``, ``public_url``,
   ``content_type``, ``doc_type``, ``uploaded_at`` and links it to the
   property via ``[:HAS_DOCUMENT]``. If the enrichment pipeline already
   created a Document with the same filename, that node is updated in
   place; otherwise a new one is created keyed by its R2 storage_key.

Run standalone:
    python -m scripts.upload_downloads_to_r2 --dry-run
    python -m scripts.upload_downloads_to_r2
    python -m scripts.upload_downloads_to_r2 --auction-id AUC123
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

# Support running as a module (python -m scripts.upload_downloads_to_r2) and
# as a plain script from anywhere.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline import storage
from pipeline.config import DOWNLOADS_DIR
from api.neo4j_client import run_query

# The scraper has written files to a few layouts over time. Search each
# known location so the script works against any historical dataset.
_DOWNLOAD_SEARCH_DIRS = [
    DOWNLOADS_DIR / "live_properties",
    DOWNLOADS_DIR / "tn_properties",
    DOWNLOADS_DIR,
]

FETCH_CYPHER = """
MATCH (a:AuctionProperty)
WHERE a.downloads_list IS NOT NULL AND size(a.downloads_list) > 0
  AND ($auction_id IS NULL OR a.auction_id = $auction_id)
RETURN a.auction_id AS auction_id, a.downloads_list AS downloads_list
"""

# Single MERGE on the relationship + filename: stable across machines and
# pipeline re-runs, so we never create a duplicate :Document node for the
# same (auction_id, filename) pair (issue #45). file_path is preserved if
# the enrichment pipeline already populated it; otherwise we fall back to
# the R2 storage_key so the field is never null.
UPSERT_DOC_CYPHER = """
MATCH (a:AuctionProperty {auction_id: $auction_id})
MERGE (a)-[:HAS_DOCUMENT]->(doc:Document {filename: $filename})
SET doc.storage_key  = $storage_key,
    doc.public_url   = $public_url,
    doc.content_type = $content_type,
    doc.doc_type     = $doc_type,
    doc.file_path    = coalesce(doc.file_path, $storage_key),
    doc.uploaded_at  = datetime()
"""


@dataclass
class UploadResult:
    uploaded: int = 0
    skipped_exists: int = 0
    skipped_missing: int = 0
    graph_updates: int = 0
    errors: int = 0


def locate_local_file(filename: str) -> Path | None:
    for base in _DOWNLOAD_SEARCH_DIRS:
        candidate = base / filename
        if candidate.is_file():
            return candidate
    return None


def upsert_document(
    *,
    auction_id: str,
    filename: str,
    storage_key: str,
    public_url: str,
    content_type: str,
    doc_type: str,
) -> None:
    """Idempotently upsert a :Document node keyed by (auction_id, filename)."""
    run_query(UPSERT_DOC_CYPHER, {
        "auction_id":   auction_id,
        "filename":     filename,
        "storage_key":  storage_key,
        "public_url":   public_url,
        "content_type": content_type,
        "doc_type":     doc_type,
    })


def process_auction(
    auction_id: str,
    filenames: list[str],
    *,
    dry_run: bool,
    result: UploadResult,
) -> None:
    for raw in filenames:
        filename = (raw or "").strip()
        if not filename or filename.upper() == "N/A":
            continue

        local = locate_local_file(filename)
        if local is None:
            print(f"  [missing] {auction_id} :: {filename}")
            result.skipped_missing += 1
            continue

        key = storage.object_key(auction_id, filename)
        content_type = storage.guess_content_type(filename)
        doc_type = storage.doc_type_from_content_type(content_type)

        if dry_run:
            print(f"  [dry-run] would upload {local} -> {key} ({content_type})")
            continue

        try:
            if storage.exists(key):
                public_url = storage.public_url_for(key)
                result.skipped_exists += 1
                action = "exists"
            else:
                public_url = storage.upload_file(local, key, content_type)
                result.uploaded += 1
                action = "uploaded"

            upsert_document(
                auction_id=auction_id,
                filename=filename,
                storage_key=key,
                public_url=public_url,
                content_type=content_type,
                doc_type=doc_type,
            )
            result.graph_updates += 1
            print(f"  [{action}] {auction_id} :: {filename} -> {public_url}")
        except Exception as e:
            result.errors += 1
            print(f"  [error] {auction_id} :: {filename}: {e}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="List intended uploads without touching R2 or Neo4j.")
    parser.add_argument("--auction-id", default=None,
                        help="Only process this single auction_id (handy for testing).")
    args = parser.parse_args()

    if not args.dry_run:
        # Fail fast if R2 isn't configured rather than halfway through.
        try:
            storage.r2_client()
        except storage.R2ConfigError as e:
            sys.exit(f"R2 is not configured: {e}")

    properties = run_query(FETCH_CYPHER, {"auction_id": args.auction_id})
    if not properties:
        print("No properties with downloads_list found.")
        return

    print(f"Processing {len(properties)} auction properties...")
    result = UploadResult()
    for row in properties:
        process_auction(
            auction_id=row["auction_id"],
            filenames=row["downloads_list"] or [],
            dry_run=args.dry_run,
            result=result,
        )

    print("\n" + "=" * 50)
    print(f"  Uploaded       : {result.uploaded}")
    print(f"  Already in R2  : {result.skipped_exists}")
    print(f"  Missing locally: {result.skipped_missing}")
    print(f"  Graph upserts  : {result.graph_updates}")
    print(f"  Errors         : {result.errors}")
    print("=" * 50)


if __name__ == "__main__":
    main()
