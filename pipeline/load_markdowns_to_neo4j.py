"""
pipeline/load_markdowns_to_neo4j.py
-----------------------------------
Load every cached MinerU markdown into the corresponding :Document.markdown
property in Neo4j.

Complements pipeline/embed_markdowns.py (which writes the 3072-dim vector).
Storing the raw markdown alongside the embedding lets the agent and graph
queries surface the full notice text — bank, borrowers, schedule, terms —
not just retrieve Documents by similarity.

The on-disk cache at pipeline/cache/mineru_markdown/ remains the canonical
artifact for re-running OCR; the Neo4j copy is a derived projection meant
for serving and ad-hoc Cypher queries.

Each markdown write also stamps minimal provenance — markdown_source,
markdown_model, markdown_loaded_at — so downstream reviewers can tell
which OCR backend produced the text. Pass --backfill-only to stamp the
provenance fields on every Document that already has markdown but no
provenance recorded (one-shot use after this change ships).

Run:  python -m pipeline.load_markdowns_to_neo4j

Safe to re-run. Documents that already have d.markdown set are skipped
unless --force is passed. Pure local-file + Neo4j-write — no API calls,
finishes in seconds.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from api.neo4j_client import run_query, run_read_query, session
from pipeline.score_markdown import score_freshly_loaded


REPO_ROOT = Path(__file__).resolve().parent.parent
MD_DIR = REPO_ROOT / "pipeline" / "cache" / "mineru_markdown"

# MinerU is the only producer of cached markdowns under MD_DIR. The model
# tag mirrors MinerU's "model_version: vlm" request param in
# scripts/ocr_with_mineru.py:107, so future backends can drop in a
# different value via --markdown-model.
DEFAULT_MARKDOWN_SOURCE = "mineru"
DEFAULT_MARKDOWN_MODEL = "mineru-vlm"


def safe_name(file_path: str) -> str:
    return file_path.replace("/", "_").replace("\\", "_").replace(":", "_")


def fetch_pending(force: bool) -> list[dict]:
    if force:
        cypher = """
            MATCH (d:Document)
            WHERE d.file_path IS NOT NULL AND d.file_path <> ''
            RETURN d.file_path AS file_path
        """
    else:
        cypher = """
            MATCH (d:Document)
            WHERE d.file_path IS NOT NULL AND d.file_path <> ''
              AND d.markdown IS NULL
            RETURN d.file_path AS file_path
        """
    return run_read_query(cypher, max_rows=20_000)


def write_markdowns(rows: list[dict], source: str, model: str) -> None:
    cypher = """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.markdown            = row.markdown,
            d.markdown_source     = $source,
            d.markdown_model      = $model,
            d.markdown_loaded_at  = datetime()
    """
    run_query(cypher, {"rows": rows, "source": source, "model": model})


def backfill_provenance(source: str, model: str) -> int:
    """Stamp provenance on every Document that already has markdown but no
    markdown_source recorded. One-shot use after this change first ships.
    Returns the number of Documents stamped.
    """
    cypher = """
        MATCH (d:Document)
        WHERE d.markdown IS NOT NULL AND d.markdown <> ''
          AND d.markdown_source IS NULL
        SET d.markdown_source     = $source,
            d.markdown_model      = $model,
            d.markdown_loaded_at  = coalesce(d.markdown_loaded_at, datetime())
        RETURN count(d) AS n
    """
    rows = run_query(cypher, {"source": source, "model": model})
    return int(rows[0]["n"]) if rows else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="overwrite even if d.markdown is already set")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents (staged rollout)")
    ap.add_argument("--write-batch", type=int, default=200,
                    help="rows per Neo4j UNWIND write")
    ap.add_argument("--markdown-source", default=DEFAULT_MARKDOWN_SOURCE,
                    help="value to stamp as d.markdown_source")
    ap.add_argument("--markdown-model", default=DEFAULT_MARKDOWN_MODEL,
                    help="value to stamp as d.markdown_model")
    ap.add_argument("--backfill-only", action="store_true",
                    help="skip the load step; only stamp provenance on "
                         "existing Documents that already have markdown")
    args = ap.parse_args()

    if args.backfill_only:
        stamped = backfill_provenance(args.markdown_source, args.markdown_model)
        print(f"Backfilled provenance on {stamped} Documents "
              f"(source={args.markdown_source}, model={args.markdown_model})")
        return 0

    pending = fetch_pending(args.force)
    if args.limit:
        pending = pending[:args.limit]
    total = len(pending)

    if total == 0:
        print("nothing to load")
        stamped = backfill_provenance(args.markdown_source, args.markdown_model)
        if stamped:
            print(f"Stamped provenance on {stamped} pre-existing Documents")
        return 0

    print(f"Loading markdown for {total} Documents from {MD_DIR}")
    print(f"(source={args.markdown_source}, model={args.markdown_model})")
    print("(re-runs skip Documents that already have d.markdown; pass "
          "--force to overwrite)")

    payloads: list[dict] = []
    done = 0
    missing = 0
    failed = 0

    written_file_paths: list[str] = []

    def safe_write(rows: list[dict]) -> bool:
        for attempt in range(3):
            try:
                write_markdowns(rows, args.markdown_source, args.markdown_model)
                written_file_paths.extend(r["file_path"] for r in rows)
                return True
            except Exception as e:
                if attempt < 2:
                    print(f"  [neo4j retry {attempt + 1}] {type(e).__name__}: {e}")
                    time.sleep(2)
                else:
                    print(f"  [neo4j FAIL] {e}")
                    return False
        return False

    t0 = time.time()
    for r in pending:
        fp = r["file_path"]
        md_path = MD_DIR / f"{safe_name(fp)}.md"
        if not md_path.exists():
            missing += 1
            continue
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  [read-fail] {md_path.name}: {e}")
            failed += 1
            continue
        if not text.strip():
            missing += 1
            continue
        payloads.append({"file_path": fp, "markdown": text})
        done += 1

        if len(payloads) >= args.write_batch:
            if safe_write(payloads):
                payloads = []

        if done % 500 == 0 or done == total:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  [{done}/{total}]  rate={rate:.0f}/s  "
                  f"(missing_md={missing}, failed={failed})", flush=True)

    if payloads:
        safe_write(payloads)

    # Catch any Documents that already had markdown set but no provenance
    # (e.g. from a previous loader run before this change shipped).
    stamped = backfill_provenance(args.markdown_source, args.markdown_model)
    backfill_note = f"  backfilled_existing={stamped}" if stamped else ""

    scored = 0
    if written_file_paths:
        try:
            scored = score_freshly_loaded(written_file_paths)
        except Exception as e:
            print(f"  [score-fail] {type(e).__name__}: {e}")

    print(f"\nLoaded {done} markdowns  missing_md={missing}  failed={failed}"
          f"  scored={scored}{backfill_note}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
