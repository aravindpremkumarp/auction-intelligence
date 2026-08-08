"""Backfill OCR markdown for Documents that lack it.

Targets only :Document nodes where ``d.markdown IS NULL`` or empty —
the existing markdowns are untouched. Downloads each missing notice
from its R2 ``public_url`` into ``downloads/tn_properties/``, then OCRs
with the engine chosen by ``--engine``:

``mineru`` (default)
    Reuses ``stage1_mineru`` from ``scripts.ocr_with_mineru`` to OCR in
    batches of 20, caching under ``pipeline/cache/mineru_markdown/``.
    Writes via ``pipeline.load_markdowns_to_neo4j.write_markdowns``.

``datalab``
    One Datalab job per file (``pipeline.datalab_api``), parsed into
    blocks by ``pipeline.datalab.parse_datalab_blocks`` and written with
    ``markdown_source='datalab'`` — the same write shape as
    ``scripts.reocr_low_health_datalab``, minus its strict-improvement
    gate (a Document with no markdown has no score to improve on, so any
    non-empty result is a win).

    Datalab normally picks its mode from ``notice_type``, but a Document
    with no markdown has not been classified yet — so the mode is a flag
    here, defaulting to ``accurate``. Fast mode on an unclassified batch
    notice collapses its lot table, which is expensive to detect later.

Idempotent: a re-run skips Documents that now have markdown, and the
MinerU helpers skip files whose cache file already exists.

Usage:
  python -m scripts.ocr_missing_markdowns                        # MinerU
  python -m scripts.ocr_missing_markdowns --engine datalab       # Datalab
  python -m scripts.ocr_missing_markdowns --engine datalab --mode fast
  python -m scripts.ocr_missing_markdowns --dry-run              # list only
  python -m scripts.ocr_missing_markdowns --limit 10             # cap N

Auth: MINERU_API_KEY or DATALAB_API_KEY in .env (both paid), depending
on ``--engine``. Network egress to R2 + the chosen OCR provider.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from dotenv import load_dotenv

from api.neo4j_client import run_query, run_read_query
from pipeline import datalab_api
from pipeline.config import DOWNLOADS_DIR
from pipeline.datalab import parse_datalab_blocks
from pipeline.load_markdowns_to_neo4j import (
    DEFAULT_MARKDOWN_MODEL,
    DEFAULT_MARKDOWN_SOURCE,
    read_raw_artifacts,
    write_markdowns,
)
from pipeline.mineru import MINERU_SUPPORTED_EXTS, assemble_markdown
from pipeline.ocr_health import score_ocr_health
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


def _bid() -> str:
    return f"blk_{secrets.token_hex(6)}"


def datalab_one(m: dict, *, mode: str) -> dict:
    """OCR one already-downloaded notice with Datalab. Never raises.

    ``m`` carries ``filename``/``file_path``; the source file is the copy
    Stage 0 placed in ``DOWNLOAD_TARGET_DIR`` (kept on disk, unlike
    reocr_low_health_datalab's tempfile, so a re-run can reuse it).
    """
    out = {**m, "markdown": "", "blocks": [], "score": None, "flags": [],
           "ok": False, "note": ""}
    try:
        src = DOWNLOAD_TARGET_DIR / m["filename"]
        result = datalab_api.run_file(src, output_format="json", mode=mode)
        _md, doc, _img = datalab_api.extract_payload(result)
        blocks = parse_datalab_blocks(doc)
        for b in blocks:
            b["id"] = _bid()
        markdown = result.get("markdown") or assemble_markdown(blocks)
        if not (markdown or "").strip():
            out["note"] = "empty result"
            return out
        health = score_ocr_health(markdown)
        out.update(markdown=markdown, blocks=blocks,
                   score=health["score"], flags=health["flags"], ok=True)
    except Exception as e:
        out["note"] = f"{type(e).__name__}: {e}"
    return out


def write_datalab(results: list[dict], mode: str) -> int:
    """Persist Datalab OCR results. Same field shape as
    scripts.reocr_low_health_datalab.write_back."""
    rows = [{
        "file_path":   r["file_path"],
        "markdown":    r["markdown"],
        "blocks_raw":  json.dumps(r["blocks"], ensure_ascii=False),
        "blocks_json": json.dumps(
            {"schema_version": 1, "blocks": r["blocks"]}, ensure_ascii=False),
        "model":       f"datalab-{mode}",
        "score":       r["score"],
        "flags":       r["flags"],
    } for r in results if r.get("ok")]
    if not rows:
        return 0
    for i in range(0, len(rows), 200):
        run_query(
            """
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            SET d.markdown           = row.markdown,
                d.markdown_source    = 'datalab',
                d.markdown_model     = row.model,
                d.markdown_loaded_at = datetime(),
                d.markdown_raw       = row.markdown,
                d.markdown_raw_at    = datetime(),
                d.blocks_raw         = row.blocks_raw,
                d.blocks             = row.blocks_json,
                d.blocks_revision    = coalesce(d.blocks_revision, 0) + 1,
                d.ocr_health_score   = row.score,
                d.ocr_health_flags   = row.flags,
                d.ocr_health_at      = datetime()
            """,
            {"rows": rows[i:i + 200]},
        )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="list what would be processed; no downloads, no OCR")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N missing Documents")
    ap.add_argument("--engine", choices=["mineru", "datalab"], default="mineru",
                    help="OCR provider (default: mineru)")
    ap.add_argument("--mode", choices=["fast", "accurate"], default="accurate",
                    help="Datalab mode; ignored for --engine mineru "
                         "(default: accurate — these Documents have no "
                         "notice_type yet, so mode cannot be derived)")
    ap.add_argument("--concurrency", type=int, default=4,
                    help="parallel Datalab jobs (default: 4)")
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

    if args.engine == "mineru" and not MINERU_KEY:
        sys.exit("MINERU_API_KEY not set in .env")
    if args.engine == "datalab" and not datalab_api.DATALAB_API_KEY:
        sys.exit("DATALAB_API_KEY not set in .env")

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

    work = [{"filename": m["filename"], "file_path": m["file_path"]}
            for m in downloaded]

    if args.engine == "datalab":
        # ── Stage 1: Datalab OCR (one job per file, N in flight) ────────────
        print(f"\n[Stage 1] Datalab OCR ({args.mode}) on {len(work)} files, "
              f"concurrency={args.concurrency}")
        results: list[dict] = []
        failures = 0
        with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
            futures = {pool.submit(datalab_one, m, mode=args.mode): m
                       for m in work}
            for i, fut in enumerate(as_completed(futures), 1):
                r = fut.result()
                results.append(r)
                if not r["ok"]:
                    failures += 1
                    print(f"  [{i}/{len(work)}] FAIL {r['filename']}: {r['note']}",
                          flush=True)
                elif i % 10 == 0 or i == len(work):
                    ok = sum(1 for x in results if x["ok"])
                    print(f"  [{i}/{len(work)}] ok={ok} failed={failures}",
                          flush=True)

        ok_results = [r for r in results if r["ok"]]
        print(f"  Datalab returned markdown for {len(ok_results)} / {len(work)} files")
        if not ok_results:
            print("\nNo markdown produced — nothing to write.")
            return 1

        print(f"\n[Stage 2] Writing {len(ok_results)} markdowns to Neo4j")
        wrote = write_datalab(ok_results, args.mode)
        print(f"  wrote {wrote} / {len(ok_results)}", flush=True)

        scored = [r["score"] for r in ok_results if r["score"] is not None]
        if scored:
            scored.sort()
            print(f"  ocr_health: min={scored[0]} "
                  f"median={scored[len(scored) // 2]} max={scored[-1]} "
                  f"| below_70={sum(1 for s in scored if s < 70)}")
    else:
        # ── Stage 1: MinerU OCR ─────────────────────────────────────────────
        print(f"\n[Stage 1] MinerU OCR on {len(work)} files")
        mds = stage1_mineru(work)
        print(f"  MinerU returned markdown for {len(mds)} / {len(work)} files")

        # ── Stage 2: write markdowns to Neo4j ───────────────────────────────
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
