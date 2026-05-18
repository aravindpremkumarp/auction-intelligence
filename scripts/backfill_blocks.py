"""Populate ``Document.blocks`` for every Document missing it.

Two cost tiers:

  1. ``Document`` has a cached ``pipeline/cache/mineru_blocks/<safe>.json``
     -> parse + write to Neo4j. **Free.**
  2. Cache is missing -> with ``--reingest``, re-run MinerU for that one
     file via the cloud API. **Costs one MinerU call per document.**

Usage::

    python -m scripts.backfill_blocks                     # cache-only path
    python -m scripts.backfill_blocks --reingest          # also re-OCR missing
    python -m scripts.backfill_blocks --reingest --limit 10
    python -m scripts.backfill_blocks --dry-run

Idempotent: documents that already have ``d.blocks`` set are skipped.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from api.neo4j_client import run_query, run_read_query
from pipeline.load_markdowns_to_neo4j import load_blocks_for
from pipeline.mineru import (
    MINERU_BLOCKS_DIR,
    MINERU_SUPPORTED_EXTS,
    find_disk_path,
    safe_cache_name,
)


def fetch_pending(limit: int | None) -> list[dict]:
    cypher = """
        MATCH (d:Document)
        WHERE d.file_path IS NOT NULL AND d.file_path <> ''
          AND d.blocks IS NULL
        RETURN d.filename  AS filename,
               d.file_path AS file_path
        ORDER BY d.markdown_loaded_at DESC
    """
    rows = run_read_query(cypher, max_rows=20_000)
    return rows[: limit] if limit else rows


def write_blocks(file_path: str, blocks: list[dict]) -> None:
    payload = json.dumps({"schema_version": 1, "blocks": blocks},
                         ensure_ascii=False)
    run_query(
        """
        MATCH (d:Document {file_path: $file_path})
        SET d.blocks          = $blocks_json,
            d.blocks_revision = coalesce(d.blocks_revision, 0)
        """,
        {"file_path": file_path, "blocks_json": payload},
    )


def reingest_one(filename: str, file_path: str) -> list[dict] | None:
    """Call MinerU on a single file and return the parsed block list.

    Returns ``None`` when the source disk file is missing or MinerU fails.
    """
    disk = find_disk_path(filename)
    if disk is None:
        print(f"    [no-disk] {filename}")
        return None
    if disk.suffix.lower() not in MINERU_SUPPORTED_EXTS:
        print(f"    [bad-ext] {filename}")
        return None

    from pipeline.mineru_api import (
        download_and_cache, poll, request_batch, upload_files,
    )
    item = {"filename": filename, "file_path": file_path, "disk_path": disk}
    try:
        batch_id, urls = request_batch([item])
        upload_files([item], urls)
        results = poll(batch_id, timeout_s=600)
    except Exception as e:
        print(f"    [mineru-fail] {filename}: {e}")
        return None
    if not results or results[0].get("state") != "done":
        print(f"    [mineru-state] {filename}: "
              f"{results[0].get('state') if results else 'no-rows'}")
        return None
    zip_url = results[0].get("full_zip_url")
    if not zip_url:
        return None
    md_path, blocks_path = download_and_cache(file_path, zip_url)
    if blocks_path is None:
        print(f"    [no-blocks-json] {filename}")
        return None
    return load_blocks_for(file_path)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reingest", action="store_true",
                    help="re-call MinerU for documents whose blocks cache "
                         "is missing (costs API quota)")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N documents")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pending = fetch_pending(args.limit)
    print(f"{len(pending)} Documents missing d.blocks")

    from_cache = 0
    from_api   = 0
    skipped    = 0
    failed     = 0

    for i, row in enumerate(pending, 1):
        fp = row["file_path"]
        fn = row["filename"]
        cached = MINERU_BLOCKS_DIR / f"{safe_cache_name(fp)}.json"

        blocks: list[dict] | None
        if cached.exists():
            blocks = load_blocks_for(fp)
            origin = "cache"
        elif args.reingest:
            blocks = reingest_one(fn, fp)
            origin = "api"
        else:
            skipped += 1
            continue

        if not blocks:
            failed += 1
            continue

        if args.dry_run:
            print(f"  [{i}] DRY {origin}: {fn} -> {len(blocks)} blocks")
        else:
            try:
                write_blocks(fp, blocks)
                if origin == "cache":
                    from_cache += 1
                else:
                    from_api += 1
                print(f"  [{i}] {origin}: {fn} -> {len(blocks)} blocks")
            except Exception as e:
                failed += 1
                print(f"  [{i}] write-fail: {fn}: {e}")

        # Throttle MinerU re-ingests so we don't pound their queue.
        if origin == "api":
            time.sleep(1)

    print(f"\nDone. from_cache={from_cache}  from_api={from_api}  "
          f"skipped_no_cache={skipped}  failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
