"""One-off: purge cached MinerU output for low-resolution Documents so
stage1_mineru re-OCRs them with the new pre-clean step.

Stage1 skips Documents that already have a cached markdown
(scripts/ocr_with_mineru.py::stage1_mineru). After the pre-clean change
shipped, low-res docs cached before the change still hold their old
hallucinated output. Deleting their cache entries lets the next stage1
run pre-clean and re-OCR them.

Scans Documents in Neo4j, resolves each to a disk file, checks pixel
dimensions, and (with --apply) deletes the matching entries from:
  - pipeline/cache/mineru_markdown/<safe>.md
  - pipeline/cache/mineru_blocks/<safe>.json
  - pipeline/cache/mineru_markdown/<safe>.preclean  (sentinel)

Also nulls ``Document.markdown`` (and related provenance fields) in
Neo4j for low-res docs, so the next loader run picks up the freshly
pre-cleaned output without needing --force. Blocks edited by reviewers
are NOT touched -- only the markdown text + provenance fields, so the
review UI keeps any human annotations.

Default is dry-run. Pass --apply to actually delete.

Usage:
  python -m scripts._purge_lowres_cache              # dry-run; print plan
  python -m scripts._purge_lowres_cache --apply      # actually delete
"""
from __future__ import annotations

import argparse
from pathlib import Path

from PIL import Image

from api.neo4j_client import run_query, run_read_query
from pipeline.mineru import (
    MINERU_BLOCKS_DIR,
    MINERU_MARKDOWN_DIR,
    PRECLEAN_EXTS,
    PRECLEAN_LONG_EDGE_THRESHOLD,
    find_disk_path,
    preclean_sentinel_path,
    safe_cache_name,
)


def fetch_docs() -> list[dict]:
    return run_read_query(
        """
        MATCH (d:Document)
        WHERE d.filename IS NOT NULL AND d.file_path IS NOT NULL
        RETURN d.filename  AS filename,
               d.file_path AS file_path
        """,
        max_rows=50_000,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true",
                    help="actually delete cache files (default is dry-run)")
    args = ap.parse_args()

    docs = fetch_docs()
    print(f"Scanning {len(docs)} Documents against cache at:")
    print(f"  md:     {MINERU_MARKDOWN_DIR}")
    print(f"  blocks: {MINERU_BLOCKS_DIR}\n")

    scanned        = 0
    no_disk        = 0
    skipped_ext    = 0
    skipped_large  = 0
    no_md_cache    = 0
    no_md_neo4j    = 0
    purged         = 0
    purged_blocks  = 0
    purged_sentinel = 0
    open_failed   = 0
    lowres_fps: list[str] = []  # file_paths whose Neo4j markdown will be cleared

    for row in docs:
        scanned += 1
        filename = row["filename"]
        fp = row["file_path"]
        disk = find_disk_path(filename)
        if disk is None:
            no_disk += 1
            continue
        if disk.suffix.lower() not in PRECLEAN_EXTS:
            skipped_ext += 1
            continue
        try:
            with Image.open(disk) as im:
                w, h = im.size
        except Exception:
            open_failed += 1
            continue
        if max(w, h) >= PRECLEAN_LONG_EDGE_THRESHOLD:
            skipped_large += 1
            continue

        # Low-res Document confirmed. Track it for the Neo4j clear, even
        # if the local disk cache has no entry yet (most prod-synced docs
        # have markdown in Neo4j but never wrote to the local md cache).
        lowres_fps.append(fp)

        safe = safe_cache_name(fp)
        md_path     = MINERU_MARKDOWN_DIR / f"{safe}.md"
        blocks_path = MINERU_BLOCKS_DIR   / f"{safe}.json"
        sentinel    = preclean_sentinel_path(fp)

        has_local_md = md_path.exists()
        if not has_local_md:
            no_md_cache += 1

        action = "would clear" if not args.apply else "clearing"
        print(f"  [{w}x{h}] {action}: {filename}")
        if args.apply:
            if has_local_md:
                try:
                    md_path.unlink()
                    purged += 1
                except FileNotFoundError:
                    pass
            if blocks_path.exists():
                try:
                    blocks_path.unlink()
                    purged_blocks += 1
                except FileNotFoundError:
                    pass
            if sentinel.exists():
                try:
                    sentinel.unlink()
                    purged_sentinel += 1
                except FileNotFoundError:
                    pass
        else:
            if has_local_md:
                purged += 1
            if blocks_path.exists():
                purged_blocks += 1
            if sentinel.exists():
                purged_sentinel += 1

    # Clear Document.markdown in Neo4j for every low-res doc so the next
    # loader run writes the freshly pre-cleaned output without --force.
    # ``d.blocks`` is intentionally NOT touched: reviewers may have edited
    # blocks (rev > 0) and the next stage1 + loader pass will overwrite
    # them with fresh MinerU output anyway when --force isn't passed.
    if lowres_fps and args.apply:
        run_query(
            """
            UNWIND $fps AS fp
            MATCH (d:Document {file_path: fp})
            SET d.markdown            = NULL,
                d.markdown_source     = NULL,
                d.markdown_model      = NULL,
                d.markdown_loaded_at  = NULL,
                d.markdown_verified_at = NULL,
                d.markdown_verified_by = NULL,
                d.markdown_quality    = NULL
            """,
            {"fps": lowres_fps},
        )
        no_md_neo4j = len(lowres_fps)
    elif lowres_fps:
        no_md_neo4j = len(lowres_fps)

    verb = "Would purge" if not args.apply else "Purged"
    print(f"\n{verb}:")
    print(f"  disk md cache:        {purged}")
    print(f"  disk blocks cache:    {purged_blocks}")
    print(f"  disk preclean marker: {purged_sentinel}")
    print(f"  Neo4j Document.markdown cleared: {no_md_neo4j}")
    print(f"\nSkipped:")
    print(f"  source not on disk:        {no_disk}")
    print(f"  no disk md to delete:      {no_md_cache}  (Neo4j still cleared)")
    print(f"  not a raster image ext:    {skipped_ext}")
    print(f"  at or above threshold:     {skipped_large}")
    print(f"  could not open with PIL:   {open_failed}")
    print(f"\nTotal scanned: {scanned}")
    if not args.apply:
        print("\nDry-run only. Re-run with --apply to delete.")


if __name__ == "__main__":
    main()
