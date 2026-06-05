"""Backfill MinerU markdown for Documents that lack it.

Targets only :Document nodes where ``d.markdown IS NULL`` or empty —
the existing 2,450 markdowns are untouched. Downloads each missing
notice from its R2 ``public_url`` into ``downloads/tn_properties/``
so the MinerU helpers can find it, then reuses ``stage1_mineru`` from
``scripts.ocr_with_mineru`` to OCR in batches of 20 and cache the
results under ``pipeline/cache/mineru_markdown/``. Finally writes the
new markdowns into Neo4j via the same write path as
``pipeline.load_markdowns_to_neo4j`` (with provenance stamping).

Idempotent: a re-run skips Documents that now have markdown, and the
MinerU helpers skip files whose cache file already exists.

Usage:
  python -m scripts.ocr_missing_markdowns                 # run it
  python -m scripts.ocr_missing_markdowns --dry-run       # list only
  python -m scripts.ocr_missing_markdowns --limit 10      # cap N

Auth: MINERU_API_KEY in .env (paid). Network egress to R2 + MinerU.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline.config import DOWNLOADS_DIR
from pipeline.load_markdowns_to_neo4j import (
    DEFAULT_MARKDOWN_MODEL,
    DEFAULT_MARKDOWN_SOURCE,
    read_raw_artifacts,
    write_markdowns,
)
from pipeline.mineru import MINERU_SUPPORTED_EXTS
from scripts.ocr_with_mineru import MINERU_KEY, stage1_mineru


load_dotenv()
DOWNLOAD_TARGET_DIR = DOWNLOADS_DIR / "tn_properties"


def fetch_missing() -> list[dict]:
    """Documents whose markdown is null/empty but that have a public_url
    we can fetch the source file from."""
    return run_read_query(
        """
        MATCH (d:Document)
        WHERE (d.markdown IS NULL OR d.markdown = '')
          AND d.public_url IS NOT NULL AND d.public_url <> ''
          AND d.filename IS NOT NULL AND d.filename <> ''
          AND d.file_path IS NOT NULL AND d.file_path <> ''
        RETURN d.filename   AS filename,
               d.file_path  AS file_path,
               d.public_url AS public_url
        """,
        max_rows=10_000,
    )


def download_file(url: str, dest: Path, timeout: int = 60) -> bool:
    """Fetch ``url`` into ``dest`` with two retries on transient errors.
    Returns True on success (or if file already exists)."""
    if dest.exists() and dest.stat().st_size > 0:
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=timeout, stream=True)
            if not r.ok:
                print(f"    [{r.status_code}] {url}")
                return False
            tmp = dest.with_suffix(dest.suffix + ".part")
            with tmp.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        fh.write(chunk)
            tmp.rename(dest)
            return True
        except requests.exceptions.RequestException as e:
            if attempt < 2:
                wait = 2 ** attempt * 3
                print(f"    [retry {attempt + 1}] {type(e).__name__}: {e}; waiting {wait}s")
                time.sleep(wait)
            else:
                print(f"    [GAVE UP] {url}: {e}")
    return False


def build_write_rows(mds: dict[str, str]) -> list[dict]:
    """Build ``write_markdowns`` rows from ``{file_path: markdown}``.

    Attaches the durable raw artifacts that stage1 just cached on disk so
    documents OCR'd through this script also get ``markdown_raw`` /
    ``blocks_raw`` (the same fields ``load_markdowns_to_neo4j.main`` writes).
    ``markdown_raw`` is the markdown we're writing — it *is* the raw
    ``full.md`` MinerU returned; ``blocks_raw`` is the verbatim
    ``content_list.json`` read from the on-disk cache.
    """
    rows: list[dict] = []
    for fp, md in mds.items():
        if not (md and md.strip()):
            continue
        _, blocks_raw = read_raw_artifacts(fp)
        rows.append({
            "file_path":    fp,
            "markdown":     md,
            "markdown_raw": md,
            "blocks_raw":   blocks_raw,
        })
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be processed; no downloads, no OCR")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N missing Documents")
    args = ap.parse_args()

    missing = fetch_missing()
    if args.limit:
        missing = missing[:args.limit]

    print(f"Missing markdown: {len(missing)} Documents")
    if not missing:
        print("nothing to do")
        return 0

    if args.dry_run:
        for m in missing:
            print(f"  {m['filename']}  <- {m['public_url']}")
        return 0

    if not MINERU_KEY:
        sys.exit("MINERU_API_KEY not set in .env")

    # ── Stage 0: download source files from R2 ──────────────────────────────
    print(f"\n[Stage 0] Downloading source files into {DOWNLOAD_TARGET_DIR}")
    downloaded: list[dict] = []
    skipped_unsupported = 0
    download_failed = 0
    for i, m in enumerate(missing, 1):
        filename = m["filename"]
        ext = Path(filename).suffix.lower()
        if ext not in MINERU_SUPPORTED_EXTS:
            skipped_unsupported += 1
            print(f"  [{i}/{len(missing)}] skip (ext={ext}) {filename}")
            continue
        dest = DOWNLOAD_TARGET_DIR / filename
        if download_file(m["public_url"], dest):
            downloaded.append(m)
        else:
            download_failed += 1
        if i % 25 == 0 or i == len(missing):
            print(f"  [{i}/{len(missing)}] downloaded={len(downloaded)} "
                  f"failed={download_failed} unsupported={skipped_unsupported}",
                  flush=True)

    if not downloaded:
        print("\nNo files available to OCR — nothing to do.")
        return 1

    # ── Stage 1: MinerU OCR ─────────────────────────────────────────────────
    print(f"\n[Stage 1] MinerU OCR on {len(downloaded)} files")
    work = [{"filename": m["filename"], "file_path": m["file_path"]}
            for m in downloaded]
    mds = stage1_mineru(work)
    print(f"  MinerU returned markdown for {len(mds)} / {len(work)} files")

    # ── Stage 2: write markdowns to Neo4j ───────────────────────────────────
    if not mds:
        print("\nNo markdown produced — nothing to write.")
        return 1

    rows = build_write_rows(mds)
    print(f"\n[Stage 2] Writing {len(rows)} markdowns to Neo4j")
    if rows:
        # Write in batches of 200 like the loader does.
        for i in range(0, len(rows), 200):
            batch = rows[i:i + 200]
            write_markdowns(batch, DEFAULT_MARKDOWN_SOURCE, DEFAULT_MARKDOWN_MODEL)
            print(f"  wrote {min(i + 200, len(rows))} / {len(rows)}", flush=True)

    # ── Stage 3: final tally ────────────────────────────────────────────────
    final = run_read_query(
        """
        MATCH (d:Document)
        RETURN count(d) AS total,
               sum(CASE WHEN d.markdown IS NOT NULL AND d.markdown <> ''
                        THEN 1 ELSE 0 END) AS with_md
        """,
        max_rows=1,
    )
    if final:
        t, w = final[0]["total"], final[0]["with_md"]
        print(f"\nFinal: {w} / {t} Documents have markdown "
              f"({t - w} still missing)")

    if download_failed or skipped_unsupported:
        print(f"  download_failed={download_failed} "
              f"unsupported_ext={skipped_unsupported}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
