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

One page is OCR'd once. A portal names each upload with its millisecond, so
one notice published against six lots arrives as six file names holding
identical bytes (``pipeline/notice_twins``). Every downloaded file is
SHA-256'd into ``Document.content_sha256``; a hash some other Document
already holds markdown for is copied instead of OCR'd, and within one run
only the first copy of a repeated page is sent to the provider. Nothing is
overwritten — a copy lands only on a Document whose markdown is still empty.

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
from pipeline.ink_fingerprint import content_hash
from pipeline.load_markdowns_to_neo4j import (
    DEFAULT_MARKDOWN_MODEL,
    DEFAULT_MARKDOWN_SOURCE,
    read_raw_artifacts,
    write_markdowns,
)
from pipeline.mineru import MINERU_SUPPORTED_EXTS, assemble_markdown
from pipeline.notice_twins import plan_reuse, source_key
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
        RETURN d.filename       AS filename,
               d.file_path      AS file_path,
               d.public_url     AS public_url,
               d.content_sha256 AS content_sha256
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


# ── Same-page reuse ──────────────────────────────────────────────────────────
# The file is already on disk by the time these run: hashing it is free next to
# the OCR call the hash may save. See pipeline/notice_twins for why the file
# bytes are the right key for this pass (and the markdown for extraction's).

def hash_downloaded(docs: list[dict]) -> int:
    """Stamp ``content_sha256`` on each downloaded doc, in place.

    Returns how many were hashed. A file that cannot be read is left without a
    hash, which ``plan_reuse`` treats as its own group — it is OCR'd as before.
    """
    n = 0
    for d in docs:
        if d.get("content_sha256"):
            continue  # already fingerprinted by an earlier run
        try:
            sha = content_hash((DOWNLOAD_TARGET_DIR / d["filename"]).read_bytes())
        except OSError as e:
            print(f"    [hash failed] {d['filename']}: {e}")
            continue
        if sha:
            d["content_sha256"] = sha
            n += 1
    return n


def write_content_hashes(docs: list[dict]) -> int:
    """Persist the hashes we just took, so the next run groups without reading.

    Also feeds ``scripts/find_duplicate_notices.py --cached``, which reads the
    same field.
    """
    rows = [{"file_path": d["file_path"], "sha": d["content_sha256"]}
            for d in docs if d.get("content_sha256") and d.get("file_path")]
    if not rows:
        return 0
    for i in range(0, len(rows), 200):
        run_query(
            """
            UNWIND $rows AS row
            MATCH (d:Document {file_path: row.file_path})
            SET d.content_sha256   = row.sha,
                d.fingerprinted_at = coalesce(d.fingerprinted_at, datetime())
            """,
            {"rows": rows[i:i + 200]},
        )
    return len(rows)


def fetch_donors(shas: list[str]) -> dict[str, str]:
    """``{sha: filename}`` for pages some Document already holds markdown for.

    One donor per hash, chosen by filename so a re-run picks the same one.
    """
    if not shas:
        return {}
    rows = run_read_query(
        """
        UNWIND $shas AS sha
        MATCH (d:Document {content_sha256: sha})
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''
        WITH sha, d ORDER BY d.filename
        RETURN sha AS sha, collect(d.filename)[0] AS donor
        """,
        {"shas": sorted(set(shas))},
        max_rows=10_000,
    )
    return {r["sha"]: r["donor"] for r in rows if r.get("donor")}


def copy_markdown(copies: list[dict]) -> int:
    """Copy a donor's OCR onto same-byte copies that still have none.

    Everything the OCR write path sets travels together — the markdown, the raw
    artifacts behind it, the block list the annotator edits, and the health
    score — because a Document holding markdown whose blocks belong to a
    different pass is worse than one holding nothing. ``markdown_source`` is
    carried across unchanged (the text really did come from that engine) and
    ``markdown_reused_from`` records the donor, so a reader can tell a copy from
    a paid run.

    The ``WHERE`` re-asserts what the caller already promised: a copy never
    lands on a Document that has markdown of its own.
    """
    rows = [c for c in copies if c.get("donor") and c.get("file_path")]
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 200):
        res = run_query(
            """
            UNWIND $rows AS row
            MATCH (src:Document {filename: row.donor})
            WHERE src.markdown IS NOT NULL AND src.markdown <> ''
            // A filename can still address more than one node until
            // scripts/dedupe_documents.py --global has run; take one, by a
            // stable key, so a re-run copies the same text it copied before.
            WITH row, src ORDER BY src.file_path
            WITH row, collect(src)[0] AS src
            MATCH (dst:Document {file_path: row.file_path})
            WHERE dst.markdown IS NULL OR dst.markdown = ''
            SET dst.markdown             = src.markdown,
                dst.markdown_raw         = src.markdown_raw,
                dst.markdown_raw_at      = src.markdown_raw_at,
                dst.blocks_raw           = src.blocks_raw,
                dst.blocks               = src.blocks,
                dst.blocks_revision      = coalesce(dst.blocks_revision, 0) + 1,
                dst.markdown_source      = src.markdown_source,
                dst.markdown_model       = src.markdown_model,
                dst.markdown_loaded_at   = datetime(),
                dst.markdown_reused_from = src.filename,
                dst.ocr_health_score     = src.ocr_health_score,
                dst.ocr_health_flags     = src.ocr_health_flags,
                dst.ocr_health_at        = src.ocr_health_at,
                dst.parse_quality_score  = src.parse_quality_score,
                dst.parse_quality_at     = src.parse_quality_at
            RETURN count(dst) AS n
            """,
            {"rows": rows[i:i + 200]},
        )
        written += (res[0].get("n") or 0) if res else 0
    return written


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
                   score=health["score"], flags=health["flags"], ok=True,
                   parse_quality=datalab_api.parse_quality(result))
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
        "parse_quality": r.get("parse_quality"),
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
                d.ocr_health_at      = datetime(),
                d.parse_quality_score = coalesce(row.parse_quality,
                                                 d.parse_quality_score),
                d.parse_quality_at    = CASE WHEN row.parse_quality IS NULL
                                            THEN d.parse_quality_at ELSE datetime() END
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
        # Reuse is planned off stored hashes here: a dry run downloads nothing,
        # so a page whose copies have never been fingerprinted still shows as
        # separate work. The real run hashes as it downloads and finds the rest.
        known = fetch_donors([m["content_sha256"] for m in missing
                              if m.get("content_sha256")])
        to_ocr, copies = plan_reuse(missing, known, key=source_key)
        leaders = {d["filename"] for d in to_ocr}
        for m in to_ocr:
            print(f"  OCR   {m['filename']}  <- {m['public_url']}")
        for c in copies:
            via = "this run" if c["donor"] in leaders else "the graph"
            print(f"  copy  {c['filename']}  <- {c['donor']} ({via})")
        print(f"\nwould OCR {len(to_ocr)}, copy {len(copies)} "
              f"(on hashes stored so far)")
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

    # ── Stage 0.5: same-page reuse ──────────────────────────────────────────
    # One notice published against N lots is N file names holding one page.
    # Hash what we just downloaded, and OCR each distinct page once.
    print("\n[Stage 0.5] Fingerprinting downloads for same-page reuse")
    hashed = hash_downloaded(downloaded)
    stamped = write_content_hashes(downloaded)
    donors = fetch_donors([d["content_sha256"] for d in downloaded
                           if d.get("content_sha256")])
    to_ocr, copies = plan_reuse(downloaded, donors, key=source_key)
    print(f"  newly hashed={hashed}/{len(downloaded)} stamped={stamped} "
          f"donors_in_graph={len(donors)}")
    print(f"  to OCR: {len(to_ocr)}   to copy: {len(copies)} "
          f"(saves {len(downloaded) - len(to_ocr)} paid job(s))")

    # A copy whose donor already holds markdown can be written now; one whose
    # donor is a leader in this run has to wait until that leader's OCR lands.
    leaders = {d["filename"] for d in to_ocr}
    reuse_now = [c for c in copies if c["donor"] not in leaders]
    reuse_after = [c for c in copies if c["donor"] in leaders]
    if reuse_now:
        n = copy_markdown(reuse_now)
        print(f"  reused OCR already in the graph for {n} Document(s)")

    if not to_ocr:
        print("\nEvery downloaded page was already OCR'd — no provider calls made.")
        return 0

    work = [{"filename": m["filename"], "file_path": m["file_path"]}
            for m in to_ocr]

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

    # ── Stage 3: hand this run's OCR to the copies of the same page ─────────
    if reuse_after:
        n = copy_markdown(reuse_after)
        print(f"\n[Stage 3] Copied OCR onto {n} / {len(reuse_after)} same-page "
              f"Document(s)")

    # ── Stage 4: final tally ────────────────────────────────────────────────
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
