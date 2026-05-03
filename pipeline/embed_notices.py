"""
pipeline/embed_notices.py
-------------------------
Embed every :Document's notice file (image / PDF) into a 3072-dim vector via
Google's gemini-embedding-2 multimodal model, write the vector back as
`d.image_embedding`, and create the `notice_image_idx` vector index.

This complements `pipeline/embed_descriptions.py` (which embeds the short
property description into `property_desc_idx`). Notices embedded here capture
the full notice as a multimodal object — table layout, seals, multi-page
structure — for richer semantic / cross-modal search.

Run:  python -m pipeline.embed_notices

Safe to re-run. Documents that already have an embedding are skipped unless
--force is passed. Resumable: aborts mid-run leave the cache and any written
vectors intact.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from api.neo4j_client import run_read_query, session
from pipeline.embeddings import (
    GEMINI_EMBED_DIM, GEMINI_EMBED_MODEL, embed_file_gemini,
)


REPO_ROOT = Path(__file__).resolve().parent.parent
DOWNLOADS_TN = REPO_ROOT / "downloads" / "tn_properties"
DOWNLOADS_FALLBACK = REPO_ROOT / "downloads"

INDEX_NAME = "notice_image_idx"
SLEEP_BETWEEN = 0.15  # seconds between API calls — gentle rate limit


MIME_BY_EXT = {
    ".jpg":  "image/jpeg",
    ".jpeg": "image/jpeg",
    ".jfif": "image/jpeg",
    ".png":  "image/png",
    ".pdf":  "application/pdf",
}


def find_disk_path(filename: str) -> Path | None:
    for base in (DOWNLOADS_TN, DOWNLOADS_FALLBACK):
        p = base / filename
        if p.exists():
            return p
    return None


def fetch_pending(force: bool) -> list[dict]:
    if force:
        cypher = """
            MATCH (d:Document)
            WHERE d.filename IS NOT NULL AND d.filename <> ''
            RETURN d.file_path AS file_path, d.filename AS filename
        """
    else:
        cypher = """
            MATCH (d:Document)
            WHERE d.filename IS NOT NULL AND d.filename <> ''
              AND d.image_embedding IS NULL
            RETURN d.file_path AS file_path, d.filename AS filename
        """
    return run_read_query(cypher, max_rows=10_000)


def write_embeddings(rows: list[dict]) -> None:
    cypher = """
        UNWIND $rows AS row
        MATCH (d:Document {file_path: row.file_path})
        SET d.image_embedding = row.embedding
    """
    with session() as s:
        s.run(cypher, {"rows": rows})


def ensure_vector_index() -> None:
    cypher = f"""
        CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
        FOR (d:Document) ON (d.image_embedding)
        OPTIONS {{ indexConfig: {{
            `vector.dimensions`: {GEMINI_EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }} }}
    """
    with session() as s:
        s.run(cypher)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-embed even if d.image_embedding already exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N Documents (staged rollout)")
    ap.add_argument("--write-batch", type=int, default=64,
                    help="rows per Neo4j UNWIND write")
    args = ap.parse_args()

    pending = fetch_pending(args.force)
    if args.limit:
        pending = pending[:args.limit]
    total = len(pending)

    if total == 0:
        print("nothing to embed")
        ensure_vector_index()
        return 0

    print(f"Embedding {total} Documents via {GEMINI_EMBED_MODEL} ({GEMINI_EMBED_DIM} dims)")
    print("(re-runs skip Documents that already have d.image_embedding; pass --force to override)")

    payloads: list[dict] = []
    done = 0
    failed = 0
    missing_disk = 0
    quota_exhausted = False

    def safe_write(rows: list[dict]) -> bool:
        """Write with one retry on Neo4j session expiry."""
        for attempt in range(2):
            try:
                write_embeddings(rows)
                return True
            except Exception as e:
                if attempt == 0:
                    print(f"  [neo4j retry] {type(e).__name__}: {e}")
                    time.sleep(2)
                else:
                    print(f"  [neo4j FAIL] giving up on this batch: {e}")
                    return False
        return False

    t0 = time.time()
    for r in pending:
        fp = r["file_path"]
        fn = r["filename"]
        disk = find_disk_path(fn)
        if disk is None:
            missing_disk += 1
            continue
        mime = MIME_BY_EXT.get(disk.suffix.lower())
        if mime is None:
            print(f"  [skip] unsupported ext: {disk.name}")
            failed += 1
            continue

        # Embed with one retry on transient 429
        vec = None
        for attempt in range(2):
            try:
                data = disk.read_bytes()
                vec = embed_file_gemini(data, mime)
                break
            except Exception as e:
                msg = str(e)
                # Daily/free-tier quota exhausted → stop gracefully
                if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                    if "EmbedContentRequestsPerDay" in msg or "embed_content_free_tier_requests" in msg or attempt == 1:
                        print(f"  [QUOTA EXHAUSTED] stopping cleanly. Resume tomorrow or upgrade tier.")
                        quota_exhausted = True
                        break
                    # Per-minute rate-limit: try waiting once
                    print(f"  [rate-limit] {fn}: waiting 60s and retrying once")
                    time.sleep(60)
                    continue
                print(f"  [embed-fail] {fn}: {e}")
                failed += 1
                break
        if quota_exhausted:
            break
        if vec is None:
            continue
        payloads.append({"file_path": fp, "embedding": vec})
        done += 1

        # Flush in batches
        if len(payloads) >= args.write_batch:
            if not safe_write(payloads):
                # Write failed → don't lose work; try once more end-of-loop
                pass
            else:
                payloads = []

        if done % 25 == 0 or done == total:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            eta = (total - done) / rate if rate > 0 else 0
            print(f"  [{done}/{total}]  rate={rate:.1f}/s  eta={eta:.0f}s  "
                  f"(failed={failed}, missing_disk={missing_disk})", flush=True)

        time.sleep(SLEEP_BETWEEN)

    # Final flush
    if payloads:
        safe_write(payloads)

    print(f"\nWrote {done} embeddings  failed={failed}  "
          f"missing_disk={missing_disk}  quota_exhausted={quota_exhausted}")
    print("Creating vector index (idempotent)...")
    ensure_vector_index()
    if quota_exhausted:
        print("\nNOTE: Daily free-tier quota (1,000/day) was exhausted before "
              "completion. Re-run this script tomorrow (UTC midnight reset) — "
              "it will skip the already-embedded Documents and pick up where "
              "it left off. Or upgrade to a paid Gemini tier for unrestricted "
              "throughput.")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
