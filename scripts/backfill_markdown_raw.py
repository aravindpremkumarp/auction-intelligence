"""Populate Document.markdown_raw + blocks_raw from the on-disk MinerU cache.

Durability backfill: copies the raw full.md and content_list.json that the
pipeline already cached on disk onto each Document, so a reviewer edit can never
lose the original MinerU output. Free — reads the cache, no MinerU calls. See
docs/superpowers/specs/2026-06-03-durable-raw-mineru-output-design.md.

Usage::

    python -m scripts.backfill_markdown_raw                 # only docs missing raw
    python -m scripts.backfill_markdown_raw --force         # overwrite existing
    python -m scripts.backfill_markdown_raw --limit 50
    python -m scripts.backfill_markdown_raw --dry-run

Idempotent: skips Documents that already have markdown_raw unless --force.
"""
from __future__ import annotations

import argparse
import sys

from api.neo4j_client import run_query, run_read_query
from pipeline.load_markdowns_to_neo4j import read_raw_artifacts


def fetch_pending(limit: int | None, force: bool) -> list[dict]:
    cond = "" if force else "AND d.markdown_raw IS NULL"
    cypher = f"""
        MATCH (d:Document)
        WHERE d.file_path IS NOT NULL AND d.file_path <> ''
          {cond}
        RETURN d.filename  AS filename,
               d.file_path AS file_path
        ORDER BY d.markdown_loaded_at DESC
    """
    rows = run_read_query(cypher, max_rows=20_000, timeout=120.0)
    return rows[:limit] if limit else rows


def write_raw(file_path: str, markdown_raw: str, blocks_raw: str | None) -> None:
    run_query(
        """
        MATCH (d:Document {file_path: $file_path})
        SET d.markdown_raw    = $markdown_raw,
            d.blocks_raw      = coalesce($blocks_raw, d.blocks_raw),
            d.markdown_raw_at = datetime()
        """,
        {"file_path": file_path, "markdown_raw": markdown_raw,
         "blocks_raw": blocks_raw},
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite Documents that already have markdown_raw")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N documents")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    pending = fetch_pending(args.limit, args.force)
    print(f"{len(pending)} Documents to consider")

    wrote = 0
    no_cache = 0
    failed = 0
    for i, row in enumerate(pending, 1):
        fp = row["file_path"]
        md_raw, bl_raw = read_raw_artifacts(fp)
        if md_raw is None:
            no_cache += 1
            continue
        if args.dry_run:
            blen = "-" if bl_raw is None else len(bl_raw)
            print(f"  [{i}] DRY {row['filename']}: md={len(md_raw)}B blocks={blen}B")
            wrote += 1
            continue
        try:
            write_raw(fp, md_raw, bl_raw)
            wrote += 1
            if i % 200 == 0:
                print(f"  [{i}/{len(pending)}] wrote={wrote} no_cache={no_cache}")
        except Exception as e:
            failed += 1
            print(f"  [{i}] write-fail {row['filename']}: {e}")

    verb = "would_write" if args.dry_run else "wrote"
    print(f"\nDone. {verb}={wrote}  no_cache={no_cache}  failed={failed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
