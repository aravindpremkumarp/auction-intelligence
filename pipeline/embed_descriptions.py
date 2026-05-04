"""
pipeline/embed_descriptions.py
------------------------------
Embed every :AuctionProperty.description into a 3072-dim vector via Google's
gemini-embedding-2 multimodal model and write it back as
`p.description_embedding`. Also (re)creates the `property_desc_idx` vector
index with the right dimensions.

This complements:
  - `pipeline/embed_markdowns.py`  (Document.markdown_embedding, structured text)
  - `pipeline/embed_notices.py`    (Document.image_embedding, image / PDF bytes)

All three share gemini-embedding-2 (3072 dims) so scores are directly
comparable across indexes — `semantic_search` ranks by max cosine across all
three lenses.

Run:  python -m pipeline.embed_descriptions

Safe to re-run. Properties that already have a description_embedding are
skipped unless --force is passed. Resumable: aborts mid-run leave any written
vectors intact.
"""
from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from api.neo4j_client import run_read_query, session
from pipeline.embeddings import (
    GEMINI_EMBED_DIM, GEMINI_EMBED_MODEL, embed_text_gemini,
)


INDEX_NAME = "property_desc_idx"


def fetch_pending(force: bool) -> list[dict]:
    if force:
        cypher = """
            MATCH (p:AuctionProperty)
            WHERE p.description IS NOT NULL AND trim(p.description) <> ''
            RETURN p.auction_id AS auction_id, p.description AS description
        """
    else:
        cypher = """
            MATCH (p:AuctionProperty)
            WHERE p.description IS NOT NULL AND trim(p.description) <> ''
              AND p.description_embedding IS NULL
            RETURN p.auction_id AS auction_id, p.description AS description
        """
    return run_read_query(cypher, max_rows=20_000)


def write_embeddings(rows: list[dict]) -> None:
    cypher = """
        UNWIND $rows AS row
        MATCH (p:AuctionProperty {auction_id: row.auction_id})
        SET p.description_embedding = row.embedding
    """
    with session() as s:
        s.run(cypher, {"rows": rows})


def drop_old_index_if_legacy() -> None:
    """If a property_desc_idx exists with the legacy 1536 dimensions
    (OpenAI text-embedding-3-small), drop it so we can recreate at 3072."""
    with session() as s:
        rows = list(s.run(
            "SHOW INDEXES YIELD name, options "
            "WHERE name = $name "
            "RETURN options.indexConfig.`vector.dimensions` AS dim",
            {"name": INDEX_NAME},
        ))
        if rows and rows[0]["dim"] != GEMINI_EMBED_DIM:
            print(f"  dropping legacy {INDEX_NAME} (dim={rows[0]['dim']})")
            s.run(f"DROP INDEX {INDEX_NAME} IF EXISTS")


def ensure_vector_index() -> None:
    cypher = f"""
        CREATE VECTOR INDEX {INDEX_NAME} IF NOT EXISTS
        FOR (p:AuctionProperty) ON (p.description_embedding)
        OPTIONS {{ indexConfig: {{
            `vector.dimensions`: {GEMINI_EMBED_DIM},
            `vector.similarity_function`: 'cosine'
        }} }}
    """
    with session() as s:
        s.run(cypher)


def _embed_one(auction_id: str, text: str) -> dict:
    """Worker: embed one description, return result dict.

    Returns one of:
      {"auction_id": id, "embedding": [...]}   on success
      {"auction_id": id, "quota": True}        on daily / free-tier quota
      {"auction_id": id, "error": "..."}       on other failures
    """
    for attempt in range(2):
        try:
            vec = embed_text_gemini(text)
            return {"auction_id": auction_id, "embedding": vec}
        except Exception as e:
            msg = str(e)
            if "RESOURCE_EXHAUSTED" in msg or "429" in msg:
                if "PerDay" in msg or "free_tier" in msg or attempt == 1:
                    return {"auction_id": auction_id, "quota": True}
                time.sleep(30)
                continue
            return {"auction_id": auction_id, "error": f"embed-fail: {e}"}
    return {"auction_id": auction_id, "error": "embed-fail: exhausted retries"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true",
                    help="re-embed even if p.description_embedding exists")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap to first N properties (staged rollout)")
    ap.add_argument("--workers", type=int, default=8,
                    help="thread-pool size for concurrent Gemini calls")
    ap.add_argument("--write-batch", type=int, default=64,
                    help="rows per Neo4j UNWIND write")
    args = ap.parse_args()

    drop_old_index_if_legacy()

    pending = fetch_pending(args.force)
    if args.limit:
        pending = pending[:args.limit]
    total = len(pending)

    if total == 0:
        print("nothing to embed")
        ensure_vector_index()
        return 0

    print(f"Embedding {total} property descriptions via "
          f"{GEMINI_EMBED_MODEL} ({GEMINI_EMBED_DIM} dims) "
          f"with {args.workers} workers")
    print("(re-runs skip properties that already have p.description_embedding; "
          "pass --force to override)")

    payloads: list[dict] = []
    done = 0
    failed = 0
    quota_exhausted = False

    def safe_write(rows: list[dict]) -> bool:
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
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(_embed_one, r["auction_id"], r["description"]):
                   r["auction_id"] for r in pending}
        try:
            for fut in as_completed(futures):
                res = fut.result()
                if res.get("quota"):
                    print("  [QUOTA EXHAUSTED] stopping cleanly. "
                          "Resume tomorrow or upgrade tier.")
                    quota_exhausted = True
                    break
                if "error" in res:
                    print(f"  [{res['error']}] {res['auction_id']}")
                    failed += 1
                    continue
                payloads.append({"auction_id": res["auction_id"],
                                 "embedding": res["embedding"]})
                done += 1

                if len(payloads) >= args.write_batch:
                    if safe_write(payloads):
                        payloads = []

                if done % 50 == 0 or done == total:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0
                    eta = (total - done - failed) / rate if rate > 0 else 0
                    print(f"  [{done}/{total}]  rate={rate:.1f}/s  eta={eta:.0f}s  "
                          f"(failed={failed})", flush=True)
        finally:
            if quota_exhausted:
                for f in futures:
                    f.cancel()

    if payloads:
        safe_write(payloads)

    print(f"\nWrote {done} embeddings  failed={failed}  "
          f"quota_exhausted={quota_exhausted}")
    print("Creating vector index (idempotent)...")
    ensure_vector_index()
    if quota_exhausted:
        print("\nNOTE: Daily quota exhausted before completion. Re-run this "
              "script after the reset — it will skip already-embedded "
              "properties and pick up where it left off.")
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
