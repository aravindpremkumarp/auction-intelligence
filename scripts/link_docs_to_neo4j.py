"""
scripts/link_docs_to_neo4j.py
------------------------------
For every TN auction property, creates a Document node for each download
file and links it via (AuctionProperty)-[:HAS_DOCUMENT]->(Document).

Sets:
  Document.filename     — local filename (e.g. sbi17783221633083.pdf)
  Document.storage_key  — R2 object key  (e.g. notices/762710/sbi17783221633083.pdf)
  Document.public_url   — full public URL (R2_PUBLIC_BASE_URL + storage_key)
  Document.file_type    — 'pdf' | 'image' | 'other'

A Document is only created when BOTH conditions hold:
  1. The scraper recorded a successful local download (filename appears in
     ``downloads_found``, NOT in ``downloads_missing``).
  2. The corresponding object exists in R2.

This prevents phantom Documents whose ``public_url`` returns 404 in the
review UI.

Safe to re-run (MERGE idempotent).

Run:
    python scripts/link_docs_to_neo4j.py --dry-run   # preview counts
    python scripts/link_docs_to_neo4j.py              # live
    python scripts/link_docs_to_neo4j.py --skip-r2-check   # trust JSONL only
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)

from neo4j import GraphDatabase

# ── Config ────────────────────────────────────────────────────────────────────
NEO4J_URI      = os.environ["NEO4J_URI"]
NEO4J_USERNAME = os.environ["NEO4J_USERNAME"]
NEO4J_PASSWORD = os.environ["NEO4J_PASSWORD"]
NEO4J_DATABASE = os.environ["NEO4J_DATABASE"]
PUBLIC_BASE    = os.environ["R2_PUBLIC_BASE_URL"].rstrip("/")

JSONL_FILE     = ROOT / "data" / "tn_auction_data.jsonl"
BATCH_SIZE     = 200

# ── Document constraint ───────────────────────────────────────────────────────
CONSTRAINT = (
    "CREATE CONSTRAINT doc_filename IF NOT EXISTS "
    "FOR (d:Document) REQUIRE d.storage_key IS UNIQUE"
)

# ── Cypher: MERGE Document + link to AuctionProperty ─────────────────────────
LINK_QUERY = """
UNWIND $rows AS row
MATCH (a:AuctionProperty {auction_id: row.auction_id})
MERGE (d:Document {storage_key: row.storage_key})
SET
  d.filename   = row.filename,
  d.public_url = row.public_url,
  d.file_type  = row.file_type
MERGE (a)-[:HAS_DOCUMENT]->(d)
"""


def guess_file_type(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return "pdf"
    if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"):
        return "image"
    return "other"


def _coerce_list(value) -> list[str]:
    """Normalize a list/semicolon-string field to a stripped list of filenames."""
    if not value:
        return []
    if isinstance(value, str):
        return [f.strip() for f in value.split(";") if f.strip()]
    return [str(f).strip() for f in value if str(f).strip()]


def build_rows(jsonl_path: Path) -> list[dict]:
    """Build flat list of {auction_id, filename, storage_key, public_url, file_type}.

    Only includes files the scraper confirmed it downloaded locally. We trust
    ``downloads_found`` when present; for older records that lack it we fall
    back to ``downloads_list`` minus ``downloads_missing``.
    """
    rows = []
    skipped_missing = 0
    with open(jsonl_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue

            aid = rec.get("auction_id", "")

            found   = _coerce_list(rec.get("downloads_found"))
            missing = set(_coerce_list(rec.get("downloads_missing")))
            listed  = _coerce_list(rec.get("downloads_list"))

            # Prefer the scraper's explicit success list. For legacy records
            # that don't track it, derive: listed - missing.
            if found:
                downloaded = found
            else:
                downloaded = [f for f in listed if f not in missing]

            skipped_missing += len(missing)

            for fname in downloaded:
                if not fname or fname.upper() == "N/A":
                    continue
                storage_key = f"notices/{aid}/{fname}"
                rows.append({
                    "auction_id" : aid,
                    "filename"   : fname,
                    "storage_key": storage_key,
                    "public_url" : f"{PUBLIC_BASE}/{storage_key}",
                    "file_type"  : guess_file_type(fname),
                })

    if skipped_missing:
        print(f"  Skipped {skipped_missing:,} download entries flagged as missing by scraper")
    return rows


def pick_canonical_keys(r2_keys: set[str],
                        existing_doc_keys: dict[str, str]) -> dict[str, str]:
    """filename -> the one R2 key that should represent it.

    A file can sit in R2 under several auction_ids, because the uploader's
    filename->auction_id map used to keep only the last writer. Prefer the key
    an existing Document already points at: choosing a different copy makes
    MERGE create a second, empty Document beside the one carrying the
    extraction output. With no Document to defer to, pick the lowest key so the
    choice is stable across runs.
    """
    canonical: dict[str, str] = {}
    for k in sorted(r2_keys):
        name = k.rsplit("/", 1)[-1]
        if existing_doc_keys.get(name) == k:
            canonical[name] = k
        elif name not in canonical:
            canonical[name] = k
    return canonical


def resolve_storage_keys(rows: list[dict], r2_keys: set[str],
                         existing_doc_keys: dict[str, str]
                         ) -> tuple[list[dict], int, int]:
    """Point every row at a storage_key that exists in R2.

    build_rows() optimistically assumes notices/<this aid>/<file>, but a batch
    notice is stored once, so every other property sharing it would fail the R2
    gate and be dropped. Returns (rows, dropped, remapped).
    """
    canonical = pick_canonical_keys(r2_keys, existing_doc_keys)
    resolved: list[dict] = []
    dropped = remapped = 0
    for r in rows:
        if r["storage_key"] in r2_keys:
            resolved.append(r)
            continue
        key = canonical.get(r["filename"])
        if not key:
            dropped += 1
            continue
        r["storage_key"] = key
        r["public_url"] = f"{PUBLIC_BASE}/{key}"
        resolved.append(r)
        remapped += 1
    return resolved, dropped, remapped


def fetch_existing_doc_keys() -> dict[str, str]:
    """filename -> storage_key for Documents already in the graph.

    Used to keep a shared notice pinned to the key its extracted Document
    already points at, instead of drifting to another copy of the same file.
    """
    drv = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))
    try:
        with drv.session(database=NEO4J_DATABASE) as s:
            return {
                r["fn"]: r["sk"]
                for r in s.run(
                    "MATCH (d:Document) "
                    "WHERE d.filename IS NOT NULL AND d.storage_key IS NOT NULL "
                    "RETURN d.filename AS fn, d.storage_key AS sk"
                )
            }
    finally:
        drv.close()


def fetch_r2_keys() -> set[str]:
    """List every object key in the R2 bucket. Single API pass."""
    import boto3
    s3 = boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET"]
    keys: set[str] = set()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for obj in page.get("Contents", []):
            keys.add(obj["Key"])
    return keys


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true",
                        help="Show counts only, do not touch Neo4j.")
    parser.add_argument("--skip-r2-check", action="store_true",
                        help="Skip the R2 existence check. Use only when R2 "
                             "is unreachable; the JSONL filter alone is "
                             "weaker protection against phantom Documents.")
    args = parser.parse_args()

    print("Building document rows from tn_auction_data.jsonl...")
    rows = build_rows(JSONL_FILE)
    print(f"  Doc-property pairs (after JSONL filter): {len(rows):,}")

    if not args.skip_r2_check:
        print("Fetching R2 object keys to gate Document creation...")
        r2_keys = fetch_r2_keys()
        print(f"  R2 objects found: {len(r2_keys):,}")

        existing = fetch_existing_doc_keys()
        rows, dropped, remapped = resolve_storage_keys(rows, r2_keys, existing)

        if remapped:
            print(f"  Remapped {remapped:,} rows to a shared notice's existing R2 key")
        if dropped:
            print(f"  Dropped {dropped:,} rows whose file is not yet in R2 "
                  f"(run upload_downloads_to_r2.py first to land them)")

    print(f"  Final doc-property pairs : {len(rows):,}")

    # Summary by file type
    from collections import Counter
    type_counts = Counter(r["file_type"] for r in rows)
    for ft, cnt in type_counts.most_common():
        print(f"    {ft:8s}: {cnt:,}")

    if args.dry_run:
        print("\n[dry-run] No Neo4j writes. Sample rows:")
        for r in rows[:5]:
            print(f"  {r['auction_id']} | {r['filename']} | {r['public_url']}")
        return

    print(f"\nConnecting to {NEO4J_URI} ...")
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USERNAME, NEO4J_PASSWORD))

    # Ensure unique constraint on storage_key
    with driver.session(database=NEO4J_DATABASE) as s:
        try:
            s.run(CONSTRAINT)
            print("  Constraint ensured: Document.storage_key IS UNIQUE")
        except Exception as e:
            print(f"  [WARN] Constraint: {e}")

    # Batch upsert
    linked = errors = 0
    total  = len(rows)

    with driver.session(database=NEO4J_DATABASE) as s:
        for i in range(0, total, BATCH_SIZE):
            batch = rows[i : i + BATCH_SIZE]
            try:
                s.run(LINK_QUERY, rows=batch)
                linked += len(batch)
                pct = linked / total * 100
                print(f"  [{linked:>5}/{total}] {pct:5.1f}% linked", end="\r")
            except Exception as e:
                errors += 1
                print(f"\n  [ERROR] batch {i}: {e}")

    driver.close()

    print(f"\n\n{'='*55}")
    print(f"  Document-property links created/verified : {linked:,}")
    print(f"  Errors                                   : {errors}")
    print(f"{'='*55}")
    print("\nVerify in Neo4j Browser:")
    print("  MATCH (a:AuctionProperty)-[:HAS_DOCUMENT]->(d:Document)")
    print("  RETURN count(d) AS docs, count(DISTINCT a) AS props")


if __name__ == "__main__":
    main()
