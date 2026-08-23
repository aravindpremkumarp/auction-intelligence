"""Batch OCR of TN notices into the markdown cache (MinerU API or Datalab).

Pipeline:
  notice file (jpg/png/pdf)
    -> MinerU API (https://mineru.net/api/v4) — vlm model, batched in groups of 20
       1. POST /file-urls/batch  -> batch_id + signed OSS upload URLs
       2. PUT file content to each signed URL
       3. Poll GET /extract-results/batch/<batch_id> until done
       4. Download full_zip_url, extract full.md
  (or Datalab, per DESCRIPTION_OCR_ENGINE)

Descriptions are NOT extracted here — LangExtract's full_description is the
sole automated description source (pipeline/apply_extractions.py).

Resumable:
  - MinerU markdown cached at pipeline/cache/mineru_markdown/<safe_path>.md
  - Re-runs skip cached entries

Usage:
  python -m scripts.ocr_with_mineru                 # full run
  python -m scripts.ocr_with_mineru --limit 50      # cap to first 50 Documents
  python -m scripts.ocr_with_mineru --missing-only  # only Documents with d.markdown IS NULL

Auth: MINERU_API_KEY in .env
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from concurrent.futures import ThreadPoolExecutor, as_completed

from api.neo4j_client import run_read_query
from pipeline.config import (
    DESCRIPTION_OCR_ENGINE, DATALAB_PIPELINE_CONCURRENCY, datalab_mode_for,
)
from pipeline.mineru import (
    MINERU_BLOCKS_DIR,
    MINERU_MARKDOWN_DIR as MINERU_MD_DIR,
    MINERU_SUPPORTED_EXTS,
    find_disk_path,
    mark_precleaned,
    preclean_if_needed,
    preclean_sentinel_path,
    safe_cache_name,
)
from pipeline.mineru_api import (
    download_and_cache as mineru_download_and_cache,
    poll as mineru_poll,
    request_batch as mineru_request_batch,
    upload_files as mineru_upload_files,
)


load_dotenv()
MINERU_KEY = os.environ.get("MINERU_API_KEY")

REPO_ROOT          = Path(__file__).resolve().parent.parent

MINERU_BATCH_SIZE = 10      # files per MinerU batch request
                            # (signed OSS URLs are short-lived; smaller
                            # batches reduce the chance of expiry mid-batch)


def chunked(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def fetch_all_work(missing_only: bool = False) -> list[dict]:
    """Every Document + its linked listings, with notice_type for routing.

    ``missing_only=True`` restricts the worklist to Documents whose
    ``markdown`` is NULL or empty — used for catch-up runs that should
    not re-OCR already-loaded notices.
    """
    where = ""
    if missing_only:
        where = "WHERE d.markdown IS NULL OR d.markdown = ''"
    return run_read_query(f"""
      MATCH (d:Document)
      {where}
      OPTIONAL MATCH (d)<-[:HAS_DOCUMENT]-(a:AuctionProperty)
      WITH d, collect(a.auction_id) AS aids
      RETURN d.filename       AS filename,
             d.file_path      AS file_path,
             coalesce(d.notice_type, 'unknown') AS notice_type,
             aids
    """, max_rows=10_000)


# ── MinerU stage ─────────────────────────────────────────────────────────────
# The four HTTP helpers (request_batch, upload_files, poll,
# download_and_cache) live in ``pipeline/mineru_api.py`` so the annotator's
# per-block re-extract path can reuse them. Each result zip now caches
# both ``full.md`` and the per-block content-list JSON.


def stage1_mineru(work: list[dict]) -> dict[str, str]:
    """{file_path: markdown}. Cache hits skip the API.

    The result zip carries both ``full.md`` and a per-block content-list
    JSON; both are cached on disk (see ``pipeline/mineru_api.download_and_cache``).
    Only the markdown is returned here — the loader reads the blocks JSON
    from disk when projecting into Neo4j.
    """
    md_by_path: dict[str, str] = {}
    items_to_call: list[dict] = []
    missing_disk = 0
    unsupported_ext = 0

    for w in work:
        cache_path = MINERU_MD_DIR / f"{safe_cache_name(w['file_path'])}.md"
        if cache_path.exists():
            md_by_path[w["file_path"]] = cache_path.read_text(encoding="utf-8")
            continue
        disk = find_disk_path(w["filename"])
        if disk is None:
            missing_disk += 1
            continue
        if disk.suffix.lower() not in MINERU_SUPPORTED_EXTS:
            unsupported_ext += 1
            continue
        items_to_call.append({**w, "disk_path": disk})

    print(f"  cached={len(md_by_path)}  to_call={len(items_to_call)}  "
          f"missing_disk={missing_disk}  unsupported_ext={unsupported_ext}")
    if not items_to_call:
        return md_by_path

    # Pre-clean low-resolution images before upload. Swaps disk_path to a
    # temp JPEG and tags the filename so the API-side name reflects the
    # transformation. The file_path / data_id stays canonical so the
    # downstream cache lands in the same key.
    preclean_tmps: list[Path] = []
    precleaned_fps: set[str] = set()
    for it in items_to_call:
        new_disk, was_pre = preclean_if_needed(it["disk_path"])
        if was_pre:
            it["disk_path"] = new_disk
            it["filename"] = Path(it["filename"]).stem + "_preclean.jpg"
            preclean_tmps.append(new_disk)
            precleaned_fps.add(it["file_path"])
    if precleaned_fps:
        print(f"  pre-cleaned {len(precleaned_fps)}/{len(items_to_call)} "
              f"low-res images (long edge < 1500 px)")

    batches = list(chunked(items_to_call, MINERU_BATCH_SIZE))
    try:
        for bi, batch in enumerate(batches, 1):
            print(f"\n  Batch {bi}/{len(batches)}: {len(batch)} files", flush=True)
            # Retry the whole batch up to 3 times for transient network errors.
            for attempt in range(3):
                try:
                    batch_id, urls = mineru_request_batch(batch)
                    print(f"    batch_id={batch_id}")
                    mineru_upload_files(batch, urls)
                    results = mineru_poll(batch_id)
                    break
                except (requests.exceptions.ConnectionError,
                        requests.exceptions.Timeout) as e:
                    wait = 5 * (attempt + 1)
                    print(f"    [transient] {type(e).__name__}, retry {attempt+1}/3 in {wait}s")
                    time.sleep(wait)
                    continue
                except Exception as e:
                    print(f"    [BATCH FAIL] {e}")
                    results = []
                    break
            else:
                print(f"    [BATCH FAIL] gave up after 3 retries")
                continue
            if not results:
                continue

            for r in results:
                data_id = r.get("data_id")
                state = r.get("state")
                match = next((it for it in batch if safe_cache_name(it["file_path"])[:128] == data_id), None)
                if match is None:
                    continue
                if state != "done":
                    print(f"    [{match['filename']}] state={state}  err={r.get('err_msg')}")
                    continue
                zip_url = r.get("full_zip_url")
                if zip_url is None:
                    continue
                # archive_to_r2: keep MinerU's complete output (full zip + image
                # crops) so nothing it emits is lost; loader stamps it on the Document.
                md_path, blocks_path = mineru_download_and_cache(
                    match["file_path"], zip_url, archive_to_r2=True)
                if md_path:
                    md_by_path[match["file_path"]] = md_path.read_text(encoding="utf-8")
                    blocks_note = " +blocks.json" if blocks_path else " (no blocks.json)"
                    pre_note = ""
                    if match["file_path"] in precleaned_fps:
                        mark_precleaned(match["file_path"])
                        pre_note = " +preclean"
                    else:
                        # If a prior pre-cleaned run cached this doc and
                        # the source was since re-OCR'd at full size,
                        # clear the stale marker so the loader doesn't
                        # mis-tag the new run.
                        stale = preclean_sentinel_path(match["file_path"])
                        if stale.exists():
                            stale.unlink()
                    print(f"    [{match['filename']}] -> {md_path.stat().st_size} bytes"
                          f"{blocks_note}{pre_note}")
    finally:
        for tmp in preclean_tmps:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    return md_by_path


# ── Datalab stage (default engine) ───────────────────────────────────────────
# Datalab replaces MinerU as the default OCR engine (DESCRIPTION_OCR_ENGINE).
# Unlike MinerU (batched), Datalab is one convert call per file, tier-routed by
# notice_type (single -> fast, multi -> accurate). Output is written to the same
# markdown/blocks cache the loader reads (see datalab_api.run_and_cache), so the
# LLM stage and Neo4j loader are unchanged.

def stage1_datalab(work: list[dict]) -> dict[str, str]:
    """{file_path: markdown} via Datalab. Cache hits skip the API.

    Mirrors ``stage1_mineru``'s cache / disk / extension gating, then runs the
    uncached notices through Datalab concurrently with per-notice tier routing.
    """
    from pipeline import datalab_api

    md_by_path: dict[str, str] = {}
    items_to_call: list[dict] = []
    missing_disk = 0
    unsupported_ext = 0

    for w in work:
        cache_path = MINERU_MD_DIR / f"{safe_cache_name(w['file_path'])}.md"
        if cache_path.exists():
            md_by_path[w["file_path"]] = cache_path.read_text(encoding="utf-8")
            continue
        disk = find_disk_path(w["filename"])
        if disk is None:
            missing_disk += 1
            continue
        if disk.suffix.lower() not in MINERU_SUPPORTED_EXTS:
            unsupported_ext += 1
            continue
        items_to_call.append({**w, "disk_path": disk})

    print(f"  [datalab] cached={len(md_by_path)}  to_call={len(items_to_call)}  "
          f"missing_disk={missing_disk}  unsupported_ext={unsupported_ext}")
    if not items_to_call:
        return md_by_path

    def _one(it: dict) -> tuple[str, str | None]:
        mode = datalab_mode_for(it.get("notice_type"))
        try:
            md_path, _bl = datalab_api.run_and_cache(
                it["file_path"], it["disk_path"], mode=mode)
            md = md_path.read_text(encoding="utf-8") if md_path else None
            print(f"    [{it['filename']}] datalab:{mode} -> "
                  f"{len(md or '')} chars")
            return it["file_path"], md
        except Exception as e:
            print(f"    [datalab-fail] {it['filename']}: {type(e).__name__}: {e}")
            return it["file_path"], None

    workers = max(1, DATALAB_PIPELINE_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for fut in as_completed([pool.submit(_one, it) for it in items_to_call]):
            fp, md = fut.result()
            if md:
                md_by_path[fp] = md
    return md_by_path


# ── Verification ─────────────────────────────────────────────────────────────

def print_summary():
    print("\n--- Final state ---")
    for row in run_read_query("""
        MATCH (a:AuctionProperty) RETURN a.description_source AS src, count(*) AS n ORDER BY src
    """):
        print(f"  source={str(row['src']):<20} {row['n']:>5}")
    md_count     = len(list(MINERU_MD_DIR.glob("*.md")))      if MINERU_MD_DIR.exists()     else 0
    blocks_count = len(list(MINERU_BLOCKS_DIR.glob("*.json"))) if MINERU_BLOCKS_DIR.exists() else 0
    print(f"\n  mineru_markdown cache: {md_count} files")
    print(f"  mineru_blocks cache:   {blocks_count} files")


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, default=None,
                        help="Cap to first N Documents (staged rollout)")
    parser.add_argument("--skip-mineru", action="store_true",
                        help="Skip MinerU stage; reuse cached markdowns only")
    parser.add_argument("--missing-only", action="store_true",
                        help="Only Documents whose d.markdown is NULL/empty")
    args = parser.parse_args()

    if not MINERU_KEY:
        sys.exit("MINERU_API_KEY not set in .env")

    work = fetch_all_work(missing_only=args.missing_only)
    if args.limit:
        work = work[:args.limit]
    by_type = {}
    for w in work:
        by_type[w["notice_type"]] = by_type.get(w["notice_type"], 0) + 1
    print(f"Worklist: {len(work)} Documents  notice_type={by_type}")

    print("\n[Stage 1] MinerU OCR (markdown cache)")
    if args.skip_mineru:
        print("  --skip-mineru: loading cached markdowns only")
        mds: dict[str, str] = {}
        for w in work:
            cache_path = MINERU_MD_DIR / f"{safe_cache_name(w['file_path'])}.md"
            if cache_path.exists():
                mds[w["file_path"]] = cache_path.read_text(encoding="utf-8")
        print(f"  loaded {len(mds)} cached markdowns")
    elif DESCRIPTION_OCR_ENGINE == "datalab":
        print(f"  engine=datalab (single->{datalab_mode_for('single')}, "
              f"multi->{datalab_mode_for('multi')})")
        mds = stage1_datalab(work)
    else:
        print("  engine=mineru")
        mds = stage1_mineru(work)
    print(f"  markdowns available: {len(mds)}")

    print_summary()


if __name__ == "__main__":
    main()
